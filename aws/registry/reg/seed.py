"""Seed desde contracts/ids.yaml segun el modelo de docs/superpowers/plans/2026-11-14-aws-core-pipeline.md:
- fp-registry: STOCK#<part>#<zone>/META (on_hand, reserved, available, reorder_point, sku, name).
- fp-state: MACHINE#<id>/META y TECH#<id>/META (GSI1 ZONE#<zona>).
Idempotente: UpdateItem por clave; nunca pisa lo dinamico (stock reservado, severidad, posicion de ingest, tecnico ocupado).
"""
from __future__ import annotations

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


def seed(client, ids: dict, with_state: bool = True) -> dict:
    zones = {z["zone_id"]: z for z in ids["zones"]}
    parts = {p["part_id"]: p for p in ids["parts"]}
    for s in ids["part_stock"]:
        p = parts[s["part_id"]]
        for z in zones:
            _upd(client, REGISTRY, f"STOCK#{s['part_id']}#{z}", "META",
                 {"part_id": s["part_id"], "zone_id": z, "sku": p["sku"], "name": p["name"],
                  "reorder_point": s["reorder_point"]},
                 keep={"on_hand": s["on_hand_per_zone"], "reserved": 0, "available": s["on_hand_per_zone"]})
    if with_state:
        for m in ids["machines"]:
            z = zones[m["zone_id"]]
            _upd(client, STATE, f"MACHINE#{m['machine_id']}", "META",
                 {"machine_id": m["machine_id"], "model": m["model_id"], "serial": m["serial"],
                  "GSI1PK": f"ZONE#{m['zone_id']}", "GSI1SK": f"MACHINE#{m['machine_id']}"},
                 keep={"zone_id": m["zone_id"], "lat": z["lat"], "lon": z["lon"], "status": "ok",
                       "hours_total": m["hours_total"]})
        for t in ids["technicians"]:
            z = zones[t["zone_id"]]
            _upd(client, STATE, f"TECH#{t['tech_id']}", "META",
                 {"tech_id": t["tech_id"], "name": t["name"], "specialty": t["specialty"], "zone_id": t["zone_id"],
                  "GSI1PK": f"ZONE#{t['zone_id']}", "GSI1SK": f"TECH#{t['tech_id']}"},
                 keep={"lat": z["lat"], "lon": z["lon"], "available": True})
    return {"stock_rows": len(ids["part_stock"]) * len(zones),
            "machines": len(ids["machines"]) if with_state else 0,
            "technicians": len(ids["technicians"]) if with_state else 0}


def counts(client, ids: dict) -> dict:
    stock = sum(1 for p in ids["parts"] for z in ids["zones"]
                if store.get(client, f"STOCK#{p['part_id']}#{z['zone_id']}", "META"))
    machines = sum(len(store.query_all(client, TableName=STATE, IndexName="GSI1",
                                       KeyConditionExpression="GSI1PK = :z AND begins_with(GSI1SK, :m)",
                                       ExpressionAttributeValues=vals(z=f"ZONE#{z['zone_id']}", m="MACHINE#")))
                   for z in ids["zones"])
    return {"machines": machines, "stock_rows": stock}
