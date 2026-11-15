"""Pruebas de la Lambda `reports` (T32): crear, listar, ver y cerrar reportes con claims de Cognito por rol.
Moto DynamoDB; Cloud Run `/recommend-checks`, Secrets Manager y Step Functions con dobles (sin red)."""
import json
from datetime import timedelta

import pytest
from reports_support import (
    ADMIN,
    AUDIENCE,
    CLOSE_BODY,
    CREATE_BODY,
    INC,
    MANAGEMENT,
    NOW,
    OPERATOR,
    OPERATOR2,
    RPT,
    SUPERVISOR,
    T33_EXAMPLE,
    TECH1,
    TECH3,
    FakeHttp,
    FakeSfn,
    api_event,
    body_of,
    ev_items,
    hot_readings,
    log_lines,
    outbox_events,
    seed_incident,
    seed_machine,
    seed_readings,
)

from fplib import clock, envelope, ids


# Alias locales de los fixtures de conftest.py (ver su docstring): `_env` local tambien evita el `_env` global de
# aws/incidents en la sesion unica de CI.
@pytest.fixture(autouse=True)
def _env(reports_env):
    return reports_env


@pytest.fixture
def ddb(reports_ddb):
    return reports_ddb


@pytest.fixture
def invoker(reports_invoker):
    return reports_invoker


@pytest.fixture
def svc(reports_svc):
    return reports_svc


CREATE = "POST /reports"
GET = "GET /reports/{report_id}"
LIST = "GET /reports"
CLOSE = "POST /reports/{report_id}/close"


def _create(svc, claims=OPERATOR, body=CREATE_BODY):
    return svc.call(api_event(CREATE, claims, body=body))


def _close(svc, claims, report_id=RPT, body=CLOSE_BODY):
    return svc.call(api_event(CLOSE, claims, body=body, path={"report_id": report_id}))


def _meta(ddb, report_id=RPT):
    return ddb.registry.get_item(Key={"PK": f"REPORT#{report_id}", "SK": "META"}).get("Item")


@pytest.fixture
def ex017(ddb):
    seed_machine(ddb.state)
    seed_readings(ddb.state, hot_readings())
    return ddb


# --- POST /reports -------------------------------------------------------------------------------------------------

def test_operator_creates_report_with_ai_checks_and_starts_incident_flow(svc, ex017, caplog):
    with caplog.at_level("INFO", logger="fplib"):
        resp = _create(svc)
    assert resp["statusCode"] == 201, resp["body"]
    rep = body_of(resp)["report"]
    assert rep["report_id"] == RPT and rep["status"] == "open" and rep["reported_by"] == "operador@example.com"
    assert rep["recommendation"]["source"] == "gemini" and len(rep["recommendation"]["checks"]) == 3
    assert rep["recommendation"]["suggested_parts"] == ["PRT-01"]  # en el BOM de EX
    assert rep["incident_id"] == INC and rep["incident_link"] == "started"
    assert rep["telemetry_snapshot"]["means"]["coolant_temp_c"] == 104.0
    assert "PK" not in rep and "GSI2PK" not in rep

    (call,) = svc.http.calls  # /recommend-checks con el ID token y el contrato de T33
    assert call["url"] == f"{AUDIENCE}/recommend-checks" and call["timeout"] == 8
    assert call["headers"]["Authorization"] == "Bearer id-tok-123"
    sent = call["body"]
    assert sent["report_id"] == RPT and sent["model"] == "EX" and "open_incident" not in sent
    assert set(sent["telemetry_snapshot"]) == {"means", "fault_codes", "hours_total", "hours_since_service"}
    assert sent["telemetry_snapshot"]["fault_codes"] == [{"spn": 110, "fmi": 0, "severity": "high"}]

    (start,) = svc.sfn.calls  # incident-flow con una entrada como la de aws/scoring
    assert start["name"] == INC and start["stateMachineArn"].endswith(":stateMachine:incident-flow")
    inp = start["input"]
    assert inp["score"] == 3.6 and inp["severity"] == "immediate" and inp["source"] == "operator_report"
    assert inp["batch_id"] == RPT and inp["lat"] == -13.9003 and inp["zone_id"] == "NORTE"
    expected_mode = "head_gasket" if "head_gasket" in ids.load()["failure_modes"] else "engine_overheat"
    assert inp["failure_mode"] == expected_mode
    assert inp["deviated_vars"][0]["var"] == "coolant_temp_c" and inp["deviated_vars"][0]["z"] > 5
    # CreateIncident lo aceptaria: incident.opened valida con estos campos
    envelope.build("incident.opened", "aws.incident-flow", "incident", INC, {
        "zone_id": inp["zone_id"], "severity": inp["severity"], "score": inp["score"],
        "failure_mode": inp["failure_mode"], "deviated_vars": inp["deviated_vars"], "group_id": None,
        "recurrence_flag": False, "opened_at": "2026-11-15T14:40:13Z", "lat": inp["lat"], "lon": inp["lon"]},
        incident_id=INC, machine_id="EX-017")

    (env,) = outbox_events(ex017.outbox, "report.created")
    assert env["incident_id"] == INC and env["payload"]["incident_link"] == "started"
    assert env["payload"]["urgency"] == "immediate" and env["payload"]["recommendation_source"] == "gemini"
    (ev,) = ev_items(ex017.registry, f"REPORT#{RPT}")
    assert ev["event_type"] == "report.created"
    meta = _meta(ex017)
    assert meta["GSI1PK"] == "MACHINE#EX-017" and meta["GSI1SK"] == "RPT#2026-11-15T14:40:12Z"
    assert meta["GSI2PK"] == "RSTATUS#open" and meta["GSI2SK"] == "2026-11-15T14:40:12Z"
    (line,) = log_lines(caplog, "reporte_creado")
    assert line["report_id"] == RPT and line["machine_id"] == "EX-017" and line["incident_id"] == INC
    assert "no-se-registra" not in caplog.text and "id-tok-123" not in caplog.text


def test_report_links_the_open_incident_and_does_not_start_a_flow(svc, ex017):
    seed_incident(ex017.registry, tech_id="TEC-01")
    resp = _create(svc)
    assert resp["statusCode"] == 201, resp["body"]
    rep = body_of(resp)["report"]
    assert rep["incident_id"] == INC and rep["incident_link"] == "linked"
    assert rep["assigned_tech_id"] == "TEC-01" and rep["status"] == "in_review"  # el incidente ya tenia tecnico
    assert svc.sfn.calls == []
    assert svc.http.calls[0]["body"]["open_incident"] == {"incident_id": INC, "failure_mode": "hydraulic_pump",
                                                          "severity": "schedule"}
    (ev,) = ev_items(ex017.registry, f"INCIDENT#{INC}")  # misma transaccion que el reporte
    linked = json.loads(ev["payload"])
    assert ev["type"] == "report.linked" and linked["payload"]["report_id"] == RPT
    assert linked["payload"]["probable_cause"] == "Junta de culata con fuga"
    assert _meta(ex017)["GSI2PK"] == "RSTATUS#in_review"


def test_observe_urgency_without_open_incident_creates_no_incident(svc, ex017):
    svc.http = FakeHttp((200, json.dumps({**T33_EXAMPLE, "urgency": "observe"}).encode()))
    rep = body_of(_create(svc))["report"]
    assert rep["incident_id"] is None and rep["incident_link"] == "none" and svc.sfn.calls == []
    (env,) = outbox_events(ex017.outbox, "report.created")
    assert "incident_id" not in env and env["payload"]["incident_id"] is None


def test_execution_already_exists_reuses_the_window_incident(svc, ex017):
    svc.sfn = FakeSfn(fail="ExecutionAlreadyExists")
    rep = body_of(_create(svc))["report"]
    assert rep["incident_id"] == INC and rep["incident_link"] == "started"


def test_start_execution_failure_keeps_the_report(svc, ex017, caplog):
    svc.sfn = FakeSfn(fail="ThrottlingException")
    with caplog.at_level("INFO", logger="fplib"):
        resp = _create(svc)
    assert resp["statusCode"] == 201 and body_of(resp)["report"]["incident_id"] is None
    assert log_lines(caplog, "start_execution_fallo")[0]["incident_id"] == INC


def test_create_falls_back_to_the_template_when_insights_times_out(svc, ex017, caplog):
    svc.http = FakeHttp(TimeoutError("timed out"))
    with caplog.at_level("INFO", logger="fplib"):
        rep = body_of(_create(svc))["report"]
    rec = rep["recommendation"]
    assert rec["source"] == "template" and rec["urgency"] == "immediate" and len(rec["checks"]) == 3
    assert "culata" in rec["probable_causes"][0]["cause"].lower()
    assert "PRT-11" in rec["suggested_parts"]  # engine_overheat de EX
    (line,) = log_lines(caplog, "recommend_fallback")
    assert line["reason"] == "TimeoutError" and line["report_id"] == RPT
    assert "id-tok-123" not in caplog.text and "NO-ES-UNA-LLAVE" not in caplog.text
    assert rep["incident_link"] == "started"  # la plantilla tambien abre el incidente


def test_technician_cannot_create_reports(svc, ex017):
    resp = _create(svc, TECH1)
    assert resp["statusCode"] == 403 and body_of(resp)["error"] == "forbidden"
    assert ex017.registry.scan()["Items"] == [] and svc.http.calls == [] and svc.sfn.calls == []


@pytest.mark.parametrize("claims", [SUPERVISOR, ADMIN])
def test_supervisor_and_admin_can_create(svc, ex017, claims):
    assert _create(svc, claims)["statusCode"] == 201


def test_missing_claims_is_401(svc, ex017):
    assert _create(svc, None)["statusCode"] == 401
    assert svc.call(api_event(LIST, None))["statusCode"] == 401


@pytest.mark.parametrize("body,path", [
    ({**CREATE_BODY, "description": ""}, ["description"]),
    ({**CREATE_BODY, "severity_perceived": "critical"}, ["severity_perceived"]),
    ({**CREATE_BODY, "machine_id": "EX-17"}, ["machine_id"]),
    ({**CREATE_BODY, "extra": 1}, []),
])
def test_invalid_body_is_400_with_the_path(svc, ex017, body, path):
    resp = _create(svc, body=body)
    assert resp["statusCode"] == 400 and body_of(resp)["path"] == path


def test_unknown_machine_or_type_is_400(svc, ex017):
    resp = _create(svc, body={**CREATE_BODY, "machine_id": "EX-099"})
    assert resp["statusCode"] == 400 and body_of(resp)["error"] == "unknown machine_id"
    resp = _create(svc, body={**CREATE_BODY, "abnormality_type": "alien_noise"})
    assert resp["statusCode"] == 400 and "high_engine_temp" in body_of(resp)["allowed"]
    resp = svc.call({**api_event(CREATE, OPERATOR), "body": "{no json"})
    assert resp["statusCode"] == 400


def test_same_second_duplicate_is_409(svc, ex017):
    assert _create(svc)["statusCode"] == 201
    resp = _create(svc)
    assert resp["statusCode"] == 409 and body_of(resp)["report_id"] == RPT
    assert len(outbox_events(ex017.outbox, "report.created")) == 1


def test_create_without_telemetry_uses_ids_yaml(svc, ddb):
    rep = body_of(_create(svc))["report"]
    snap = rep["telemetry_snapshot"]
    assert snap["means"] == {} and snap["hours_total"] == 2762 and snap["hours_since_service"] == 286
    assert snap["zone_id"] == "NORTE"
    inp = svc.sfn.calls[0]["input"]  # sin META: centroide de NORTE y entrada sintetica del sintoma
    assert (inp["lat"], inp["lon"]) == (-13.9, -71.56)
    assert inp["deviated_vars"] == [{"var": "coolant_temp_c", "value": 1.0, "z": 1.0}]


# --- GET /reports y GET /reports/{id} ------------------------------------------------------------------------------

def test_get_report_by_role(svc, ex017):
    _create(svc)
    for claims in (OPERATOR, TECH1, SUPERVISOR, MANAGEMENT, ADMIN):
        resp = svc.call(api_event(GET, claims, path={"report_id": RPT}))
        assert resp["statusCode"] == 200, claims
        out = body_of(resp)
        assert out["report"]["report_id"] == RPT and out["timeline"][0]["type"] == "report.created"
    resp = svc.call(api_event(GET, OPERATOR2, path={"report_id": RPT}))  # operador ajeno
    assert resp["statusCode"] == 403
    resp = svc.call(api_event(GET, ADMIN, path={"report_id": "RPT-EX-017-20261115T999999"}))
    assert resp["statusCode"] == 404
    resp = svc.call(api_event(GET, ADMIN, path={"report_id": "nope"}))
    assert resp["statusCode"] == 404


def test_list_mine_filters_by_reporter_or_assigned_technician(svc, ex017, monkeypatch):
    _create(svc)  # operador, EX-017, inicia INC (sin tecnico aun)
    monkeypatch.setattr(clock, "now", lambda: NOW + timedelta(seconds=5))
    _create(svc, OPERATOR2, body={**CREATE_BODY, "machine_id": "EX-019", "abnormality_type": "noise"})
    monkeypatch.setattr(clock, "now", lambda: NOW + timedelta(seconds=9))
    _create(svc, SUPERVISOR, body={**CREATE_BODY, "machine_id": "CF-042", "severity_perceived": "low"})

    def ids_for(claims, **qs):
        resp = svc.call(api_event(LIST, claims, qs={k: str(v) for k, v in qs.items()} or None))
        assert resp["statusCode"] == 200, resp["body"]
        return [r["report_id"] for r in body_of(resp)["reports"]]

    assert ids_for(OPERATOR) == [RPT]  # un operador solo ve los suyos, aun sin mine
    assert ids_for(OPERATOR2) == ["RPT-EX-019-20261115T144017"]
    assert len(ids_for(SUPERVISOR)) == 3 and ids_for(SUPERVISOR)[0] == "RPT-CF-042-20261115T144021"
    assert ids_for(SUPERVISOR, mine=1) == ["RPT-CF-042-20261115T144021"]
    assert ids_for(TECH1, mine=1) == []
    seed_incident(ex017.registry, tech_id="TEC-01")  # incident-flow asigno a TEC-01: lectura perezosa
    assert ids_for(TECH1, mine=1) == [RPT]
    assert _meta(ex017)["assigned_tech_id"] == "TEC-01" and _meta(ex017)["status"] == "in_review"
    assert ids_for(TECH3, mine=1) == []
    assert ids_for(SUPERVISOR, machine_id="EX-017") == [RPT]
    assert ids_for(SUPERVISOR, incident_id=INC) == [RPT]
    assert ids_for(SUPERVISOR, status="in_review") == [RPT]
    assert ids_for(SUPERVISOR, status="open", limit=1) == ["RPT-CF-042-20261115T144021"]
    for bad in ({"status": "nope"}, {"machine_id": "X"}, {"incident_id": "INC-1"}, {"limit": "x"}):
        assert svc.call(api_event(LIST, SUPERVISOR, qs=bad))["statusCode"] == 400


# --- POST /reports/{id}/close --------------------------------------------------------------------------------------

def test_assigned_technician_closes_report_and_incident_gets_the_event(svc, ex017, caplog, monkeypatch):
    seed_incident(ex017.registry, tech_id="TEC-01")
    _create(svc)
    monkeypatch.setattr(clock, "now", lambda: NOW + timedelta(hours=1, minutes=22))
    with caplog.at_level("INFO", logger="fplib"):
        resp = _close(svc, TECH1)
    assert resp["statusCode"] == 200, resp["body"]
    out = body_of(resp)
    assert out["incident_event"] is True
    rep = out["report"]
    assert rep["status"] == "closed" and rep["closed_by"] == "TEC-01" and rep["confirmed_cause"]
    assert rep["closed_at"] == "2026-11-15T16:02:12Z"
    assert rep["parts_used"] == CLOSE_BODY["parts_used"]
    meta = _meta(ex017)
    assert meta["status"] == "closed" and meta["GSI2PK"] == "RSTATUS#closed"
    (env,) = outbox_events(ex017.outbox, "report.closed")
    assert env["incident_id"] == INC and env["payload"]["findings"] == CLOSE_BODY["findings"]
    types = sorted(i["type"] for i in ev_items(ex017.registry, f"INCIDENT#{INC}"))
    assert types == ["report.closed", "report.linked"]
    timeline = body_of(svc.call(api_event(GET, SUPERVISOR, path={"report_id": RPT})))["timeline"]
    assert [t["type"] for t in timeline] == ["report.created", "report.closed"]
    (line,) = log_lines(caplog, "reporte_cerrado")
    assert line["report_id"] == RPT and line["incident_id"] == INC and line["incident_ev"] is True


def test_other_technician_cannot_close(svc, ex017):
    seed_incident(ex017.registry, tech_id="TEC-01")
    _create(svc)
    resp = _close(svc, TECH3)
    assert resp["statusCode"] == 403 and "otro tecnico" in body_of(resp)["detail"]
    assert _meta(ex017)["status"] == "in_review" and outbox_events(ex017.outbox, "report.closed") == []


def test_unassigned_report_can_be_closed_by_any_technician_who_takes_it(svc, ex017):
    svc.http = FakeHttp((200, json.dumps({**T33_EXAMPLE, "urgency": "observe"}).encode()))
    _create(svc)
    resp = _close(svc, TECH3)
    assert resp["statusCode"] == 200
    assert _meta(ex017)["assigned_tech_id"] == "TEC-03" and body_of(resp)["incident_event"] is False


def test_close_twice_is_409(svc, ex017):
    _create(svc)
    assert _close(svc, SUPERVISOR)["statusCode"] == 200
    resp = _close(svc, ADMIN)
    assert resp["statusCode"] == 409 and body_of(resp)["closed_at"] == "2026-11-15T14:40:12Z"
    assert len(outbox_events(ex017.outbox, "report.closed")) == 1


@pytest.mark.parametrize("claims", [OPERATOR, MANAGEMENT])
def test_operator_and_management_cannot_close(svc, ex017, claims):
    _create(svc)
    assert _close(svc, claims)["statusCode"] == 403


def test_close_validates_the_body_and_closed_incident_gets_no_event(svc, ex017):
    seed_incident(ex017.registry, tech_id="TEC-01")
    _create(svc)
    resp = _close(svc, TECH1, body={"findings": ""})
    assert resp["statusCode"] == 400 and body_of(resp)["path"] == ["findings"]
    resp = _close(svc, TECH1, body={"findings": "ok", "parts_used": [{"part_id": "PRT-11", "zone_id": "NORTE",
                                                                       "qty": 0}]})
    assert resp["statusCode"] == 400
    seed_incident(ex017.registry, status="closed", tech_id="TEC-01")  # el tecnico ya cerro el incidente
    resp = _close(svc, TECH1, body={"findings": "Junta cambiada"})
    assert resp["statusCode"] == 200 and body_of(resp)["incident_event"] is False
    assert body_of(resp)["report"]["parts_used"] == []
    assert [i["type"] for i in ev_items(ex017.registry, f"INCIDENT#{INC}")] == ["report.linked"]


def test_technician_without_tech_id_claim_is_403(svc, ex017):
    _create(svc)
    claims = {k: v for k, v in TECH1.items() if k != "custom:tech_id"}
    assert _close(svc, claims)["statusCode"] == 403


# --- GET /abnormality-types y ruta desconocida ---------------------------------------------------------------------

def test_any_role_reads_the_catalog_seeded_on_first_use(svc, ddb):
    resp = svc.call(api_event("GET /abnormality-types", OPERATOR))
    assert resp["statusCode"] == 200
    types = body_of(resp)["abnormality_types"]
    assert len(types) == 10 and {"code", "label_es", "default_checks"} <= set(types[0])


def test_unknown_route_is_404(svc, ddb):
    assert svc.call(api_event("DELETE /reports", ADMIN))["statusCode"] == 404


# --- flujo completo (lo mismo que el smoke test en vivo, con moto y dobles) ----------------------------------------

def test_report_lifecycle_end_to_end_with_roles(svc, ex017, monkeypatch):
    routes = ((CREATE, None, CREATE_BODY), (LIST, None, None), (GET, {"report_id": RPT}, None),
              (CLOSE, {"report_id": RPT}, CLOSE_BODY), ("GET /abnormality-types", None, None))
    for route, path, body in routes:  # sin token: 401 en todas las rutas
        assert svc.call(api_event(route, None, body=body, path=path))["statusCode"] == 401, route

    types = body_of(svc.call(api_event("GET /abnormality-types", OPERATOR)))["abnormality_types"]
    assert CREATE_BODY["abnormality_type"] in {t["code"] for t in types}

    assert _create(svc, TECH1)["statusCode"] == 403  # un tecnico no reporta
    svc.http = FakeHttp(TimeoutError("timed out"))  # /recommend-checks (T33) aun no responde
    resp = _create(svc)
    assert resp["statusCode"] == 201, resp["body"]
    rep = body_of(resp)["report"]
    assert rep["status"] == "open" and rep["recommendation"]["source"] == "template"
    assert rep["incident_id"] == INC and rep["incident_link"] == "started" and len(svc.sfn.calls) == 1

    out = body_of(svc.call(api_event(GET, OPERATOR, path={"report_id": RPT})))
    assert out["report"]["telemetry_snapshot"]["means"]["coolant_temp_c"] == 104.0
    assert svc.call(api_event(GET, OPERATOR2, path={"report_id": RPT}))["statusCode"] == 403

    def mine(claims):
        resp = svc.call(api_event(LIST, claims, qs={"mine": "1"}))
        assert resp["statusCode"] == 200, resp["body"]
        return [(r["report_id"], r["status"]) for r in body_of(resp)["reports"]]

    assert mine(OPERATOR) == [(RPT, "open")] and mine(OPERATOR2) == [] and mine(TECH1) == []

    seed_incident(ex017.registry, tech_id="TEC-01")  # incident-flow asigno a TEC-01
    assert mine(TECH1) == [(RPT, "in_review")]
    assert _close(svc, MANAGEMENT)["statusCode"] == 403
    assert _close(svc, TECH3)["statusCode"] == 403  # asignado a otro tecnico

    monkeypatch.setattr(clock, "now", lambda: NOW + timedelta(minutes=45))
    resp = _close(svc, TECH1)
    assert resp["statusCode"] == 200, resp["body"]
    assert body_of(resp)["incident_event"] is True and body_of(resp)["report"]["closed_by"] == "TEC-01"
    assert _close(svc, SUPERVISOR)["statusCode"] == 409

    out = body_of(svc.call(api_event(GET, SUPERVISOR, path={"report_id": RPT})))
    assert out["report"]["status"] == "closed"
    assert [t["type"] for t in out["timeline"]] == ["report.created", "report.closed"]
    assert mine(OPERATOR) == [(RPT, "closed")]
    assert len(outbox_events(ex017.outbox, "report.created")) == 1
    assert len(outbox_events(ex017.outbox, "report.closed")) == 1
    assert sorted(i["type"] for i in ev_items(ex017.registry, f"INCIDENT#{INC}")) == ["report.closed"]


# --- multi-tenancy: reportes acotados al parque ----------------------------------------------------------------------

OPERATOR_AGRO = {**OPERATOR, "email": "operador.agro@example.com", "cognito:groups": "[tenant-agro]"}
SUPERVISOR_AGRO = {**SUPERVISOR, "email": "supervisor.agro@example.com", "cognito:groups": "[tenant-agro]"}
HEAD = {"custom:role": "maintenance_head", "email": "jefe.mantenimiento@example.com", "sub": "sub-head",
        "cognito:groups": "[maintenance-head]"}


def test_report_carries_the_park_and_its_tenant_index(svc, ddb, ex017):
    rep = body_of(_create(svc))["report"]
    meta = _meta(ddb)
    assert meta["tenant_id"] == "mineria" and meta["GSI3PK"] == "TENANT#mineria"
    assert meta["GSI3SK"] == f"RPT#{meta['reported_at']}" and "GSI3PK" not in rep and rep["tenant_id"] == "mineria"
    (env, *_) = outbox_events(ddb.outbox, "report.created")
    assert env["tenant_id"] == "mineria"


def test_operator_cannot_report_a_machine_of_another_park(svc, ddb, ex017):
    r = _create(svc, OPERATOR_AGRO)
    assert r["statusCode"] == 403 and _meta(ddb) is None


def test_reports_of_another_park_are_invisible(svc, ddb, ex017):
    assert _create(svc)["statusCode"] == 201
    assert body_of(svc.call(api_event(LIST, SUPERVISOR_AGRO)))["reports"] == []
    assert svc.call(api_event(GET, SUPERVISOR_AGRO, path={"report_id": RPT}))["statusCode"] == 404
    assert _close(svc, SUPERVISOR_AGRO)["statusCode"] == 404
    assert svc.call(api_event(LIST, SUPERVISOR_AGRO, qs={"tenant": "mineria"}))["statusCode"] == 403
    assert len(body_of(svc.call(api_event(LIST, SUPERVISOR)))["reports"]) == 1


def test_maintenance_head_sees_every_park_and_can_filter(svc, ddb, ex017):
    assert _create(svc)["statusCode"] == 201
    assert len(body_of(svc.call(api_event(LIST, HEAD)))["reports"]) == 1
    assert body_of(svc.call(api_event(LIST, HEAD, qs={"tenant": "agro"})))["reports"] == []
    assert svc.call(api_event(GET, HEAD, path={"report_id": RPT}))["statusCode"] == 200
