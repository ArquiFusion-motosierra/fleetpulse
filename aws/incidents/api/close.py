"""Lambda `close`, dos rutas en la HTTP API (payload v2) con el mismo cierre:
- `POST /incidents/{incident_id}/close` con `x-api-key`: la consola sin login (T15) con `fleetpulse/api/console-key`;
  el simulador o un script (smoke) con `fleetpulse/api/ingest-key`. `closed_by` va en el cuerpo.
- `POST /me/incidents/{incident_id}/close` (`USER_ROUTE`) detras del authorizer JWT `cognito`: la consola por roles
  (T34) desde su BFF con el ID token del usuario. API Gateway ya valido el token; aqui se leen las claims
  (fplib.authz): pueden cerrar `supervisor`, `admin` y el `technician` cuyo `custom:tech_id` es el `tech_id` del
  incidente (otro tecnico, un incidente sin tecnico o cualquier otro rol: 403, log `cierre_prohibido`; sin claims:
  401). `closed_by` sale del token (email, o sub si no hay email, cortado a 64) y el del cuerpo se ignora. HTTP API
  admite un solo authorizer por ruta, por eso la ruta con login es otra y la de API key sigue igual.

Cuerpo: `{"closed_by", "parts_used": [{part_id, zone_id, qty}], "cause_summary"}` (`closed_by` obligatorio en la
ruta con API key, `parts_used` por defecto [], `cause_summary` por defecto ""; en `USER_ROUTE` el cuerpo puede
faltar). Respuestas: 401 sin una de las dos API keys (log `cierre_no_autorizado` con el motivo por secreto, nunca
la llave), 403 en USER_ROUTE segun el rol, 404 incidente desconocido (log
`cierre_incidente_inexistente`), 409 ya cerrado, 400 cuerpo invalido (incluye `incident.closed` fuera de
contrato), 500 si el tecnico cambio de incidente durante el cierre (no se escribe nada; reintentar), 200
`{"incident_id", "downtime_h"}`.
Efectos en una sola transaccion: META (`status` closed, `closed_at`, `downtime_h` = horas desde `opened_at`
redondeadas a 2, `closed_by`, `cause_summary`, `parts_used`, `GSI2PK` STATUS#closed; la recurrencia de
CreateIncident filtra por `closed_at`) + EV# + `incident.closed` en fp-outbox (lo consume `registry` para descontar
stock) + `TECH#<id>` libre (`available` true, `current_incident` null) si el tecnico sigue enlazado a este
incidente; un tecnico asignado ocupado (`tech_busy`) sigue con el suyo.
Despues del cierre se detiene la ejecucion `incident-flow` de nombre `<incident_id>` (`EXECUTION_ARN_PREFIX`),
por si sigue esperando la aprobacion: `ExecutionDoesNotExist` se ignora y cualquier otro error solo se registra (el
cierre ya quedo escrito; AssignTechnician y ReserveParts exigen `status = open` igual).
Idempotencia: la transaccion exige `status = open`; un segundo cierre responde 409 sin escribir nada.
"""
from __future__ import annotations

import base64
import json
import os
import re

from botocore.config import Config

from fplib import auth, authz, clock, envelope, log
from fplib.schema import ContractError
from lib import registry as fp_registry
from lib import technicians

KEY_SECRETS = ("fleetpulse/api/console-key", "fleetpulse/api/ingest-key")
USER_ROUTE = "POST /me/incidents/{incident_id}/close"
USER_ROLES = ("technician", "supervisor", "admin")
MAX_CLOSED_BY = 64  # incident.closed: closed_by maxLength
INC_RE = re.compile(r"^INC-(?:EX|CF|VQ|MN|TO)-[0-9]{3}-[0-9]{8}T[0-9]{4}$")
AWS_CONFIG = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 3})
_sfn = None


def _client():
    global _sfn
    if _sfn is None:
        import boto3
        _sfn = boto3.client("stepfunctions", config=AWS_CONFIG)
    return _sfn


def _resp(code: int, body: dict) -> dict:
    return {"statusCode": code, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def _body(event: dict, closed_by: str | None = None) -> dict:
    """`closed_by`: el del token en USER_ROUTE (reemplaza al del cuerpo, que entonces puede faltar)."""
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    body = json.loads(raw or "{}")
    if not isinstance(body, dict):
        raise ValueError("el cuerpo no es un objeto")
    closed_by, cause = closed_by or body.get("closed_by"), body.get("cause_summary") or ""
    if not isinstance(closed_by, str) or not closed_by.strip() or not isinstance(cause, str):
        raise ValueError("closed_by (texto) es obligatorio y cause_summary debe ser texto")
    return {"closed_by": closed_by.strip(), "parts_used": body.get("parts_used") or [], "cause_summary": cause}


def _release(state, meta: dict, incident_id: str) -> dict | None:
    tech_id = meta.get("tech_id")
    item = technicians.get(state, tech_id) if tech_id else None
    if item is None or item.get("current_incident") != incident_id:
        return None
    return technicians.release(state.name, tech_id, incident_id)


def _stop_execution(sfn, incident_id: str, ctx: dict) -> None:
    arn = f"{os.environ['EXECUTION_ARN_PREFIX']}:{incident_id}"
    try:
        sfn.stop_execution(executionArn=arn, error="IncidentClosed", cause="closed from console")
    except sfn.exceptions.ExecutionDoesNotExist:
        log.event("ejecucion_inexistente", **ctx)
        return
    except Exception as exc:  # el cierre ya esta escrito: no se le devuelve un error a la consola
        log.error("stop_execution_fallo", error=type(exc).__name__, **ctx)
        return
    log.event("ejecucion_detenida", **ctx)


def _unauthorized(headers: dict, secrets) -> dict[str, str] | None:
    """None si alguna de las llaves sirve; si no, el motivo por secreto (sin valores). Prueba en orden y para en la
    primera valida, asi que la ingest-key no deja un `mismatch` falso de la console-key."""
    reasons = {}
    for name in KEY_SECRETS:
        reason = auth.rejection(headers, name, client=secrets)
        if reason is None:
            return None
        reasons[name] = reason
    return reasons


def _forbidden(event: dict, incident_id: str, why: str) -> dict:
    log.event("cierre_prohibido", status=403, incident_id=incident_id[:64], role=authz.role_of(event),
              tech_id=authz.tech_id_of(event), reason=why)
    return _resp(403, {"error": "forbidden", "detail": why})


def handler(event, context=None, state=None, registry=None, outbox=None, secrets=None, sfn=None):
    incident_id = (event.get("pathParameters") or {}).get("incident_id") or ""
    user = role = None
    if event.get("routeKey") == USER_ROUTE:
        user = authz.user_of(event)[:MAX_CLOSED_BY]
        if not user:  # el authorizer no dejaria pasar una ruta sin token; por si la ruta se publica sin el
            log.error("cierre_no_autorizado", status=401, incident_id=incident_id[:64],
                      reasons={"jwt": "missing_claims"})
            return _resp(401, {"error": "unauthorized"})
        try:
            role = authz.require_role(event, *USER_ROLES)
        except authz.Forbidden as exc:
            return _forbidden(event, incident_id, str(exc))
    else:
        reasons = _unauthorized(event.get("headers") or {}, secrets)
        if reasons:
            log.error("cierre_no_autorizado", status=401, incident_id=incident_id[:64], reasons=reasons)
            return _resp(401, {"error": "unauthorized"})
    registry = registry or fp_registry.table_from_env("REGISTRY_TABLE")
    meta = fp_registry.get_incident(registry, incident_id) if INC_RE.match(incident_id) else None
    if meta is None:
        log.event("cierre_incidente_inexistente", status=404, incident_id=incident_id[:64])
        return _resp(404, {"error": "incident not found", "incident_id": incident_id})
    ctx = {"incident_id": incident_id, "machine_id": meta.get("machine_id")}
    if role == "technician" and (not authz.tech_id_of(event) or authz.tech_id_of(event) != meta.get("tech_id")):
        return _forbidden(event, incident_id, "el incidente no esta asignado a este tecnico")
    if meta.get("status") == "closed":
        return _resp(409, {"error": "incident already closed", "closed_at": meta.get("closed_at")})
    try:
        body = _body(event, closed_by=user)
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
        if exc.index != 0:  # el tecnico cambio de incidente entre la lectura y la escritura: no se escribio nada
            log.error("cierre_tecnico_cambio", tech_id=meta.get("tech_id"), index=exc.index, **ctx)
            return _resp(500, {"error": "technician changed during close, retry"})
        log.event("cierre_ya_aplicado", **ctx)
        return _resp(409, {"error": "incident already closed"})
    _stop_execution(sfn or _client(), incident_id, ctx)
    log.event("incidente_cerrado", downtime_h=payload["downtime_h"], closed_by=body["closed_by"], role=role,
              tech_id=meta.get("tech_id"), tech_released=release is not None, event_id=env["event_id"], **ctx)
    return _resp(200, {"incident_id": incident_id, "downtime_h": payload["downtime_h"]})
