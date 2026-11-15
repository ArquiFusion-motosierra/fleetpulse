"""Lambda `reports`: reportes de anormalidad del operario en la HTTP API fp-api (payload v2, autorizador JWT
`cognito`; el rol llega en la claim `custom:role`, ver fplib.authz).

Rutas:
- `POST /reports` (operator, supervisor, admin): cuerpo `contracts/report.schema.json#/$defs/create`. Valida la
  maquina (ids.yaml) y el tipo (catalogo `CATALOG#abnormality_type`, sembrado al primer uso), arma el snapshot de
  telemetria (lib.snapshot), pide los chequeos a `/recommend-checks` o usa la plantilla (lib.recommend), vincula el
  incidente abierto o inicia `incident-flow` (lib.link_incident) y escribe META + EV# + `report.created` (y el EV#
  `report.linked` del incidente) en una transaccion. 201 `{report}`; 400 cuerpo, maquina o tipo invalidos; 409 si
  ya existe un reporte de la maquina en ese segundo.
- `GET /reports?status=&machine_id=&incident_id=&mine=1&limit=` (todos los roles): `mine` filtra por
  `reported_by == usuario` o `assigned_tech_id == custom:tech_id`; un operator solo ve los suyos. 200 `{reports}`.
- `GET /reports/{report_id}` (todos los roles; un operator solo el suyo, si no 403): 200 `{report, timeline}`.
- `POST /reports/{report_id}/close` (technician asignado o sin asignar, supervisor, admin): cuerpo `$defs/close`.
  META cerrado + EV# + `report.closed` y, si el incidente vinculado sigue abierto, el mismo sobre como EV# del
  incidente, en una transaccion. 200 `{report}`; 409 si ya estaba cerrado.
- `GET /abnormality-types` (todos los roles): el catalogo, para el formulario del operario. Lo edita el admin con
  la Lambda `admin` (api/admin.py, T35), asi que sus altas y bajas se ven aqui y en la validacion de `POST /reports`.
Tecnico asignado: lectura perezosa. Si el reporte esta `open`, tiene `incident_id` y aun no tiene tecnico, al leerlo
o cerrarlo se copia el `tech_id` del incidente (si incident-flow ya asigno) y pasa a `in_review`.
Idempotencia: crear exige que el META no exista y cerrar que no este cerrado (condiciones en la transaccion); el
inicio de `incident-flow` usa el incident_id como nombre. Cada log lleva report_id, machine_id e incident_id.
"""
from __future__ import annotations

import re
from datetime import timezone
from types import SimpleNamespace

from fplib import authz, clock, envelope, ids, log, schema
from lib import link_incident, recommend, snapshot, store, web

ROLES = ("operator", "technician", "supervisor", "management", "admin")
CREATE_ROLES = ("operator", "supervisor", "admin")
CLOSE_ROLES = ("technician", "supervisor", "admin")
RPT_RE = re.compile(r"^RPT-(?:EX|CF|VQ|MN|TO)-[0-9]{3}-[0-9]{8}T[0-9]{6}$")
MACHINE_RE = re.compile(r"^(?:EX|CF|VQ|MN|TO)-[0-9]{3}$")
INC_RE = re.compile(r"^INC-((?:EX|CF|VQ|MN|TO)-[0-9]{3})-[0-9]{8}T[0-9]{4}$")
MAX_USER = 128


def report_id_for(machine_id: str, now) -> str:
    return f"RPT-{machine_id}-{now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S')}"


def _validate(ref: str, obj: dict) -> None:
    try:
        schema.validate("report", obj, ref=ref)
    except schema.ContractError as exc:
        raise web.HttpError(400, {"error": exc.message, "path": exc.path}) from None


def _user(event: dict) -> str:
    user = authz.user_of(event)[:MAX_USER]
    if not user:
        raise web.HttpError(401, {"error": "unauthorized"})
    return user


def _ctx(item: dict) -> dict:
    return {"report_id": item.get("report_id"), "machine_id": item.get("machine_id"),
            "incident_id": item.get("incident_id")}


def resolve_tech(registry, item: dict, cache: dict) -> dict:
    """Copia al reporte el tecnico del incidente vinculado (ver docstring del modulo); `cache` evita leer dos veces
    el mismo incidente en un listado."""
    if item.get("assigned_tech_id") or item.get("status") != "open" or not item.get("incident_id"):
        return item
    inc_id = item["incident_id"]
    if inc_id not in cache:
        cache[inc_id] = store.get_incident(registry, inc_id) or {}
    tech = cache[inc_id].get("tech_id")
    if not tech:
        return item
    if store.set_assigned(registry, item["report_id"], tech):
        log.event("tecnico_copiado_al_reporte", tech_id=tech, **_ctx(item))
        return {**item, "assigned_tech_id": tech, "status": "in_review", "GSI2PK": "RSTATUS#in_review"}
    return store.get_report(registry, item["report_id"]) or item


def _catalog(registry, now) -> dict[str, dict]:
    return {t["code"]: t for t in store.catalog(registry, recommend.seed_catalog(), clock.iso(now))}


# --- rutas ----------------------------------------------------------------------------------------------------------

def create(event: dict, deps) -> dict:
    authz.require_role(event, *CREATE_ROLES)
    user = _user(event)
    body = web.body(event)
    _validate("create", body)
    machine_id, kind = body["machine_id"], body["abnormality_type"]
    if machine_id not in ids.machines():
        raise web.HttpError(400, {"error": "unknown machine_id", "machine_id": machine_id})
    now = clock.now()
    reported_at = clock.iso(now)
    types = _catalog(deps.registry, now)
    if kind not in types:
        raise web.HttpError(400, {"error": "unknown abnormality_type", "allowed": sorted(types)})
    report_id = report_id_for(machine_id, now)
    ctx = {"report_id": report_id, "machine_id": machine_id}
    snap = snapshot.build(deps.state, machine_id)
    open_inc = store.open_incident_for(deps.registry, machine_id)
    request = {
        "report_id": report_id, "machine_id": machine_id, "model": snap["model"], "abnormality_type": kind,
        "description": body["description"], "severity_perceived": body["severity_perceived"],
        "telemetry_snapshot": {k: snap[k] for k in ("means", "fault_codes", "hours_total", "hours_since_service")},
        "oem_specs": snap["oem"],
    }
    if open_inc:
        request["open_incident"] = {k: open_inc.get(k) for k in ("incident_id", "failure_mode", "severity")}
    rec = recommend.recommend(request, secrets=deps.secrets, http=deps.http, now=now,
                              default_checks=types[kind].get("default_checks"))
    draft = {"report_id": report_id, "machine_id": machine_id, "abnormality_type": kind,
             "description": body["description"], "severity_perceived": body["severity_perceived"],
             "reported_by": user, "reported_at": reported_at}
    link = link_incident.link_or_start(deps.registry, deps.state, deps.sfn, draft, snap, rec, now,
                                       open_incident=open_inc, lookup=False)
    status = "in_review" if link["tech_id"] else "open"
    item = {
        **store.report_key(report_id), **draft, "model": snap["model"], "zone_id": snap["zone_id"],
        "status": status, "telemetry_snapshot": snap, "recommendation": rec, "incident_id": link["incident_id"],
        "incident_link": link["link"],
        "GSI1PK": f"MACHINE#{machine_id}", "GSI1SK": f"RPT#{reported_at}", "GSI2PK": f"RSTATUS#{status}",
        "GSI2SK": reported_at,
    }
    if link["tech_id"]:
        item["assigned_tech_id"] = link["tech_id"]
    payload = {
        "report_id": report_id, "model": snap["model"], "zone_id": snap["zone_id"], "abnormality_type": kind,
        "description": body["description"], "severity_perceived": body["severity_perceived"], "reported_by": user,
        "reported_at": reported_at, "urgency": rec["urgency"], "recommendation_source": rec["source"],
        "suggested_parts": rec["suggested_parts"], "incident_id": link["incident_id"], "incident_link": link["link"],
    }
    env = envelope.build("report.created", "aws.reports-api", "report", report_id, payload,
                         incident_id=link["incident_id"], machine_id=machine_id, occurred_at=now)
    try:
        store.put_report(deps.registry, deps.outbox.name, item, env, link["tx_items"])
    except store.ConditionFailed:
        log.event("reporte_duplicado", incident_id=link["incident_id"], **ctx)
        raise web.HttpError(409, {"error": "report already exists, retry", "report_id": report_id}) from None
    log.event("reporte_creado", incident_id=link["incident_id"], link=link["link"], urgency=rec["urgency"],
              source=rec["source"], abnormality_type=kind, status=status, event_id=env["event_id"], **ctx)
    return web.resp(201, {"report": store.public(item)})


def _is_mine(item: dict, user: str, tech_id: str | None) -> bool:
    return item.get("reported_by") == user or (tech_id is not None and item.get("assigned_tech_id") == tech_id)


def list_reports(event: dict, deps) -> dict:
    role = authz.require_role(event, *ROLES)
    qs = event.get("queryStringParameters") or {}
    status, machine_id, incident_id = qs.get("status"), qs.get("machine_id"), qs.get("incident_id")
    if status is not None and status not in store.STATUSES:
        raise web.HttpError(400, {"error": "status must be one of " + ", ".join(store.STATUSES)})
    if machine_id is not None and not MACHINE_RE.match(machine_id):
        raise web.HttpError(400, {"error": "invalid machine_id"})
    inc_match = INC_RE.match(incident_id) if incident_id is not None else None
    if incident_id is not None and not inc_match:
        raise web.HttpError(400, {"error": "invalid incident_id"})
    try:
        limit = max(1, min(int(qs.get("limit", 50)), 200))
    except ValueError:
        raise web.HttpError(400, {"error": "limit must be an integer"}) from None
    mine = qs.get("mine") in ("1", "true") or role == "operator"
    if inc_match:
        if machine_id and machine_id != inc_match.group(1):
            return web.resp(200, {"reports": []})
        machine_id = inc_match.group(1)
    if machine_id:
        rows = store.reports_by_machine(deps.registry, machine_id)
    elif status:
        rows = store.reports_by_status(deps.registry, status)
    else:
        rows = [r for s in store.STATUSES for r in store.reports_by_status(deps.registry, s)]
    if incident_id:
        rows = [r for r in rows if r.get("incident_id") == incident_id]
    cache: dict = {}
    rows = [resolve_tech(deps.registry, r, cache) for r in rows]
    if status:
        rows = [r for r in rows if r.get("status") == status]
    if mine:
        user, tech = authz.user_of(event)[:MAX_USER], authz.tech_id_of(event)
        rows = [r for r in rows if _is_mine(r, user, tech)]
    rows.sort(key=lambda r: r.get("reported_at", ""), reverse=True)
    return web.resp(200, {"reports": [store.public(r) for r in rows[:limit]]})


def _load(event: dict, deps) -> dict:
    report_id = (event.get("pathParameters") or {}).get("report_id") or ""
    item = store.get_report(deps.registry, report_id) if RPT_RE.match(report_id) else None
    if item is None:
        raise web.HttpError(404, {"error": "report not found", "report_id": report_id})
    return item


def get_report(event: dict, deps) -> dict:
    role = authz.require_role(event, *ROLES)
    item = _load(event, deps)
    if role == "operator" and item.get("reported_by") != authz.user_of(event)[:MAX_USER]:
        raise authz.Forbidden("un operador solo ve sus propios reportes")
    item = resolve_tech(deps.registry, item, {})
    return web.resp(200, {"report": store.public(item), "timeline": store.timeline(deps.registry,
                                                                                    item["report_id"])})


def close(event: dict, deps) -> dict:
    role = authz.require_role(event, *CLOSE_ROLES)
    user = _user(event)
    item = _load(event, deps)
    ctx = _ctx(item)
    if item.get("status") == "closed":
        raise web.HttpError(409, {"error": "report already closed", "closed_at": item.get("closed_at")})
    tech = None
    if role == "technician":
        tech = authz.tech_id_of(event)
        if not tech:
            raise authz.Forbidden("el tecnico no tiene custom:tech_id")
        item = resolve_tech(deps.registry, item, {})
        if item.get("assigned_tech_id") not in (None, tech):
            raise authz.Forbidden("el reporte esta asignado a otro tecnico")
    body = web.body(event)
    _validate("close", body)
    now = clock.now()
    attrs = {"closed_at": clock.iso(now), "closed_by": tech or user, "findings": body["findings"],
             "parts_used": body.get("parts_used") or []}
    if body.get("confirmed_cause"):
        attrs["confirmed_cause"] = body["confirmed_cause"]
    payload = {"report_id": item["report_id"], **attrs, "incident_id": item.get("incident_id")}
    try:
        env = envelope.build("report.closed", "aws.reports-api", "report", item["report_id"], payload,
                             incident_id=item.get("incident_id"), machine_id=item["machine_id"], occurred_at=now)
    except schema.ContractError as exc:
        raise web.HttpError(400, {"error": exc.message, "path": exc.path}) from None
    extra = []
    inc = store.get_incident(deps.registry, item["incident_id"]) if item.get("incident_id") else None
    if inc and inc.get("status") == "open":
        extra.append({"Put": {"TableName": deps.registry.name,
                              "Item": store.event_item(f"INCIDENT#{item['incident_id']}", env)}})
    if tech and not item.get("assigned_tech_id"):
        attrs["assigned_tech_id"] = tech
    try:
        store.close_report(deps.registry, deps.outbox.name, item["report_id"], env, attrs, tech_id=tech,
                           extra_items=extra)
    except store.ConditionFailed:
        current = store.get_report(deps.registry, item["report_id"]) or {}
        log.event("cierre_reporte_rechazado", status=current.get("status"),
                  assigned_tech_id=current.get("assigned_tech_id"), **ctx)
        if current.get("status") == "closed":
            raise web.HttpError(409, {"error": "report already closed", "closed_at": current.get("closed_at")}) \
                from None
        raise web.HttpError(409, {"error": "report changed during close, retry"}) from None
    log.event("reporte_cerrado", closed_by=attrs["closed_by"], incident_ev=bool(extra),
              parts_used=len(attrs["parts_used"]), event_id=env["event_id"], **ctx)
    return web.resp(200, {"report": store.public({**item, **attrs, "status": "closed"}),
                          "incident_event": bool(extra)})


def list_types(event: dict, deps) -> dict:
    authz.require_role(event, *ROLES)
    return web.resp(200, {"abnormality_types": [store.public(t) for t in
                                                _catalog(deps.registry, clock.now()).values()]})


ROUTES = {
    "POST /reports": create,
    "GET /reports": list_reports,
    "GET /reports/{report_id}": get_report,
    "POST /reports/{report_id}/close": close,
    "GET /abnormality-types": list_types,
}


def handler(event, context=None, registry=None, state=None, outbox=None, sfn=None, secrets=None, http=None):
    deps = SimpleNamespace(
        registry=registry or store.table_from_env("REGISTRY_TABLE"),
        state=state or store.table_from_env("STATE_TABLE"),
        outbox=outbox or store.table_from_env("OUTBOX_TABLE"),
        sfn=sfn, secrets=secrets, http=http)
    return web.dispatch(event, ROUTES, deps)
