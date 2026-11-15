"""Opciones para abastecer un faltante y la sugerencia elegida (diseno de abastecimiento, 5.3 y 5.4).

1. Mismo parque: ya lo hizo `reserve` en aws/registry (zona mas cercana del parque); no se repite.
2. Transferencia desde otro parque: almacenes con `available - q >= safety_stock`; eta y flete de `transit_h`.
3. Compra: cada `supplier_parts` del repuesto; eta = lead_time_d * 24, costo = precio * max(q, min_qty).
Costo total = costo + eta_h * costo de parada del modelo cuando hay una maquina detenida esperando (incidente); sin
maquina, solo el costo. Gana el menor costo total; `downtime_avoided_h` = eta del peor proveedor - eta elegida.
Si gana una transferencia y el donante queda bajo su punto de reorden, se sugiere ademas su PO de reposicion.
La decision es determinista (se puede auditar); la IA solo redacta el porque (T44).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fplib import clock, ids

from supply import policy, store

SAME_PARK_H = 2
CONSUMPTION_DAYS = 30


def stamp(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y%m%dT%H%M")


def po_id(zone_id: str, part_id: str, ts: datetime) -> str:
    return f"PO-{zone_id}-{part_id}-{stamp(ts)}"


def trf_id(from_zone: str, to_zone: str, part_id: str, ts: datetime) -> str:
    return f"TRF-{from_zone}-{to_zone}-{part_id}-{stamp(ts)}"


def model_of(machine_id: str | None) -> str | None:
    if not machine_id:
        return None
    m = ids.machines().get(machine_id)
    return (m or {}).get("model_id") or machine_id.split("-", 1)[0]


def downtime_cost_h(machine_id: str | None) -> float:
    model = model_of(machine_id)
    return float((ids.supply().get("downtime_cost_usd_h") or {}).get(model, 0)) if model else 0.0


def transit(from_tenant: str, to_tenant: str) -> dict | None:
    """Horas y flete entre parques (la tabla es simetrica); dentro de un parque, 2 h sin flete."""
    if from_tenant == to_tenant:
        return {"hours": SAME_PARK_H, "freight_usd": 0}
    for t in ids.supply().get("transit_h") or []:
        if {t["from"], t["to"]} == {from_tenant, to_tenant}:
            return {"hours": float(t["hours"]), "freight_usd": float(t["freight_usd"])}
    return None


def approver_roles(kind: str, total_usd: float, criticality: str) -> list[str]:
    """5.4: transferencias y PO >= umbral o criticidad A -> jefe; el resto tambien admin o supervisor del parque."""
    threshold = float(ids.supply().get("approval_threshold_usd", 1000))
    if kind == "transfer" or total_usd >= threshold or criticality == "A":
        return ["maintenance_head"]
    return ["admin", "supervisor", "maintenance_head"]


RECEIVER_ROLES = ["admin", "technician", "maintenance_head"]


def transfer_options(ddb, part_id: str, qty: int, zone_id: str) -> list[dict]:
    dest = ids.tenant_of_zone(zone_id)
    floor = ids.part_policy(part_id)["safety_stock"]
    out = []
    for z in ids.zones():
        tenant = ids.tenant_of_zone(z)
        if tenant == dest:
            continue
        s = store.stock(ddb, part_id, z)
        route = transit(tenant, dest)
        if not s or not route:
            continue
        safety = max(s["safety_stock"], floor)
        if s["available"] - qty < safety:
            continue
        out.append({"source_kind": "transfer", "from_zone": z, "from_tenant": tenant, "eta_h": route["hours"],
                    "cost_usd": route["freight_usd"], "_available": s["available"], "_stock": s})
    return out


def purchase_options(part_id: str, qty: int) -> list[dict]:
    out = []
    for sp in ids.supplier_parts(part_id):
        q = max(int(qty), int(sp.get("min_qty") or 1))
        out.append({"source_kind": "purchase", "supplier_id": sp["supplier_id"], "eta_h": float(sp["lead_time_d"]) * 24,
                    "cost_usd": float(sp["unit_price_usd"]) * q, "_qty": q, "_offer": sp})
    return out


def rank(options: list[dict], cost_h: float) -> list[dict]:
    for o in options:
        o["total_cost_usd"] = round(o["cost_usd"] + o["eta_h"] * cost_h, 2)
    # empate: la mas rapida, luego el donante con mas stock
    return sorted(options, key=lambda o: (o["total_cost_usd"], o["eta_h"], -o.get("_available", 0)))


def public(options: list[dict]) -> list[dict]:
    """Forma de `payload.options` en contracts/supply.schema.json (sin los campos internos `_*` ni from_tenant)."""
    keys = ("source_kind", "from_zone", "supplier_id", "eta_h", "cost_usd", "total_cost_usd")
    return [{k: _num(o[k]) for k in keys if k in o} for o in options]


def _num(v):
    return int(v) if isinstance(v, float) and v.is_integer() else v


def consumed(ddb, part_id: str, zone_id: str, now: datetime) -> int:
    since = clock.iso(now - timedelta(days=CONSUMPTION_DAYS))
    moves = store.query_all(ddb, f"MOVE#{part_id}#{zone_id}", sk_from=since)
    return sum(-int(m["qty"]) for m in moves if m.get("kind") == "consumption" and int(m.get("qty") or 0) < 0)


def _base(kind: str, doc_id: str, *, tenant_id: str, zone_id: str, part_id: str, qty: int, eta_h: float,
          cost_usd: float, source: str, now: datetime, options: list[dict], reason: str, criticality: str,
          incident_id: str | None = None, machine_id: str | None = None, downtime_avoided_h: float = 0,
          downtime_avoided_usd: float = 0, needed_qty: int | None = None) -> dict:
    created = clock.iso(now)
    prefix = "PO" if kind == "po" else "TRF"
    return {
        "PK": f"{prefix}#{doc_id}", "SK": "META", "id": doc_id, "kind": kind,
        "aggregate_type": "purchase_order" if kind == "po" else "transfer",
        "tenant_id": tenant_id, "zone_id": zone_id, "part_id": part_id, "qty": int(qty), "eta_h": _num(eta_h),
        "eta": clock.iso(now + timedelta(hours=eta_h)), "cost_usd": _num(round(cost_usd, 2)), "status": "suggested",
        "needed_qty": needed_qty, "source": source, "incident_id": incident_id, "machine_id": machine_id, "options": public(options),
        "reason": reason, "criticality": criticality,
        "approver_roles": approver_roles(kind, cost_usd, criticality), "receiver_roles": RECEIVER_ROLES,
        "downtime_avoided_h": _num(round(downtime_avoided_h, 2)),
        "downtime_avoided_usd": _num(round(downtime_avoided_usd, 2)), "created_at": created, "updated_at": created,
        "GSI2PK": "SUPPLY#suggested", "GSI2SK": created, "GSI3PK": f"TENANT#{tenant_id}", "GSI3SK": f"{prefix}#{created}",
    }


def po_doc(option: dict, *, zone_id: str, part_id: str, now: datetime, ts: datetime, **kw) -> dict:
    offer = option["_offer"]
    doc_id = po_id(zone_id, part_id, ts)
    doc = _base("po", doc_id, tenant_id=ids.tenant_of_zone(zone_id), zone_id=zone_id, part_id=part_id,
                qty=option["_qty"], eta_h=option["eta_h"], cost_usd=option["cost_usd"], now=now, **kw)
    return doc | {"po_id": doc_id, "supplier_id": offer["supplier_id"], "unit_price_usd": _num(float(offer["unit_price_usd"])),
                  "total_usd": doc["cost_usd"], "lead_time_d": offer["lead_time_d"]}


def trf_doc(option: dict, *, zone_id: str, part_id: str, qty: int, now: datetime, ts: datetime, **kw) -> dict:
    doc_id = trf_id(option["from_zone"], zone_id, part_id, ts)
    doc = _base("transfer", doc_id, tenant_id=ids.tenant_of_zone(zone_id), zone_id=zone_id, part_id=part_id,
                qty=qty, eta_h=option["eta_h"], cost_usd=option["cost_usd"], now=now, **kw)
    return doc | {"trf_id": doc_id, "from_zone": option["from_zone"], "from_tenant": option["from_tenant"],
                  "to_zone": zone_id, "freight_usd": doc["cost_usd"],
                  "GSI1PK": f"TRFFROM#{option['from_tenant']}", "GSI1SK": doc["created_at"]}


def suggest(ddb, *, part_id: str, qty: int, zone_id: str, ts: datetime, now: datetime | None = None,
            incident_id: str | None = None, machine_id: str | None = None, source: str = "shortage") -> list[dict]:
    """Documentos a crear para un faltante: la sugerencia elegida y, si es una transferencia que deja al donante bajo
    su punto de reorden, la PO de reposicion del donante. [] si no hay ninguna opcion (sin proveedor ni donante).

    `ts` fija el sufijo del id (el `occurred_at` del faltante: un reintento genera el mismo id)."""
    now = now or clock.now()
    cost_h = downtime_cost_h(machine_id)
    criticality = ids.part_policy(part_id)["criticality"]
    buys = purchase_options(part_id, qty)
    options = rank(transfer_options(ddb, part_id, qty, zone_id) + buys, cost_h)
    if not options:
        return []
    best = options[0]
    worst_eta = max((o["eta_h"] for o in buys), default=best["eta_h"])
    avoided_h = max(0.0, worst_eta - best["eta_h"])
    common = {"needed_qty": int(qty), "source": source, "options": options, "criticality": criticality, "incident_id": incident_id,
              "machine_id": machine_id, "downtime_avoided_h": avoided_h, "downtime_avoided_usd": avoided_h * cost_h}
    if best["source_kind"] == "purchase":
        reason = (f"Sin {part_id} en {zone_id} ni sobre el stock de seguridad de otro parque: compra a "
                  f"{best['supplier_id']}, llega en {_num(best['eta_h'])} h.")
        return [po_doc(best, zone_id=zone_id, part_id=part_id, now=now, ts=ts, reason=reason, **common)]
    reason = (f"{best['from_zone']} ({best['from_tenant']}) tiene {best['_available']} y queda sobre su stock de "
              f"seguridad; llega en {_num(best['eta_h'])} h contra {_num(worst_eta)} h de compra.")
    docs = [trf_doc(best, zone_id=zone_id, part_id=part_id, qty=qty, now=now, ts=ts, reason=reason, **common)]
    refill = donor_refill(ddb, best, part_id=part_id, qty=qty, now=now, ts=ts, transfer_id=docs[0]["id"])
    return docs + ([refill] if refill else [])


def donor_refill(ddb, option: dict, *, part_id: str, qty: int, now: datetime, ts: datetime,
                 transfer_id: str) -> dict | None:
    s, zone = option["_stock"], option["from_zone"]
    buys = purchase_options(part_id, 1)
    if not buys:
        return None
    offer = buys[0]["_offer"]  # el mas barato fija el lead time (5.2)
    pol = ids.part_policy(part_id)
    p = policy.plan(d=policy.daily_demand(consumed(ddb, part_id, zone, now)), lead_time_d=float(offer["lead_time_d"]),
                    criticality=pol["criticality"], safety_stock=max(s["safety_stock"], pol["safety_stock"]),
                    reorder_point=s["reorder_point"], unit_price_usd=float(offer["unit_price_usd"]),
                    min_qty=int(offer.get("min_qty") or 1), available=s["available"] - qty,
                    on_order=s["on_order"], in_transit=s["in_transit"])
    if not p["order_qty"]:
        return None
    buy = rank(purchase_options(part_id, p["order_qty"]), 0)[0]
    reason = (f"Reposicion de {zone} tras la transferencia {transfer_id}: queda en {s['available'] - qty} con punto "
              f"de reorden {p['rop']}.")
    doc = po_doc(buy, zone_id=zone, part_id=part_id, now=now, ts=ts, reason=reason, source="shortage",
                 options=[buy], criticality=pol["criticality"])
    return doc | {"replenishes": transfer_id}
