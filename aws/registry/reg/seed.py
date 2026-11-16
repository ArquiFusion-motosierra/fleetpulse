"""Seed desde contracts/ids.yaml segun el modelo de contracts/secrets.md:
- fp-registry: STOCK#<part>#<zone>/META (on_hand, reserved, available, reorder_point, sku, name).
- fp-state: MACHINE#<id>/META y TECH#<id>/META (GSI1 ZONE#<zona>).
Idempotente: UpdateItem por clave; nunca pisa lo dinamico (stock reservado, severidad, posicion de ingest, tecnico ocupado).
Abastecimiento (T37): el stock lleva `on_order`, `in_transit` (dinamicos, 0 al crear), `safety_stock` y `criticality`
(de supply.yaml `part_policy`); los proveedores van en `SUPPLIER#<id>/META` y `SUPPLIER#<id>/PART#<part>`. El perfil
`demo_supply` deja PRT-04 en 0 en las zonas de mineria (sobrescribe on_hand/reserved/available) para la historia de la
transferencia desde agro; el resto queda como en el catalogo.
Multi-tenancy: con el catalogo unido (fplib.ids.load(), cada fila con `tenant_id`) el stock de cada parque se siembra
solo en las zonas de ese parque y cada item lleva `tenant_id`. Con un ids.yaml suelto (sin `tenant_id`) todo es del
parque por defecto, como antes.
"""
from __future__ import annotations

import re

from reg import store
from reg.store import REGISTRY, STATE, key, vals


def _upd(client, table: str, pk: str, sk: str, fields: dict, keep: dict | None = None) -> None:
    """SET fields; keep = valores iniciales que solo se escriben si el atributo no existe."""
    sets, values, names = [], {}, {}
    for i, (k, v) in enumerate(fields.items()):
        names[f"#f{i}"], values[f"f{i}"] = k, v
        sets.append(f"#f{i} = :f{i}")
    for i, (k, v) in enumerate((keep or {}).items()):
        names[f"#k{i}"], values[f"k{i}"] = k, v
        sets.append(f"#k{i} = if_not_exists(#k{i}, :k{i})")
    client.update_item(TableName=table, Key=key(pk, sk), UpdateExpression="SET " + ", ".join(sets),
                       ExpressionAttributeNames=names, ExpressionAttributeValues=vals(**values))


DEFAULT_TENANT = "mineria"


def _tenant(row: dict) -> str:
    return row.get("tenant_id") or DEFAULT_TENANT


DEMO_SUPPLY = {"part_id": "PRT-04", "tenant_id": "mineria", "on_hand": 0}  # historia de la demo (T37/T43)


def _policy(supply: dict, part_id: str) -> dict:
    row = next((p for p in supply.get("part_policy") or [] if p["part_id"] == part_id), {})
    return {"criticality": row.get("criticality", "C"), "safety_stock": int(row.get("safety_stock", 0))}


def seed(client, ids: dict, with_state: bool = True, supply: dict | None = None, demo_supply: bool = False) -> dict:
    zones = {z["zone_id"]: z for z in ids["zones"]}
    parts = {p["part_id"]: p for p in ids["parts"]}
    supply = supply or {}
    rows = 0
    for s in ids["part_stock"]:
        p = parts[s["part_id"]]
        demo = demo_supply and s["part_id"] == DEMO_SUPPLY["part_id"] and _tenant(s) == DEMO_SUPPLY["tenant_id"]
        for z in (z for z in zones if _tenant(zones[z]) == _tenant(s)):
            rows += 1
            fields = {"part_id": s["part_id"], "zone_id": z, "sku": p["sku"], "name": p["name"],
                      "reorder_point": s["reorder_point"], "tenant_id": _tenant(s), **_policy(supply, s["part_id"])}
            keep = {"on_order": 0, "in_transit": 0}
            if demo:  # el perfil de la demo fija el stock aunque ya exista
                fields |= {"on_hand": DEMO_SUPPLY["on_hand"], "reserved": 0, "available": DEMO_SUPPLY["on_hand"]}
            else:
                keep |= {"on_hand": s["on_hand_per_zone"], "reserved": 0, "available": s["on_hand_per_zone"]}
            _upd(client, REGISTRY, f"STOCK#{s['part_id']}#{z}", "META", fields, keep=keep)
    suppliers = 0
    for sup in supply.get("suppliers") or []:
        suppliers += 1
        _upd(client, REGISTRY, f"SUPPLIER#{sup['supplier_id']}", "META",
             {"supplier_id": sup["supplier_id"], "name": sup["name"]})
    for sp in supply.get("supplier_parts") or []:
        _upd(client, REGISTRY, f"SUPPLIER#{sp['supplier_id']}", f"PART#{sp['part_id']}",
             {"supplier_id": sp["supplier_id"], "part_id": sp["part_id"], "unit_price_usd": sp["unit_price_usd"],
              "lead_time_d": sp["lead_time_d"], "min_qty": sp["min_qty"]})
    if with_state:
        for m in ids["machines"]:
            z = zones[m["zone_id"]]
            _upd(client, STATE, f"MACHINE#{m['machine_id']}", "META",
                 {"machine_id": m["machine_id"], "model": m["model_id"], "serial": m["serial"], "tenant_id": _tenant(m),
                  "GSI1PK": f"ZONE#{m['zone_id']}", "GSI1SK": f"MACHINE#{m['machine_id']}"},
                 keep={"zone_id": m["zone_id"], "lat": z["lat"], "lon": z["lon"], "status": "ok",
                       "hours_total": m["hours_total"]})
        for t in ids["technicians"]:
            z = zones[t["zone_id"]]
            _upd(client, STATE, f"TECH#{t['tech_id']}", "META",
                 {"tech_id": t["tech_id"], "name": t["name"], "specialty": t["specialty"], "zone_id": t["zone_id"],
                  "tenant_id": _tenant(t), "GSI1PK": f"ZONE#{t['zone_id']}", "GSI1SK": f"TECH#{t['tech_id']}"},
                 keep={"lat": z["lat"], "lon": z["lon"], "available": True})
    return {"stock_rows": rows, "suppliers": suppliers,
            "supplier_parts": len(supply.get("supplier_parts") or []), "demo_supply": demo_supply,
            "machines": len(ids["machines"]) if with_state else 0,
            "technicians": len(ids["technicians"]) if with_state else 0}


def counts(client, ids: dict) -> dict:
    stock = sum(1 for s in ids["part_stock"] for z in ids["zones"] if _tenant(z) == _tenant(s)
                and store.get(client, f"STOCK#{s['part_id']}#{z['zone_id']}", "META"))
    machines = sum(len(store.query_all(client, TableName=STATE, IndexName="GSI1",
                                       KeyConditionExpression="GSI1PK = :z AND begins_with(GSI1SK, :m)",
                                       ExpressionAttributeValues=vals(z=f"ZONE#{z['zone_id']}", m="MACHINE#")))
                   for z in ids["zones"])
    return {"machines": machines, "stock_rows": stock}


class DryRun:
    """Cliente de DynamoDB que solo anota lo que se escribiria (para `seed-registry.py --dry-run`)."""

    def __init__(self):
        self.writes: list[dict] = []

    def update_item(self, TableName, Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues):
        fields = {}
        for name, placeholder in re.findall(r"(#[a-z]\d+) = (?:if_not_exists\(#[a-z]\d+, )?(:[a-z]\d+)",
                                            UpdateExpression):
            fields[ExpressionAttributeNames[name]] = store.des({"v": ExpressionAttributeValues[placeholder]})["v"]
        self.writes.append({"table": TableName, "PK": Key["PK"]["S"], "SK": Key["SK"]["S"], **fields})


def stock_of(writes: list[dict], part_id: str) -> dict[str, int]:
    """on_hand que quedaria por zona para un repuesto (resumen del dry-run)."""
    return {w["zone_id"]: w["on_hand"] for w in writes if w["PK"].startswith(f"STOCK#{part_id}#")}
