"""Seed de fp-registry (y catalogo estatico en fp-state) desde contracts/ids.yaml. Idempotente: UpdateItem por
clave; nunca pisa campos dinamicos (severidad, incidente abierto, stock reservado, estado de ingest).
"""
from __future__ import annotations

from reg import store
from reg.store import REGISTRY, STATE, key, vals


def _upd(client, table: str, pk: str, sk: str, fields: dict, keep: dict | None = None) -> None:
    """SET fields; keep = valores iniciales que solo se escriben si el atributo no existe."""
    sets, values, names = [], {}, {}
    for i, (k, v) in enumerate({**fields}.items()):
        names[f"#f{i}"] = k
        values[f"f{i}"] = v
        sets.append(f"#f{i} = :f{i}")
    for i, (k, v) in enumerate((keep or {}).items()):
        names[f"#k{i}"] = k
        values[f"k{i}"] = v
        sets.append(f"#k{i} = if_not_exists(#k{i}, :k{i})")
    client.update_item(TableName=table, Key=key(pk, sk), UpdateExpression="SET " + ", ".join(sets),
                       ExpressionAttributeNames=names, ExpressionAttributeValues=vals(**values))


def seed(client, ids: dict, with_state: bool = True) -> dict:
    zones = {z["zone_id"]: z for z in ids["zones"]}
    parts = {p["part_id"]: p for p in ids["parts"]}
    n = 0
    for z in ids["zones"]:
        _upd(client, REGISTRY, f"ZONE#{z['zone_id']}", "META",
             {"name": z["name"], "lat": z["lat"], "lon": z["lon"], "altitude_m": z["altitude_m"]})
        n += 1
    for p in ids["parts"]:
        _upd(client, REGISTRY, f"PART#{p['part_id']}", "META",
             {"sku": p["sku"], "name": p["name"], "unit_cost_usd": p["unit_cost_usd"]})
        n += 1
    for t in ids["technicians"]:
        z = zones[t["zone_id"]]
        fields = {"name": t["name"], "specialty": t["specialty"], "zone_id": t["zone_id"],
                  "lat": z["lat"], "lon": z["lon"]}
        _upd(client, REGISTRY, f"TECH#{t['tech_id']}", "META", fields, keep={"available": True})
        if with_state:
            # ASUNCION: fp-state TECH#<id>/META con GSI1PK/GSI1SK = ZONE#<zona>/TECH#<id> (documento de diseno)
            _upd(client, STATE, f"TECH#{t['tech_id']}", "META",
                 {"specialties": [t["specialty"]], "zone": t["zone_id"], "lat": z["lat"], "lon": z["lon"],
                  "GSI1PK": f"ZONE#{t['zone_id']}", "GSI1SK": f"TECH#{t['tech_id']}"},
                 keep={"available": True})
        n += 1
    for b in ids["bom"]:
        _upd(client, REGISTRY, f"BOM#{b['model_id']}", f"{b['failure_mode']}#{b['part_id']}",
             {"qty": b["qty"], "replace_every_h": b["replace_every_h"]})
        n += 1
    for s in ids["part_stock"]:
        p = parts[s["part_id"]]
        for z in ids["zones"]:
            _upd(client, REGISTRY, "STOCK", f"{s['part_id']}#{z['zone_id']}",
                 {"sku": p["sku"], "name": p["name"], "reorder_point": s["reorder_point"]},
                 keep={"on_hand": s["on_hand_per_zone"], "reserved": 0, "available": s["on_hand_per_zone"]})
            n += 1
    for m in ids["machines"]:
        z = zones[m["zone_id"]]
        _upd(client, REGISTRY, f"MACHINE#{m['machine_id']}", "META",
             {"model": m["model_id"], "serial": m["serial"], "zone_id": m["zone_id"],
              "commissioned_at": m["commissioned_at"], "lat": z["lat"], "lon": z["lon"],
              "hours_total": m["hours_total"], "GSI1PK": "MACHINES", "GSI1SK": f"{m['zone_id']}#{m['machine_id']}"},
             keep={"severity": "ok"})
        if with_state:
            _upd(client, STATE, f"MACHINE#{m['machine_id']}", "META",
                 {"model": m["model_id"], "serial": m["serial"],
                  "GSI1PK": f"ZONE#{m['zone_id']}", "GSI1SK": f"MACHINE#{m['machine_id']}"},
                 keep={"zone": m["zone_id"], "lat": z["lat"], "lon": z["lon"], "status": "off",
                       "hours_total": m["hours_total"], "hours_since_service": m["hours_since_service"]})
        n += 1
    return {"items": n, "machines": len(ids["machines"]), "stock_rows": len(ids["part_stock"]) * len(zones)}


def counts(client) -> dict:
    machines = store.query_all(client, TableName=REGISTRY, IndexName="GSI1",
                               KeyConditionExpression="GSI1PK = :p", ExpressionAttributeValues=vals(p="MACHINES"))
    stock = store.query_all(client, TableName=REGISTRY, KeyConditionExpression="PK = :p",
                            ExpressionAttributeValues=vals(p="STOCK"))
    return {"machines": len(machines), "stock_rows": len(stock)}
