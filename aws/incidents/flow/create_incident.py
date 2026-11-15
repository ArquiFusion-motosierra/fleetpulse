"""Lambda `create_incident` (CreateIncident): abre el incidente en fp-registry y emite `incident.opened`.

META + EV# + fp-outbox van en una sola `TransactWriteItems`; el META lleva `attribute_not_exists(PK)`.
Idempotencia: el incident_id es el nombre de la ejecucion, asi que un reintento encuentra el META, la transaccion
se cancela sin escribir nada y se devuelve el incidente ya creado.
`zone_id` puede llegar vacio (META escasa en fp-state): se toma la zona de la maquina en ids.yaml o, si no esta,
la zona cuyo centroide queda mas cerca.
`deviated_vars` puede llegar vacio (score >= 2.0 con todas las |z| < 1.0), pero `incident.opened` exige al menos
una. Entonces se deriva una sola entrada del mapa `z` de la entrada: la variable con mayor |z| dominante (la de
mayor modulo entre self y peer, igual que en aws/scoring). La entrada no trae medias, asi que `value` lleva la misma
z dominante como marcador y se registra `deviated_vars_derived`. Sin `z` utilizable se registra y se lanza
ValueError, para que la alerta de fp-alerts diga por que.
"""
from __future__ import annotations

from datetime import timedelta

from fplib import clock, envelope, ids, log
from lib import registry as fp_registry
from lib.technicians import haversine_km

RECURRENCE_WINDOW = timedelta(days=7)
RESULT_KEYS = ("incident_id", "opened_at", "recurrence_flag", "group_id", "zone_id")


def _zone(zone_id: str | None, machine_id: str, lat: float, lon: float) -> str:
    zones = ids.zones()
    if zone_id in zones:
        return zone_id
    known = ids.machines().get(machine_id, {}).get("zone_id")
    if known in zones:
        return known
    return min(zones, key=lambda z: haversine_km(lat, lon, zones[z]["lat"], zones[z]["lon"]))


def _deviated(event: dict, ctx: dict) -> list[dict]:
    given = event.get("deviated_vars") or []
    if given:
        return [{"var": d["var"], "value": d["value"], "z": d["z"]} for d in given]
    dominant = {}
    for var, zz in (event.get("z") or {}).items():
        vals = [x for x in ((zz or {}).get("self"), (zz or {}).get("peer")) if x is not None]
        if vals:
            dominant[var] = max(vals, key=abs)
    if not dominant:
        log.error("sin_deviated_vars", **ctx)
        raise ValueError(f"{ctx['incident_id']}: deviated_vars vacio y sin z para derivarlo")
    var = max(dominant, key=lambda v: abs(dominant[v]))
    log.event("deviated_vars_derived", var=var, z=dominant[var], **ctx)
    return [{"var": var, "value": dominant[var], "z": dominant[var]}]


def _result(item: dict) -> dict:
    return {k: fp_registry.from_ddb(item.get(k)) for k in RESULT_KEYS}


def handler(event, context=None, registry=None, outbox=None):
    registry = registry or fp_registry.table_from_env("REGISTRY_TABLE")
    outbox = outbox or fp_registry.table_from_env("OUTBOX_TABLE")
    incident_id, machine_id = event["incident_id"], event["machine_id"]
    ctx = {"incident_id": incident_id, "machine_id": machine_id}
    now = clock.now()
    opened_at = clock.iso(now)
    lat, lon = float(event["lat"]), float(event["lon"])
    zone = _zone(event.get("zone_id"), machine_id, lat, lon)
    severity, mode = event["classified"]["severity"], event["failure_mode"]
    deviated = _deviated(event, ctx)
    group_id = (event.get("correlation") or {}).get("group_id")
    recurrence = fp_registry.recent_closed_same_mode(registry, machine_id, mode, now - RECURRENCE_WINDOW)
    payload = {
        "zone_id": zone, "severity": severity, "score": float(event["score"]), "failure_mode": mode,
        "deviated_vars": deviated,
        "group_id": group_id, "recurrence_flag": recurrence, "opened_at": opened_at, "lat": lat, "lon": lon,
    }
    env = envelope.build("incident.opened", "aws.incident-flow", "incident", incident_id, payload,
                         incident_id=incident_id, machine_id=machine_id, occurred_at=now)
    item = {
        **fp_registry.meta_key(incident_id), "incident_id": incident_id, "machine_id": machine_id,
        "model": event.get("model"), "zone_id": zone, "lat": lat, "lon": lon, "status": "open",
        "severity": severity, "score": payload["score"], "failure_mode": mode, "opened_at": opened_at,
        "group_id": group_id, "recurrence_flag": recurrence,
        "GSI1PK": f"MACHINE#{machine_id}", "GSI1SK": f"INC#{opened_at}", "GSI2PK": "STATUS#open", "GSI2SK": opened_at,
    }
    try:
        fp_registry.put_incident_tx(registry.name, outbox.name, item, env, fp_registry.event_item(env),
                                    registry.meta.client)
    except fp_registry.ConditionFailed:
        log.event("incidente_ya_creado", **ctx)
        return _result(fp_registry.get_incident(registry, incident_id))
    log.event("incidente_creado", severity=severity, zone_id=zone, recurrence_flag=recurrence, group_id=group_id,
              event_id=env["event_id"], **ctx)
    return _result(item)
