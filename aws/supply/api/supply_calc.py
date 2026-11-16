"""Calculos puros de la API `/supply/*` (T40): metricas de stock por almacen, quien aprueba, KPIs y la forma publica de
una PO/TRF. Sin AWS: los datos llegan ya leidos de fp-registry (api/supply_store.py).

Formulas con las claves que pinta la
consola (gcp/console/app/mock_supply.py, la referencia de la forma de respuesta):
- demanda diaria `d` = consumo de los ultimos 30 dias (`MOVE#` kind=consumption) / 30. Sin consumo, la cobertura usa
  la demanda de prueba del mock: punto de reorden en una semana (`max(rop, 1) / 7`).
- `L` = lead time en dias del proveedor mas barato; `z` por criticidad (A 2,05 · B 1,65 · C 1,28).
- `SS = max(safety_stock, ceil(z * sqrt(d) * sqrt(L)))`, `rop = max(reorder_point, ceil(d * L) + SS)`.
- `status`: stockout si available <= 0, low si available <= rop, ok si no. `days_of_coverage = available / d`,
  redondeado a 1 decimal: lo que esta en el estante y libre (lo reservado ya tiene dueno y lo en transito aun no
  llega; `in_transit` y `on_order` van en su propia columna). Disponible 0 -> 0 d.
"""
from __future__ import annotations

import json
import math
from datetime import timedelta

from fplib import authz, clock, ids

Z = {"A": 2.05, "B": 1.65, "C": 1.28}
SAME_PARK_TRANSIT = {"hours": 2, "freight_usd": 0}  # dentro de un parque (5.3)
DEFAULT_THRESHOLD_USD = 1000
DONE = ("approved", "ordered", "in_transit", "received")  # cuentan para la parada evitada
PARK_APPROVERS = ["admin", "supervisor", authz.GLOBAL_ROLE]
HEAD_ONLY = [authz.GLOBAL_ROLE]
HIDDEN = {"PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK", "GSI3PK", "GSI3SK", "task_token"}
# Tipos de la linea de tiempo como los nombra la consola (Supply.jsx EV_LABEL)
EV_TYPE = {"po.ordered": "supply.ordered", "transfer.shipped": "supply.in_transit"}


def cheapest(part_id: str) -> dict | None:
    offers = ids.supplier_parts(part_id)
    return offers[0] if offers else None


def unit_cost(part: dict) -> float:
    if part.get("unit_cost_usd") is not None:
        return part["unit_cost_usd"]
    offer = cheapest(part["part_id"])
    return offer["unit_price_usd"] if offer else 0


def stock_row(item: dict, part: dict, consumed_30d: float = 0) -> dict:
    """Fila de stock con la forma de MockSupply._row a partir de `STOCK#<part>#<zone>` / `META`."""
    _, part_id, zone = item["PK"].split("#", 2)
    policy = ids.part_policy(part_id)
    on_hand, reserved = int(item.get("on_hand", 0)), int(item.get("reserved", 0))
    available = int(item.get("available", on_hand - reserved))
    in_transit, on_order = int(item.get("in_transit", 0)), int(item.get("on_order", 0))
    safety = int(item.get("safety_stock", policy["safety_stock"]))
    crit = item.get("criticality") or policy["criticality"]
    reorder_point = int(item.get("reorder_point", 0))
    d = max(consumed_30d, 0) / 30
    offer = cheapest(part_id)
    lead = offer["lead_time_d"] if offer else 0
    if d > 0:
        ss = max(safety, math.ceil(Z.get(crit, Z["C"]) * math.sqrt(d) * math.sqrt(lead)))
        rop = max(reorder_point, math.ceil(d * lead) + ss)
    else:
        rop = max(reorder_point, safety)
    demand = d if d > 0 else max(rop, 1) / 7
    status = "stockout" if available <= 0 else "low" if available <= rop else "ok"
    return {"part_id": part_id, "zone_id": zone, "tenant_id": item.get("tenant_id") or ids.tenant_of_zone(zone),
            "sku": part.get("sku"), "name": part.get("name"), "on_hand": on_hand, "reserved": reserved,
            "available": available, "on_order": on_order, "in_transit": in_transit, "reorder_point": reorder_point,
            "safety_stock": safety, "criticality": crit, "rop": rop,
            "days_of_coverage": round(max(available, 0) / demand, 1), "status": status,
            "demand_d": round(d, 3), "unit_cost_usd": unit_cost(part)}


def transit(from_zone: str, to_zone: str) -> dict:
    """Horas y flete de traslado entre almacenes (`transit_h` de supply.yaml, en cualquier sentido)."""
    a, b = ids.tenant_of_zone(from_zone), ids.tenant_of_zone(to_zone)
    if a == b:
        return dict(SAME_PARK_TRANSIT)
    row = next((t for t in ids.supply().get("transit_h") or [] if {t["from"], t["to"]} == {a, b}), None)
    return {"hours": row["hours"], "freight_usd": row["freight_usd"]} if row else {"hours": 24, "freight_usd": 0}


def approver_roles(kind: str, total_usd: float, criticality: str) -> list[str]:
    """Tabla 5.4: transferencias y PO caras o de criticidad A las aprueba el jefe; el resto, el parque destino."""
    threshold = ids.supply().get("approval_threshold_usd", DEFAULT_THRESHOLD_USD)
    if kind == "transfer" or total_usd >= threshold or criticality == "A":
        return list(HEAD_ONLY)
    return list(PARK_APPROVERS)


def _num(v):
    """Decimal de DynamoDB -> int si es entero, float si no."""
    if v is None or isinstance(v, (bool, str)):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return int(f) if f == int(f) else f


def plain(o):
    if isinstance(o, dict):
        return {k: plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [plain(v) for v in o]
    return _num(o)


def kind_of(doc_id: str) -> str:
    return "transfer" if doc_id.startswith("TRF-") else "purchase_order"


def _eta_h(meta: dict) -> float:
    for key in ("eta_h", "transit_h"):
        if meta.get(key) is not None:
            return meta[key]
    if meta.get("lead_time_d") is not None:
        return meta["lead_time_d"] * 24
    if meta.get("eta") and meta.get("created_at"):
        try:
            return round((clock.parse(meta["eta"]) - clock.parse(meta["created_at"])).total_seconds() / 3600, 1)
        except ValueError:
            return 0
    return 0


def timeline_entry(item: dict) -> dict:
    raw = item.get("payload")
    env = json.loads(raw) if isinstance(raw, str) else (raw or {})
    env = env if isinstance(env, dict) else {}
    etype = item.get("event_type") or item.get("type") or env.get("event_type")
    sk_ts = item["SK"].split("#")[1] if item.get("SK", "").count("#") >= 1 else None
    out = {"type": EV_TYPE.get(etype, etype), "event_type": etype, "at": env.get("occurred_at") or sk_ts,
           "by": item.get("by") or env.get("source") or "supply-flow"}
    if item.get("detail"):
        out["detail"] = item["detail"]
    return out


def public_doc(meta: dict, events: list[dict] | None = None) -> dict:
    """PO/TRF con las claves de MockSupply (id, kind, zone_id destino, cost_usd, eta_h, timeline, ...). Nunca
    devuelve el `task_token`."""
    meta = plain(meta)
    doc_id = meta.get("id") or meta.get("po_id") or meta.get("trf_id") or meta["PK"].split("#", 1)[1]
    kind = kind_of(doc_id)
    out = {k: v for k, v in meta.items() if k not in HIDDEN}
    out["id"], out["kind"] = doc_id, kind
    out["zone_id"] = meta.get("zone_id") or meta.get("to_zone")
    out["tenant_id"] = meta.get("tenant_id") or ids.tenant_of_zone(out["zone_id"])
    if kind == "transfer":
        out["from_tenant"] = meta.get("from_tenant") or ids.tenant_of_zone(meta.get("from_zone"))
    if meta.get("cost_usd") is None:
        out["cost_usd"] = meta.get("total_usd") if kind == "purchase_order" else meta.get("freight_usd")
        out["cost_usd"] = out["cost_usd"] or 0
    out["eta_h"] = _eta_h(meta)
    out.setdefault("created_at", None)
    out["updated_at"] = meta.get("updated_at") or meta.get("created_at")
    if not meta.get("eta") and out["created_at"]:
        out["eta"] = clock.iso(clock.parse(out["created_at"]) + timedelta(hours=out["eta_h"]))
    if not meta.get("approver_roles"):
        crit = ids.part_policy(meta.get("part_id", ""))["criticality"]
        out["approver_roles"] = approver_roles(kind, out["cost_usd"], crit)
    for key, default in (("options", []), ("approved_by", None), ("downtime_avoided_h", 0),
                         ("downtime_avoided_usd", 0), ("reason", ""), ("source", None), ("waiting_for", None)):
        out.setdefault(key, default)
    timeline = sorted((timeline_entry(e) for e in events or []), key=lambda e: e["at"] or "")
    out["timeline"] = timeline or [{"type": "supply.suggested", "event_type": "supply.suggested",
                                    "at": out["created_at"], "by": meta.get("created_by") or "supply-sourcing"}]
    return out


def visible(doc: dict, tenants) -> bool:
    """Destino u origen en el alcance del usuario (el donante ve sus transferencias salientes)."""
    return doc.get("tenant_id") in tenants or doc.get("from_tenant") in tenants


def kpis(stock: list[dict], docs: list[dict], tenants) -> dict:
    """Forma de MockSupply.kpis. `fill_rate`: share de almacen x repuesto con disponible (> 0) en el alcance; el
    plan pide lineas reservadas / pedidas, pero `RSV#` no tiene indice para contarlas sin un Scan."""
    done = [d for d in docs if d.get("tenant_id") in tenants and d.get("status") in DONE]
    rows = [r for r in stock if r["tenant_id"] in tenants]
    value = {t: round(sum(r["on_hand"] * r["unit_cost_usd"] for r in rows if r["tenant_id"] == t)) for t in tenants}
    stockouts = sum(1 for r in rows if r["status"] == "stockout")
    return {"downtime_avoided_h": round(sum(d.get("downtime_avoided_h") or 0 for d in done), 1),
            "downtime_avoided_usd": round(sum(d.get("downtime_avoided_usd") or 0 for d in done)),
            "open_stockouts": stockouts, "inventory_value_usd": value,
            "fill_rate": round(1 - stockouts / len(rows), 3) if rows else 1.0, "fill_rate_basis": "stock_cells",
            "tenants": list(tenants)}
