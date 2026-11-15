"""Tecnicos en fp-state (`TECH#<id>` / `META`): candidatos, ranking (puro, sin I/O) y reserva condicional.

score = peso_severidad x 1/max(km, 0.5) x (2 si la especialidad coincide con el modo de falla) x (1 si disponible).
Idempotencia: el ranking es determinista, `claim` acepta al tecnico ya tomado por el mismo incidente y `release`
solo libera al tecnico que sigue enlazado a ese incidente.
"""
from __future__ import annotations

import math

from boto3.dynamodb.conditions import Key

from fplib import ids

SEV_W = {"observe": 1, "schedule": 2, "immediate": 3}
MIN_KM = 0.5
EARTH_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(a))


def _zone_items(state_table, zone_id: str) -> list[dict]:
    kwargs = {"IndexName": "GSI1",
              "KeyConditionExpression": Key("GSI1PK").eq(f"ZONE#{zone_id}") & Key("GSI1SK").begins_with("TECH#")}
    items = []
    while True:
        page = state_table.query(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def candidates(state_table, zone_hint: str) -> list[dict]:
    """Tecnicos `TECH#`/`META` de fp-state (GSI1 `ZONE#<zona>` por cada zona, empezando por `zone_hint`).

    Query por zona y no Scan: fp-state guarda las lecturas (millones de items), un Scan las recorreria todas.

    Sin tecnicos en la tabla: los de ids.yaml, cada uno en el centroide de su zona y disponible. Un tecnico sin
    lat/lon queda en el centroide de su zona (o de `zone_hint`).
    """
    zones = ids.zones()
    order = ([zone_hint] + [z for z in zones if z != zone_hint]) if zone_hint else list(zones)
    items = [it for z in order for it in _zone_items(state_table, z)]
    if not items:
        items = [{**t, "available": True} for t in ids.technicians()]
    out = []
    for t in items:
        home = zones.get(t.get("zone_id")) or zones.get(zone_hint) or {}
        lat, lon = t.get("lat", home.get("lat")), t.get("lon", home.get("lon"))
        if lat is None or lon is None:
            continue
        out.append({"tech_id": t["tech_id"], "name": t.get("name", t["tech_id"]), "specialty": t.get("specialty"),
                    "lat": float(lat), "lon": float(lon), "available": bool(t.get("available", True))})
    return out


def rank(cands: list[dict], lat: float, lon: float, severity: str, failure_mode: str) -> list[dict]:
    """Mejor primero; empate por especialidad, luego distancia y luego id (orden determinista)."""
    wanted = ids.specialty_for(failure_mode)
    w = SEV_W[severity]
    ranked = []
    for c in cands:
        km = haversine_km(lat, lon, c["lat"], c["lon"])
        match = c["specialty"] == wanted
        score = w * (1 / max(km, MIN_KM)) * (2 if match else 1) * (1 if c["available"] else 0)
        ranked.append({"tech_id": c["tech_id"], "name": c["name"], "distance_km": round(km, 3),
                       "specialty_match": match, "score": round(score, 4)})
    return sorted(ranked, key=lambda r: (-r["score"], not r["specialty_match"], r["distance_km"], r["tech_id"]))


def get(state_table, tech_id: str) -> dict | None:
    """Item `TECH#<id>` / `META` con lectura consistente, o None (tecnico de ids.yaml sin item)."""
    return state_table.get_item(Key={"PK": f"TECH#{tech_id}", "SK": "META"}, ConsistentRead=True).get("Item")


def is_free(item: dict, incident_id: str) -> bool:
    """Disponible, o ya tomado por este mismo incidente (reintento)."""
    return item.get("available", True) is not False or item.get("current_incident") == incident_id


def claim(state_tbl_name: str, tech_id: str, incident_id: str) -> dict:
    """`Update` de TransactWriteItems (tipos nativos) que marca al tecnico ocupado con este incidente.

    La condicion repite `is_free` en el servidor: si otra ejecucion lo tomo despues del ranking, la transaccion se
    cancela y no se pisa su `current_incident`.
    """
    return {"Update": {
        "TableName": state_tbl_name, "Key": {"PK": f"TECH#{tech_id}", "SK": "META"},
        "UpdateExpression": "SET available = :f, current_incident = :i",
        "ConditionExpression": "attribute_exists(PK) AND (attribute_not_exists(available) OR available = :t "
                               "OR current_incident = :i)",
        "ExpressionAttributeValues": {":f": False, ":t": True, ":i": incident_id},
    }}


def release(state_tbl_name: str, tech_id: str, incident_id: str) -> dict:
    """`Update` de TransactWriteItems (tipos nativos) que deja al tecnico libre al cerrar `incident_id`.

    Condicionado a que siga enlazado a ese incidente: un tecnico asignado ocupado (`tech_busy`) sigue con el suyo.
    """
    return {"Update": {
        "TableName": state_tbl_name, "Key": {"PK": f"TECH#{tech_id}", "SK": "META"},
        "UpdateExpression": "SET available = :t, current_incident = :n",
        "ConditionExpression": "current_incident = :i",
        "ExpressionAttributeValues": {":t": True, ":n": None, ":i": incident_id},
    }}
