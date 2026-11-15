"""Lambda `correlate_zone` (CorrelateZone): agrupa el incidente con otras maquinas anomalas de su celda y junta el
contexto de la zona para ExplainCause.

Celda: items `CELL#<geohash6>` / `A#<ts>#<machine_id>` de fp-state (los escribe aws/scoring con severidad >=
observe) de la ultima hora. Si hay al menos otra maquina (2 o mas con esta) -> `group_id` =
`GRP-<geohash6>-<hora UTC>` (fplib.ids.group_id), que CreateIncident guarda y publica en incident.opened. La hora
es la de la anomalia mas antigua de la celda en la ventana, no la de ahora: asi las maquinas de un mismo episodio
que abren incidente a ambos lados de hh:00 quedan en el mismo grupo (S2).
Contexto: items `CTX#<zone_id>` / `<ts>#<context_id>` de la ultima hora (POST /context, fuel.refill.schema.json):
lotes de combustible distintos y lecturas de clima. Zona vacia (META escasa) -> la de la maquina en ids.yaml.
Idempotencia: solo lee fp-state.
"""
from __future__ import annotations

from datetime import timedelta

from boto3.dynamodb.conditions import Key

from fplib import clock, geohash, ids, log
from lib import registry as fp_registry

WINDOW = timedelta(hours=1)


def _context(state, zone_id: str | None, since: str) -> dict:
    if not zone_id:
        return {"fuel_lots": [], "weather": []}
    items = list(fp_registry.query_all(state, KeyConditionExpression=Key("PK").eq(f"CTX#{zone_id}")
                                       & Key("SK").gt(since)))
    lots = sorted({i["fuel_lot"] for i in items if i.get("type") == "fuel.refill" and i.get("fuel_lot")})
    weather = [{"ts": i["ts"], "condition": i["condition"], "intensity": fp_registry.from_ddb(i.get("intensity"))}
               for i in items if i.get("type") == "weather"]
    return {"fuel_lots": lots, "weather": weather}


def handler(event, context=None, state=None):
    state = state or fp_registry.table_from_env("STATE_TABLE")
    machine_id = event["machine_id"]
    ctx = {"incident_id": event["incident_id"], "machine_id": machine_id}
    now = clock.now()
    since = clock.iso(now - WINDOW)
    gh = event.get("geohash6") or geohash.encode(float(event["lat"]), float(event["lon"]), 6)
    cell = list(fp_registry.query_all(state, KeyConditionExpression=Key("PK").eq(f"CELL#{gh}")
                                      & Key("SK").gt(f"A#{since}")))
    peers = sorted({i["machine_id"] for i in cell if i["machine_id"] != machine_id})
    first_ts = min((i["SK"].split("#")[1] for i in cell), default=None)  # ISO UTC: orden de texto = de tiempo
    first = clock.parse(first_ts[:19] + "Z") if first_ts else now
    group_id = ids.group_id(gh, first) if peers else None
    zone_id = event.get("zone_id") or ids.machines().get(machine_id, {}).get("zone_id")
    out = {"group_id": group_id, "peers": peers, "context": _context(state, zone_id, since)}
    log.event("zona_correlacionada", group_id=group_id, peers=peers, geohash6=gh, zone_id=zone_id,
              fuel_lots=out["context"]["fuel_lots"], **ctx)
    return out
