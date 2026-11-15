"""Lambda `close`: `POST /incidents/{incident_id}/close` (HTTP API payload v2). La llama la consola (T15) con
`fleetpulse/api/console-key`; el simulador o un script pueden usar `fleetpulse/api/ingest-key`.

Cuerpo: `{"closed_by", "parts_used": [{part_id, zone_id, qty}], "cause_summary"}` (`closed_by` obligatorio,
`parts_used` por defecto [], `cause_summary` por defecto ""). Respuestas: 401 sin una de las dos API keys, 404
incidente desconocido, 409 ya cerrado, 400 cuerpo invalido (incluye `incident.closed` fuera de contrato), 200
`{"incident_id", "downtime_h"}`.
Efectos en una sola transaccion: META (`status` closed, `closed_at`, `downtime_h` = horas desde `opened_at`
redondeadas a 2, `closed_by`, `cause_summary`, `parts_used`, `GSI2PK` STATUS#closed; la recurrencia de
CreateIncident filtra por `closed_at`) + EV# + `incident.closed` en fp-outbox (lo consume `registry` para descontar
stock) + `TECH#<id>` libre (`available` true, `current_incident` null) si el tecnico sigue enlazado a este
incidente; un tecnico asignado ocupado (`tech_busy`) sigue con el suyo.
Idempotencia: la transaccion exige `status = open`; un segundo cierre responde 409 sin escribir nada.
"""
from __future__ import annotations

import base64
import json
import re

from fplib import auth, clock, envelope, log
from fplib.schema import ContractError
from lib import registry as fp_registry
from lib import technicians

KEY_SECRETS = ("fleetpulse/api/console-key", "fleetpulse/api/ingest-key")
INC_RE = re.compile(r"^INC-(?:EX|CF|VQ|MN|TO)-[0-9]{3}-[0-9]{8}T[0-9]{4}$")


def _resp(code: int, body: dict) -> dict:
    return {"statusCode": code, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def _body(event: dict) -> dict:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise ValueError("el cuerpo no es un objeto")
    closed_by, cause = body.get("closed_by"), body.get("cause_summary") or ""
    if not isinstance(closed_by, str) or not closed_by.strip() or not isinstance(cause, str):
        raise ValueError("closed_by (texto) es obligatorio y cause_summary debe ser texto")
    return {"closed_by": closed_by.strip(), "parts_used": body.get("parts_used") or [], "cause_summary": cause}


def _release(state, meta: dict, incident_id: str) -> dict | None:
    tech_id = meta.get("tech_id")
    item = technicians.get(state, tech_id) if tech_id else None
    if item is None or item.get("current_incident") != incident_id:
        return None
    return technicians.release(state.name, tech_id, incident_id)


def handler(event, context=None, state=None, registry=None, outbox=None, secrets=None):
    incident_id = (event.get("pathParameters") or {}).get("incident_id") or ""
    headers = event.get("headers") or {}
    if not any(auth.check_api_key(headers, name, client=secrets) for name in KEY_SECRETS):
        return _resp(401, {"error": "unauthorized"})
    registry = registry or fp_registry.table_from_env("REGISTRY_TABLE")
    meta = fp_registry.get_incident(registry, incident_id) if INC_RE.match(incident_id) else None
    if meta is None:
        return _resp(404, {"error": "incident not found", "incident_id": incident_id})
    ctx = {"incident_id": incident_id, "machine_id": meta.get("machine_id")}
    if meta.get("status") == "closed":
        return _resp(409, {"error": "incident already closed", "closed_at": meta.get("closed_at")})
    try:
        body = _body(event)
    except ValueError as exc:  # JSON invalido incluido
        log.event("cierre_cuerpo_invalido", error=str(exc)[:200], **ctx)
        return _resp(400, {"error": "invalid body: closed_by, parts_used, cause_summary"})
    now = clock.now()
    closed_at = clock.iso(now)
    hours = (now - clock.parse(meta["opened_at"])).total_seconds() / 3600
    payload = {"closed_at": closed_at, "downtime_h": round(max(hours, 0.0), 2), **body}
    try:
        env = envelope.build("incident.closed", "aws.incidents-api", "incident", incident_id, payload,
                             incident_id=incident_id, machine_id=meta["machine_id"], occurred_at=now)
    except ContractError as exc:
        log.event("cierre_fuera_de_contrato", error=exc.message, path=exc.path, **ctx)
        return _resp(400, {"error": exc.message, "path": exc.path})
    state = state or fp_registry.table_from_env("STATE_TABLE")
    outbox = outbox or fp_registry.table_from_env("OUTBOX_TABLE")
    release = _release(state, meta, incident_id)
    try:
        fp_registry.update_incident(registry, incident_id, envelope=env, outbox_tbl_name=outbox.name,
                                    expect={"status": "open"}, extra_items=[release] if release else None,
                                    status="closed", GSI2PK="STATUS#closed", **payload)
    except fp_registry.ConditionFailed as exc:
        if exc.index != 0:  # 3 = el tecnico cambio de incidente entre la lectura y la escritura (500)
            raise
        log.event("cierre_ya_aplicado", **ctx)
        return _resp(409, {"error": "incident already closed"})
    log.event("incidente_cerrado", downtime_h=payload["downtime_h"], closed_by=body["closed_by"],
              tech_id=meta.get("tech_id"), tech_released=release is not None, event_id=env["event_id"], **ctx)
    return _resp(200, {"incident_id": incident_id, "downtime_h": payload["downtime_h"]})
