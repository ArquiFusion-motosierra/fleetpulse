"""Pruebas de las APIs HTTP de fp-incidents (payload v2): aprobacion por correo (`api/approvals`) y cierre del
incidente (`api/close`). Moto DynamoDB y Secrets Manager; Step Functions con un doble que usa las excepciones
reales de botocore."""
import json
import re
from datetime import timedelta
from decimal import Decimal
from urllib.parse import quote

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from incidents_support import (
    INC,
    KM_LAT,
    LAT,
    LON,
    NOW,
    NOW_ISO,
    FakeSfn,
    assign,
    assigned,
    created,
    ev_items,
    get_meta,
    get_tech,
    log_lines,
    outbox_events,
    ranked,
    seed_incident,
    seed_tech,
)

from api import approvals, close
from flow import reserve_parts
from fplib import clock
from lib import registry, technicians

TOKEN = "AAAAKgAAAAIAAAAAAAAAAQ+6C4y/7ZgXt=="  # los task tokens traen +, / y =
EXECUTION = f"arn:aws:states:us-east-1:000000000000:execution:incident-flow:{INC}"
CONSOLE_KEY, INGEST_KEY = "console-key-123", "ingest-key-456"
BODY = {"closed_by": "TEC-01", "parts_used": [{"part_id": "PRT-04", "zone_id": "NORTE", "qty": 1}],
        "cause_summary": "Bomba hidraulica cambiada"}


def _http(method, path, path_params, query=None, headers=None, body=None):
    return {"version": "2.0", "rawPath": path, "headers": headers or {}, "queryStringParameters": query,
            "pathParameters": path_params, "requestContext": {"http": {"method": method, "path": path}},
            "body": body, "isBase64Encoded": False}


# --- /approvals/{token} --------------------------------------------------------------------------------------------

def _approve(method="POST", token=quote(TOKEN, safe=""), tech="TEC-02", inc=INC):
    query = {k: v for k, v in (("tech", tech), ("inc", inc)) if v is not None} or None
    body = "confirm=1" if method == "POST" else None  # lo que manda el formulario de la pagina de confirmacion
    ev = _http(method, f"/approvals/{token}", {"token": token}, query, body=body)
    ev["headers"] = {"content-type": "application/x-www-form-urlencoded"} if body else {}
    return ev


@pytest.fixture
def waiting(ddb):
    """INC de EX-017 abierto, escalado a immediate y todavia sin tecnico (la ejecucion espera la aprobacion)."""
    seed_incident(ddb.registry, INC, NOW_ISO, severity="immediate", zone_id="NORTE", failure_mode="hydraulic_pump")
    return ddb


def test_approval_get_only_renders_a_confirmation_page(waiting, caplog):
    """Los escaneres de correo abren los enlaces: el GET no reanuda la ejecucion, solo pide confirmar."""
    sfn = FakeSfn()
    with caplog.at_level("INFO", logger="fplib"):
        resp = approvals.handler(_approve(method="GET"), sfn=sfn, registry=waiting.registry)
    assert sfn.calls == []
    assert resp["statusCode"] == 200 and resp["headers"]["content-type"].startswith("text/html")
    assert resp["headers"]["cache-control"] == "no-store" and resp["headers"]["referrer-policy"] == "no-referrer"
    page = resp["body"]
    assert INC in page and "EX-017" in page and "immediate" in page and "TEC-02" in page
    # el formulario vuelve a la misma URL (sin action) por POST; nada externo y el token no se copia a la pagina
    assert re.search(r'<form method="post">\s*<button type="submit"[^>]*>Aprobar</button>', page)
    assert "action=" not in page and "http" not in page.replace("http-equiv", "")
    assert TOKEN not in page and quote(TOKEN, safe="") not in page
    assert TOKEN not in caplog.text and quote(TOKEN, safe="") not in caplog.text
    (line,) = log_lines(caplog, "aprobacion_confirmacion_mostrada")
    assert line["incident_id"] == INC and line["machine_id"] == "EX-017" and line["tech_id"] == "TEC-02"


def test_approval_get_without_tech_says_the_ranking_decides(waiting):
    resp = approvals.handler(_approve(method="GET", tech=None), sfn=FakeSfn(), registry=waiting.registry)
    assert resp["statusCode"] == 200 and "el mejor técnico disponible" in resp["body"]


@pytest.mark.parametrize("attrs", [{"tech_id": "TEC-01", "approval": "approved"}, {"status": "closed"}])
def test_approval_get_for_an_incident_already_decided_says_so(ddb, attrs):
    seed_incident(ddb.registry, INC, NOW_ISO, severity="schedule", **attrs)
    sfn = FakeSfn()
    resp = approvals.handler(_approve(method="GET"), sfn=sfn, registry=ddb.registry)
    assert resp["statusCode"] == 200 and "Esta aprobación ya fue procesada" in resp["body"]
    assert "<form" not in resp["body"] and sfn.calls == []


def test_approval_get_still_asks_when_the_incident_cannot_be_read(ddb, caplog):
    """Sin META (o sin poder leerla) igual se muestra el boton: la decision la toma Step Functions en el POST."""
    sfn = FakeSfn()
    resp = approvals.handler(_approve(method="GET"), sfn=sfn, registry=ddb.registry)
    assert resp["statusCode"] == 200 and "<form" in resp["body"] and sfn.calls == []


def test_approval_post_sends_task_success(caplog):
    sfn = FakeSfn()
    with caplog.at_level("INFO", logger="fplib"):
        resp = approvals.handler(_approve(), sfn=sfn)
    (call,) = sfn.calls
    assert call["taskToken"] == TOKEN  # sin el url-encoding del enlace
    assert json.loads(call["output"]) == {"approval": "approved", "tech_id": "TEC-02"}
    assert resp["statusCode"] == 200 and resp["headers"]["content-type"].startswith("text/html")
    assert f"Aprobado: TEC-02 asignado a {INC}" in resp["body"] and "<form" not in resp["body"]
    assert TOKEN not in caplog.text and quote(TOKEN, safe="") not in caplog.text
    (line,) = log_lines(caplog, "aprobacion_recibida")
    assert line["incident_id"] == INC and line["machine_id"] == "EX-017" and line["tech_id"] == "TEC-02"


def test_approval_post_accepts_an_already_decoded_token():
    sfn = FakeSfn()
    resp = approvals.handler(_approve(token=TOKEN), sfn=sfn)
    assert resp["statusCode"] == 200 and sfn.calls[0]["taskToken"] == TOKEN


def test_approval_post_without_tech_lets_the_ranking_decide():
    sfn = FakeSfn()
    resp = approvals.handler(_approve(tech=None), sfn=sfn)
    assert json.loads(sfn.calls[0]["output"]) == {"approval": "approved", "tech_id": None}
    assert resp["statusCode"] == 200 and f"asignado a {INC}" in resp["body"]


@pytest.mark.parametrize("error", ["TaskTimedOut", "TaskDoesNotExist", "InvalidToken"])
def test_approval_post_is_idempotent(error, caplog):
    sfn = FakeSfn(fail=error)
    with caplog.at_level("INFO", logger="fplib"):
        resp = approvals.handler(_approve(), sfn=sfn)
    assert resp["statusCode"] == 200 and "Esta aprobación ya fue procesada" in resp["body"]
    (line,) = log_lines(caplog, "aprobacion_ya_procesada")
    assert line["error"] == error and line["incident_id"] == INC


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_approval_rejects_a_bad_tech_without_touching_the_execution(method):
    sfn = FakeSfn()
    resp = approvals.handler(_approve(method=method, tech="<script>alert(1)</script>"), sfn=sfn)
    assert resp["statusCode"] == 400 and sfn.calls == [] and "<script>" not in resp["body"]


# --- POST /incidents/{incident_id}/close ---------------------------------------------------------------------------

@pytest.fixture
def keys(ddb):
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    sm.create_secret(Name="fleetpulse/api/console-key", SecretString=json.dumps({"api_key": CONSOLE_KEY}))
    sm.create_secret(Name="fleetpulse/api/ingest-key", SecretString=json.dumps({"api_key": INGEST_KEY}))
    return sm


@pytest.fixture
def open_incident(ex017, ddb):
    """EX-017 abierto y asignado a TEC-01 (reservado en fp-state)."""
    seed_tech(ddb.state, "TEC-01", "hydraulics", LAT + 0.4 * KM_LAT, LON)
    return assigned(ex017, ddb)


def _close(ddb, keys, incident_id=INC, key=CONSOLE_KEY, body=BODY, sfn=None):
    headers = {"x-api-key": key} if key else {}
    raw = body if isinstance(body, str) else json.dumps(body)
    ev = _http("POST", f"/incidents/{incident_id}/close", {"incident_id": incident_id}, headers=headers, body=raw)
    return close.handler(ev, state=ddb.state, registry=ddb.registry, outbox=ddb.outbox, secrets=keys,
                         sfn=sfn or FakeSfn())


@pytest.mark.parametrize("key,reason", [(None, "missing_header"), ("wrong-key", "mismatch")])
def test_close_requires_console_key(ddb, keys, open_incident, key, reason, caplog):
    sfn = FakeSfn()
    with caplog.at_level("INFO", logger="fplib"):
        resp = _close(ddb, keys, key=key, sfn=sfn)
    assert resp["statusCode"] == 401 and sfn.stops == []
    assert get_meta(ddb.registry, INC)["status"] == "open" and get_tech(ddb.state, "TEC-01")["available"] is False
    assert outbox_events(ddb.outbox, "incident.closed") == []
    # una sola linea con el motivo por secreto, sin valores
    (line,) = log_lines(caplog, "cierre_no_autorizado")
    assert line["level"] == "ERROR" and line["incident_id"] == INC and line["status"] == 401
    assert line["reasons"] == {"fleetpulse/api/console-key": reason, "fleetpulse/api/ingest-key": reason}
    assert "wrong-key" not in caplog.text and CONSOLE_KEY not in caplog.text and INGEST_KEY not in caplog.text


def test_close_401_names_an_unreadable_secret(ddb, open_incident, caplog):
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    sm.create_secret(Name="fleetpulse/api/console-key", SecretString="raw-string-not-json")
    with caplog.at_level("INFO", logger="fplib"):
        assert _close(ddb, sm)["statusCode"] == 401
    (line,) = log_lines(caplog, "cierre_no_autorizado")
    assert line["reasons"] == {"fleetpulse/api/console-key": "secret_unavailable:JSONDecodeError",
                               "fleetpulse/api/ingest-key": "secret_unavailable:ResourceNotFoundException"}
    assert "raw-string-not-json" not in caplog.text


@pytest.mark.parametrize("key", [CONSOLE_KEY, INGEST_KEY])
def test_close_accepts_either_key_and_the_console_body(ddb, keys, open_incident, key, caplog):
    with caplog.at_level("INFO", logger="fplib"):
        resp = _close(ddb, keys, key=key, body={"closed_by": "console", "cause_summary": ""})  # cuerpo de la consola
    assert resp["statusCode"] == 200
    assert [r for r in caplog.records if r.levelname == "ERROR"] == []  # la ingest-key no deja un "mismatch" falso
    (env,) = outbox_events(ddb.outbox, "incident.closed")
    assert env["payload"]["parts_used"] == [] and env["payload"]["closed_by"] == "console"


def test_close_updates_registry_frees_tech_and_emits_closed(ddb, keys, open_incident, monkeypatch, caplog):
    later = NOW + timedelta(hours=2, minutes=30, seconds=36)  # 2.51 h despues de opened_at
    monkeypatch.setattr(clock, "now", lambda: later)
    sfn = FakeSfn()
    with caplog.at_level("INFO", logger="fplib"):
        resp = _close(ddb, keys, sfn=sfn)
    assert resp["statusCode"] == 200 and json.loads(resp["body"]) == {"incident_id": INC, "downtime_h": 2.51}
    # la ejecucion (nombre = incident_id) se detiene: no asigna ni notifica un incidente cerrado
    assert sfn.stops == [{"executionArn": EXECUTION, "error": "IncidentClosed", "cause": "closed from console"}]
    meta = get_meta(ddb.registry, INC)
    assert meta["opened_at"] == NOW_ISO and meta["closed_at"] == clock.iso(later)
    assert meta["status"] == "closed" and meta["downtime_h"] == Decimal("2.51") and meta["closed_by"] == "TEC-01"
    assert meta["cause_summary"] == "Bomba hidraulica cambiada" and meta["GSI2PK"] == "STATUS#closed"
    closed = ddb.registry.query(IndexName="GSI2", KeyConditionExpression=Key("GSI2PK").eq("STATUS#closed"))["Items"]
    assert [i["incident_id"] for i in closed] == [INC]
    tech = get_tech(ddb.state, "TEC-01")
    assert tech["available"] is True and tech["current_incident"] is None
    (env,) = outbox_events(ddb.outbox, "incident.closed")
    assert env["source"] == "aws.incidents-api" and env["incident_id"] == INC and env["machine_id"] == "EX-017"
    assert env["payload"] == {"closed_at": clock.iso(later), "downtime_h": 2.51, "parts_used": BODY["parts_used"],
                              "closed_by": "TEC-01", "cause_summary": "Bomba hidraulica cambiada"}
    (item,) = [i for i in ev_items(ddb.registry, INC) if i["event_type"] == "incident.closed"]
    assert json.loads(item["payload"]) == env
    # lo que leen CheckExistingIncident y la recurrencia de CreateIncident
    assert registry.open_incident_for(ddb.registry, "EX-017") is None
    assert registry.recent_closed_same_mode(ddb.registry, "EX-017", "hydraulic_pump", later - timedelta(days=7))
    (line,) = log_lines(caplog, "incidente_cerrado")
    assert line["incident_id"] == INC and line["machine_id"] == "EX-017" and line["downtime_h"] == 2.51


def test_close_twice_is_409(ddb, keys, open_incident):
    assert _close(ddb, keys)["statusCode"] == 200
    sfn = FakeSfn()
    resp = _close(ddb, keys, sfn=sfn)
    assert resp["statusCode"] == 409 and sfn.stops == []
    assert len(outbox_events(ddb.outbox, "incident.closed")) == 1


def test_close_racing_another_close_is_409_from_the_transaction(ddb, keys, open_incident, monkeypatch, caplog):
    stale = get_meta(ddb.registry, INC)  # la lectura lo vio abierto; otro cierre gano antes de la transaccion
    assert _close(ddb, keys)["statusCode"] == 200
    monkeypatch.setattr(registry, "get_incident", lambda table, incident_id: stale)
    sfn = FakeSfn()
    with caplog.at_level("INFO", logger="fplib"):
        resp = _close(ddb, keys, sfn=sfn)
    assert resp["statusCode"] == 409 and sfn.stops == []
    assert len(outbox_events(ddb.outbox, "incident.closed")) == 1
    (line,) = log_lines(caplog, "cierre_ya_aplicado")
    assert line["incident_id"] == INC and line["machine_id"] == "EX-017"


def test_close_when_the_tech_changes_incident_mid_close_is_500(ddb, keys, open_incident, monkeypatch, caplog):
    stale, real_get = get_tech(ddb.state, "TEC-01"), technicians.get  # leido enlazado a INC
    other = "INC-EX-003-20261115T1500"
    ddb.state.update_item(Key={"PK": "TECH#TEC-01", "SK": "META"}, UpdateExpression="SET current_incident = :i",
                          ExpressionAttributeValues={":i": other})
    monkeypatch.setattr(technicians, "get", lambda state, tid: stale if tid == "TEC-01" else real_get(state, tid))
    with caplog.at_level("INFO", logger="fplib"):
        resp = _close(ddb, keys)
    assert resp["statusCode"] == 500
    assert get_meta(ddb.registry, INC)["status"] == "open" and get_tech(ddb.state, "TEC-01")["current_incident"] == other
    assert outbox_events(ddb.outbox, "incident.closed") == []
    (line,) = log_lines(caplog, "cierre_tecnico_cambio")
    assert line["tech_id"] == "TEC-01" and line["incident_id"] == INC


@pytest.mark.parametrize("stop_fail, msg", [("ExecutionDoesNotExist", "ejecucion_inexistente"),
                                            ("ThrottlingException", "stop_execution_fallo")])
def test_close_succeeds_even_if_the_execution_cannot_be_stopped(ddb, keys, open_incident, caplog, stop_fail, msg):
    with caplog.at_level("INFO", logger="fplib"):
        resp = _close(ddb, keys, sfn=FakeSfn(stop_fail=stop_fail))
    assert resp["statusCode"] == 200 and get_meta(ddb.registry, INC)["status"] == "closed"
    (line,) = log_lines(caplog, msg)
    assert line["incident_id"] == INC


def test_close_while_waiting_for_approval_keeps_the_incident_closed(ex017, ddb, keys):
    """Secuencia del revisor: ranking (schedule, la ejecucion queda en WaitForApproval) -> cierre desde la consola
    -> la aprobacion vence y AssignTechnician corre igual (StopExecution no llego a tiempo)."""
    seed_tech(ddb.state, "TEC-01", "hydraulics", LAT + 0.4 * KM_LAT, LON)
    ev = ranked(ex017, ddb)
    assert ev["classified"]["needs_approval"] is True
    assert _close(ddb, keys)["statusCode"] == 200
    out = assign({**ev, "approval": {"approval": "timeout", "tech_id": None}}, ddb)
    assert out == {"tech_id": None, "assigned_at": None, "approval": "timeout", "tech_busy": False,
                   "skipped": "closed", "severity": "schedule"}
    assert assign({**ev, "approval": {"approval": "timeout", "tech_id": None}}, ddb) == out  # reintento
    meta = get_meta(ddb.registry, INC)
    assert meta["status"] == "closed" and meta["GSI2PK"] == "STATUS#closed" and "tech_id" not in meta
    tech = get_tech(ddb.state, "TEC-01")
    assert tech["available"] is True and tech.get("current_incident") is None  # no quedo tomado
    assert outbox_events(ddb.outbox, "incident.assigned") == []
    assert registry.open_incident_for(ddb.registry, "EX-017") is None  # CheckExistingIncident no silencia la maquina
    # ReserveParts tampoco pide repuestos para un incidente cerrado
    assert reserve_parts.handler({**ev, "assignment": out}, registry=ddb.registry, outbox=ddb.outbox) == {
        "requested": [], "skipped": "closed"}
    assert outbox_events(ddb.outbox, "part.reserve.requested", contract="part") == []
    assert _close(ddb, keys)["statusCode"] == 409


@pytest.mark.parametrize("incident_id", ["INC-EX-017-20261115T1500", "no-es-un-id"])
def test_close_unknown_incident_is_404(ddb, keys, open_incident, incident_id, caplog):
    with caplog.at_level("INFO", logger="fplib"):
        assert _close(ddb, keys, incident_id=incident_id)["statusCode"] == 404
    assert outbox_events(ddb.outbox, "incident.closed") == []
    (line,) = log_lines(caplog, "cierre_incidente_inexistente")
    assert line["incident_id"] == incident_id and line["status"] == 404


def test_close_does_not_free_a_tech_working_another_incident(ex017, ddb, keys):
    other = "INC-EX-003-20261115T1400"
    seed_tech(ddb.state, "TEC-01", "hydraulics", LAT + 0.4 * KM_LAT, LON, available=False, current_incident=other)
    ev = assigned(ex017, ddb)
    assert ev["assignment"]["tech_id"] == "TEC-01" and ev["assignment"]["tech_busy"] is True
    assert _close(ddb, keys)["statusCode"] == 200
    tech = get_tech(ddb.state, "TEC-01")
    assert tech["available"] is False and tech["current_incident"] == other
    assert get_meta(ddb.registry, INC)["status"] == "closed"


@pytest.mark.parametrize("body", [
    "no es json",
    {"parts_used": []},  # falta closed_by
    {**BODY, "parts_used": [{"part_id": "PRT-04", "zone_id": "NORTE", "qty": 0}]},  # el contrato pide qty >= 1
])
def test_close_rejects_a_bad_body(ddb, keys, open_incident, body):
    assert _close(ddb, keys, body=body)["statusCode"] == 400
    assert get_meta(ddb.registry, INC)["status"] == "open" and outbox_events(ddb.outbox, "incident.closed") == []


# --- POST /me/incidents/{incident_id}/close (ID token de Cognito) --------------------------------------------------

TECH_CLAIMS = {"sub": "0f3a-tec", "email": "tecnico@example.com", "custom:role": "technician", "custom:tech_id": "TEC-01"}


def _claims(role, **extra):
    return {"sub": f"sub-{role}", "email": f"{role}@example.com", "custom:role": role, **extra}


def _close_as(ddb, claims, incident_id=INC, body=BODY, sfn=None):
    """Como lo entrega API Gateway tras el authorizer JWT `cognito`: claims en requestContext y sin x-api-key. Sin
    secretos de API key en Secrets Manager: la ruta no los usa."""
    raw = body if isinstance(body, str) or body is None else json.dumps(body)
    ev = _http("POST", f"/me/incidents/{incident_id}/close", {"incident_id": incident_id}, body=raw)
    ev["routeKey"] = close.USER_ROUTE
    if claims is not None:
        ev["requestContext"]["authorizer"] = {"jwt": {"claims": claims, "scopes": None}}
    return close.handler(ev, state=ddb.state, registry=ddb.registry, outbox=ddb.outbox, sfn=sfn or FakeSfn())


def _untouched(ddb):
    assert get_meta(ddb.registry, INC)["status"] == "open" and get_tech(ddb.state, "TEC-01")["available"] is False
    assert outbox_events(ddb.outbox, "incident.closed") == []


def test_user_close_by_the_assigned_technician_records_who_closed_from_the_token(ddb, open_incident, caplog):
    sfn = FakeSfn()
    with caplog.at_level("INFO", logger="fplib"):
        resp = _close_as(ddb, TECH_CLAIMS, body={**BODY, "closed_by": "otro"}, sfn=sfn)  # closed_by del cuerpo no cuenta
    assert resp["statusCode"] == 200 and json.loads(resp["body"])["incident_id"] == INC
    assert sfn.stops == [{"executionArn": EXECUTION, "error": "IncidentClosed", "cause": "closed from console"}]
    meta = get_meta(ddb.registry, INC)
    assert meta["status"] == "closed" and meta["closed_by"] == "tecnico@example.com"
    assert meta["parts_used"][0]["part_id"] == "PRT-04" and meta["cause_summary"] == "Bomba hidraulica cambiada"
    tech = get_tech(ddb.state, "TEC-01")
    assert tech["available"] is True and tech["current_incident"] is None
    (env,) = outbox_events(ddb.outbox, "incident.closed")
    assert env["payload"]["closed_by"] == "tecnico@example.com"
    (line,) = log_lines(caplog, "incidente_cerrado")
    assert line["role"] == "technician" and line["closed_by"] == "tecnico@example.com"


@pytest.mark.parametrize("role", ["supervisor", "admin"])
def test_user_close_by_supervisor_or_admin(ddb, open_incident, role):
    resp = _close_as(ddb, _claims(role), body={})  # el cuerpo es opcional: sin repuestos ni causa
    assert resp["statusCode"] == 200
    meta = get_meta(ddb.registry, INC)
    assert meta["closed_by"] == f"{role}@example.com" and meta["parts_used"] == [] and meta["cause_summary"] == ""


def test_user_close_without_email_uses_sub_cut_to_the_contract(ddb, open_incident):
    claims = {"sub": "s" * 80, "custom:role": "supervisor"}
    assert _close_as(ddb, claims, body=None)["statusCode"] == 200
    assert get_meta(ddb.registry, INC)["closed_by"] == "s" * 64  # incident.closed: closed_by maxLength 64


@pytest.mark.parametrize("claims", [
    {**TECH_CLAIMS, "custom:tech_id": "TEC-02"},  # otro tecnico
    {k: v for k, v in TECH_CLAIMS.items() if k != "custom:tech_id"},  # tecnico sin tech_id
    _claims("operator"), _claims("management"), {"sub": "x", "email": "sin-rol@example.com"},
])
def test_user_close_forbidden(ddb, open_incident, claims, caplog):
    sfn = FakeSfn()
    with caplog.at_level("INFO", logger="fplib"):
        resp = _close_as(ddb, claims, sfn=sfn)
    assert resp["statusCode"] == 403 and json.loads(resp["body"])["error"] == "forbidden" and sfn.stops == []
    _untouched(ddb)
    (line,) = log_lines(caplog, "cierre_prohibido")
    assert line["status"] == 403 and line["incident_id"] == INC and line["role"] == claims.get("custom:role")


def test_user_close_technician_cannot_close_an_unassigned_incident(ex017, ddb):
    created(ex017, ddb)  # abierto, sin tecnico todavia
    resp = _close_as(ddb, TECH_CLAIMS)
    assert resp["statusCode"] == 403 and get_meta(ddb.registry, INC)["status"] == "open"
    assert _close_as(ddb, _claims("supervisor"))["statusCode"] == 200


@pytest.mark.parametrize("claims", [None, {}])
def test_user_close_without_claims_is_401(ddb, open_incident, claims, caplog):
    with caplog.at_level("INFO", logger="fplib"):
        resp = _close_as(ddb, claims)
    assert resp["statusCode"] == 401
    _untouched(ddb)
    (line,) = log_lines(caplog, "cierre_no_autorizado")
    assert line["reasons"] == {"jwt": "missing_claims"}


def test_user_close_unknown_incident_is_404_and_twice_is_409(ddb, open_incident):
    assert _close_as(ddb, _claims("supervisor"), incident_id="INC-EX-017-20261115T1500")["statusCode"] == 404
    assert _close_as(ddb, TECH_CLAIMS)["statusCode"] == 200
    assert _close_as(ddb, TECH_CLAIMS)["statusCode"] == 409
    assert len(outbox_events(ddb.outbox, "incident.closed")) == 1


def test_user_close_rejects_a_bad_body(ddb, open_incident):
    assert _close_as(ddb, TECH_CLAIMS, body="no es json")["statusCode"] == 400
    bad = {"parts_used": [{"part_id": "PRT-04", "zone_id": "NORTE", "qty": 0}]}
    assert _close_as(ddb, TECH_CLAIMS, body=bad)["statusCode"] == 400
    _untouched(ddb)


def test_api_key_route_ignores_the_user_route_rules(ddb, keys, open_incident):
    """La ruta de siempre (consola actual, simulador, smoke) sigue con x-api-key y closed_by del cuerpo."""
    resp = _close(ddb, keys, body={**BODY, "closed_by": "TEC-01"})
    assert resp["statusCode"] == 200 and get_meta(ddb.registry, INC)["closed_by"] == "TEC-01"


# --- multi-tenancy: cierre solo en el parque del usuario ------------------------------------------------------------

def test_user_close_from_another_park_is_not_found(ddb, open_incident):
    resp = _close_as(ddb, _claims("admin", **{"cognito:groups": "[tenant-agro]"}))
    assert resp["statusCode"] == 404
    _untouched(ddb)


def test_user_close_park_admin_of_the_same_park(ddb, open_incident):
    assert _close_as(ddb, _claims("admin", **{"cognito:groups": "[tenant-mineria]"}))["statusCode"] == 200


def test_user_close_maintenance_head_closes_in_any_park(ddb, open_incident):
    resp = _close_as(ddb, _claims("maintenance_head", **{"cognito:groups": "[maintenance-head]"}))
    assert resp["statusCode"] == 200 and get_meta(ddb.registry, INC)["closed_by"] == "maintenance_head@example.com"
