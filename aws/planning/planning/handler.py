"""Lambda `plan` (PlanPreventive de daily-planning): ordenes preventivas y pedidos sugeridos de repuestos.

Lee la flota de fp-state, el stock, las ordenes abiertas y los pedidos en curso de fp-registry, calcula el plan
(planning/plan.py) y escribe:
- `maintenance.order.suggested` en fp-outbox por cada maquina que vence dentro del horizonte y no tiene una orden
  `suggested`; registry la materializa en `ORDER#<maquina>/<due_at>`.
- `REORDER#<part>#<zone>/<created_at>` (`source: planning`) en fp-registry por cada repuesto y zona que queda bajo
  el punto de reorden.
Idempotencia: cada escritura lleva su reclamo `planning:<dia>:<clave>` en fp-inbox en la misma transaccion, asi que
repetir la corrida el mismo dia no duplica nada; y un pedido ya escrito cuenta como en curso en la corrida siguiente.

Entrada: `{}` (regla diaria) o `{"dry_run": true, "horizon_days": 14}`; desde Step Functions llega envuelta como
`{"input": {...}, "execution": "<nombre de la ejecucion>"}`, y el nombre va como `trace_id` del evento.
Con `dry_run` calcula y devuelve el plan sin escribir (`orders` y `reorders` son entonces lo que se escribiria).
"""
from __future__ import annotations

import os
from datetime import timedelta

from fplib import clock, envelope, ids, log, schema
from planning import plan, store

READINGS_WINDOW = timedelta(days=3)  # TTL de las lecturas R# en fp-state
_ddb = None


def ddb():
    global _ddb
    if _ddb is None:
        _ddb = store.client()
    return _ddb


def order_event(entry: dict, now, trace_id: str | None) -> dict:
    payload = {"kind": "preventive", "due_at": entry["due_at"], "reason": entry["reason"],
               "hours_since_service": entry["hours_since_service"],
               "service_interval_h": entry["service_interval_h"]}
    if entry["parts"]:
        payload["parts"] = entry["parts"]
    env = envelope.build("maintenance.order.suggested", "aws.daily-planning", "machine", entry["order_id"],
                         payload, machine_id=entry["machine_id"], occurred_at=now, trace_id=trace_id)
    schema.validate("maintenance.order", env)
    return env


def reorder_item(r: dict, created_at: str, day: str, machine_ids: list[str]) -> dict:
    return {"PK": f"REORDER#{r['part_id']}#{r['zone_id']}", "SK": created_at, "part_id": r["part_id"],
            "zone_id": r["zone_id"], "qty": r["qty"], "source": "planning", "forecast_14d": r["demand_14d"],
            "available": r["available"], "reorder_point": r["reorder_point"], "on_order": r["on_order"],
            "machine_ids": machine_ids or None, "day": day, "created_at": created_at,
            "GSI1PK": "REORDERS", "GSI1SK": f"{created_at}#{r['part_id']}#{r['zone_id']}"}


def build_plan(client, now, horizon_days: int) -> dict:
    today = plan.lima_day(now)
    catalog, bom = ids.machines(), ids.load()["bom"]
    intervals = {m["model_id"]: m["service_interval_h"] for m in ids.load()["models"]}
    fleet = store.machines(client)
    since = clock.iso(now - READINGS_WINDOW)
    due = []
    for meta in fleet:
        rate = plan.hours_per_day(meta, store.oldest_reading(client, meta["machine_id"], since))
        entry = plan.assess(meta, catalog.get(meta["machine_id"], {}), intervals.get(meta.get("model")), bom, rate,
                            today, horizon_days)
        if entry:
            due.append(entry)
    demand = plan.demand(due)
    pending = store.on_order(client, clock.iso(now - timedelta(days=horizon_days)))
    return {"day": today.isoformat(), "machines": len(fleet), "due": due,
            "open": store.open_order_machines(client),
            "reorders": plan.reorders(store.stock(client), demand, pending)}


def handler(event, context=None, client=None):
    event = event or {}
    opts, trace_id = event.get("input", event) or {}, event.get("execution")
    horizon = int(opts.get("horizon_days") or os.environ.get("HORIZON_DAYS", 14))
    dry_run = bool(opts.get("dry_run"))
    client = client or ddb()
    now = clock.now()
    p = build_plan(client, now, horizon)
    day = p["day"]
    orders, skipped_open, already = [], [], []
    for e in p["due"]:
        if e["machine_id"] in p["open"]:
            skipped_open.append(e["machine_id"])
            continue
        if not dry_run:
            try:
                store.put_order_event(client, day, order_event(e, now, trace_id))
            except store.AlreadyPlanned:
                already.append(e["machine_id"])
                continue
        orders.append(e["order_id"])
    demand_from: dict[tuple[str, str], list[str]] = {}  # maquinas cuyo servicio explica la demanda
    for e in p["due"]:
        for part in e["parts"]:
            demand_from.setdefault((part["part_id"], e["zone_id"]), []).append(e["machine_id"])
    reorders, reorders_already = [], []
    created_at = clock.iso(now)
    for r in p["reorders"]:
        key = f"{r['part_id']}#{r['zone_id']}"
        if not dry_run:
            item = reorder_item(r, created_at, day, demand_from.get((r["part_id"], r["zone_id"]), []))
            try:
                store.put_reorder(client, day, item)
            except store.AlreadyPlanned:
                reorders_already.append(key)
                continue
        reorders.append({"part_id": r["part_id"], "zone_id": r["zone_id"], "qty": r["qty"]})
    out = {"day": day, "horizon_days": horizon, "dry_run": dry_run, "machines": p["machines"],
           "due": [{"machine_id": e["machine_id"], "order_id": e["order_id"], "due_at": e["due_at"],
                    "days_to_due": e["days_to_due"]} for e in p["due"]],
           "orders": orders, "skipped_open": skipped_open, "already_planned": already,
           "reorders": reorders, "reorders_already_planned": reorders_already}
    log.event("planning_done", day=day, dry_run=dry_run, trace_id=trace_id, machines=p["machines"],
              due=len(p["due"]), orders=len(orders), skipped_open=len(skipped_open), already_planned=len(already),
              reorders=len(reorders))
    return out
