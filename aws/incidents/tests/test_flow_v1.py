"""Pruebas de incident-flow T14: CorrelateZone, ExplainCause (Cloud Run + plantilla), WaitForApproval, ReserveParts
y la aprobacion en AssignTechnician. Moto DynamoDB; Cloud Run, SNS, SSM y Secrets Manager con dobles (sin red)."""
import json
import re
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from incidents_support import (
    DEVIATED,
    INC,
    KM_LAT,
    LAT,
    LON,
    NOW_ISO,
    FakeHttp,
    FakeSecrets,
    FakeSns,
    FakeSsm,
    assign,
    assigned,
    created,
    ev_items,
    get_meta,
    get_tech,
    log_lines,
    outbox_events,
    ranked,
    seed_tech,
)

from flow import correlate_zone, explain_cause, render_messages, reserve_parts, wait_for_approval
from fplib import gcp_auth, geohash
from lib import technicians, templates

GH = geohash.encode(LAT, LON, 6)
AUDIENCE = "https://insights-abc123-ue.a.run.app"
SA_KEY = {"type": "service_account", "client_email": "fp-invoker@fleetpulse-g19-2026.iam.gserviceaccount.com",
          "private_key": "-----BEGIN PRIVATE KEY-----\nNO-ES-UNA-LLAVE\n-----END PRIVATE KEY-----\n"}
GEMINI = {
    "hypotheses": [{"text": "Desgaste de la bomba hidraulica principal: presion baja con aceite caliente",
                    "likelihood": 0.7}, "Fuga interna en los sellos de cilindro"],
    "probable_component": "Bomba hidraulica principal",
    "suggested_parts": ["PRT-04", "PRT-12", "PRT-99", "PRT-03", "PRT-04"],
    "confidence": 0.82,
    "common_cause": "Lote de combustible L-2311 en la estacion EST-NORTE",
    "role_messages": {"technician": "t", "supervisor": "s", "management": "m", "operator": "o"},
}
CONTEXT = {"fuel_lots": ["L-2311"], "weather": []}
URL_PARAM = "/fleetpulse/api/url"


@pytest.fixture(autouse=True)
def _caches(monkeypatch):
    gcp_auth.clear_cache()
    monkeypatch.setattr(wait_for_approval, "_url", None)
    yield
    gcp_auth.clear_cache()


def _cell(state, ts, machine_id, gh=GH):
    state.put_item(Item={"PK": f"CELL#{gh}", "SK": f"A#{ts}#{machine_id}", "machine_id": machine_id,
                         "score": Decimal("2.4"), "severity": "schedule", "deviated_vars": ["hydraulic_pressure_bar"]})


def _ctx(state, zone, ts, suffix, **fields):
    context_id = f"CTX-{ts[:4]}{ts[5:7]}{ts[8:10]}T{ts[11:13]}{ts[14:16]}{ts[17:19]}Z-{suffix}"
    state.put_item(Item={"PK": f"CTX#{zone}", "SK": f"{ts}#{context_id}", "context_id": context_id, "ts": ts,
                         "zone_id": zone, **fields})


# --- CorrelateZone -------------------------------------------------------------------------------------------------

def test_correlate_groups_two_machines_same_cell_last_hour(ex017, ddb):
    _cell(ddb.state, "2026-11-15T14:37:59Z", "EX-017")  # la propia maquina
    _cell(ddb.state, "2026-11-15T14:21:00Z", "EX-019")
    _cell(ddb.state, "2026-11-15T14:33:00Z", "EX-019")  # misma vecina dos veces: un solo par
    _cell(ddb.state, "2026-11-15T13:30:00Z", "EX-003")  # hace mas de 1 h
    _cell(ddb.state, "2026-11-15T14:30:00Z", "EX-005", gh=geohash.encode(-13.94, -71.555, 6))  # otra celda
    out = correlate_zone.handler(ex017, state=ddb.state)
    assert re.match(r"^GRP-[0-9a-z]{6}-\d{8}T\d{2}$", out["group_id"])
    assert out["group_id"] == f"GRP-{GH}-20261115T14" and out["peers"] == ["EX-019"]
    # CreateIncident lo guarda y lo publica en incident.opened (valida contra el contrato)
    inc = created({**ex017, "correlation": out}, ddb)["incident"]
    assert inc["group_id"] == out["group_id"] and get_meta(ddb.registry, INC)["group_id"] == out["group_id"]
    (env,) = outbox_events(ddb.outbox, "incident.opened")
    assert env["payload"]["group_id"] == out["group_id"]


def test_correlate_alone_returns_null_group(ex017, ddb):
    _cell(ddb.state, "2026-11-15T14:37:59Z", "EX-017")
    _cell(ddb.state, "2026-11-15T13:30:00Z", "EX-003")
    out = correlate_zone.handler(ex017, state=ddb.state)
    assert out == {"group_id": None, "peers": [], "context": {"fuel_lots": [], "weather": []}}


@pytest.mark.parametrize("zone_id", ["NORTE", ""])  # "" = META escasa: zona de EX-017 en ids.yaml
def test_correlate_collects_fuel_lot_context(ex017, ddb, zone_id):
    _ctx(ddb.state, "NORTE", "2026-11-15T14:10:00Z", "a1b2c3", type="fuel.refill", machine_id="EX-019",
         station_id="EST-NORTE", fuel_lot="L-2311", liters=Decimal("420"))
    _ctx(ddb.state, "NORTE", "2026-11-15T14:00:00Z", "d4e5f6", type="weather", condition="dust",
         intensity=Decimal("0.7"))
    _ctx(ddb.state, "NORTE", "2026-11-15T12:00:00Z", "0a0b0c", type="fuel.refill", machine_id="EX-017",
         station_id="EST-NORTE", fuel_lot="L-2299", liters=Decimal("300"))  # hace mas de 1 h
    _ctx(ddb.state, "SUR", "2026-11-15T14:15:00Z", "1a2b3c", type="fuel.refill", machine_id="TO-001",
         station_id="EST-SUR", fuel_lot="L-2307", liters=Decimal("500"))  # otra zona
    out = correlate_zone.handler({**ex017, "zone_id": zone_id}, state=ddb.state)
    assert out["context"] == {"fuel_lots": ["L-2311"],
                              "weather": [{"ts": "2026-11-15T14:00:00Z", "condition": "dust", "intensity": 0.7}]}
    json.dumps(out)  # Step Functions recibe JSON: nada de Decimal


# --- ExplainCause --------------------------------------------------------------------------------------------------

@pytest.fixture
def invoker(monkeypatch):
    """Secreto fleetpulse/gcp/sa-invoker y un ID token fijo (fplib.gcp_auth tiene su propia prueba en aws/shared)."""
    calls = []

    def fake_id_token(sa_key, audience, now=None):
        calls.append((sa_key["client_email"], audience))
        return "id-tok-123"

    monkeypatch.setattr(gcp_auth, "id_token", fake_id_token)
    secrets = FakeSecrets({"fleetpulse/gcp/sa-invoker": {"key": SA_KEY, "audience": AUDIENCE + "/"}})
    return SimpleNamespace(secrets=secrets, token_calls=calls)


def _explain_input(ex017, ddb, group_id=None):
    return created({**ex017, "correlation": {"group_id": group_id, "peers": [], "context": CONTEXT}}, ddb)


def test_explain_uses_cloud_run_json(ex017, ddb, invoker, caplog):
    http = FakeHttp((200, json.dumps(GEMINI).encode()))
    ev = _explain_input(ex017, ddb)
    with caplog.at_level("INFO", logger="fplib"):
        out = explain_cause.handler(ev, secrets=invoker.secrets, http=http)
    assert out == {
        "source": "gemini",
        "summary": "Desgaste de la bomba hidraulica principal: presion baja con aceite caliente; "
                   "Fuga interna en los sellos de cilindro",
        "probable_component": "Bomba hidraulica principal",
        "suggested_parts": ["PRT-04", "PRT-03"],  # solo repuestos del BOM del modelo EX, sin repetidos
        "common_cause": None,  # no esta agrupado
    }
    (call,) = http.calls
    assert call["url"] == f"{AUDIENCE}/explain" and call["timeout"] == 8
    assert call["headers"]["Authorization"] == "Bearer id-tok-123"
    assert call["body"] == {"incident_id": INC, "machine_id": "EX-017", "model": "EX", "severity": "schedule",
                            "deviated_vars": DEVIATED, "failure_mode": "hydraulic_pump", "group_id": None,
                            "context": CONTEXT, "recent_incidents": []}
    assert invoker.token_calls == [(SA_KEY["client_email"], AUDIENCE)]
    assert "id-tok-123" not in caplog.text and "PRIVATE KEY" not in caplog.text
    # AssignTechnician la guarda tal cual y incident.assigned valida contra el contrato
    seed_tech(ddb.state, "TEC-01", "hydraulics", LAT + 0.4 * KM_LAT, LON)
    rk = ranked({**ex017, "correlation": ev["correlation"]}, ddb)
    assign({**rk, "explanation": out}, ddb)
    (env,) = outbox_events(ddb.outbox, "incident.assigned")
    assert env["payload"]["explanation"] == out


def test_explain_keeps_common_cause_only_when_grouped(ex017, ddb, invoker):
    group = f"GRP-{GH}-20261115T14"
    out = explain_cause.handler(_explain_input(ex017, ddb, group), secrets=invoker.secrets,
                                http=FakeHttp((200, json.dumps(GEMINI).encode())))
    assert out["source"] == "gemini" and out["common_cause"] == GEMINI["common_cause"]


def test_explain_falls_back_on_timeout(ex017, ddb, invoker, caplog):
    ev = _explain_input(ex017, ddb)
    with caplog.at_level("INFO", logger="fplib"):
        out = explain_cause.handler(ev, secrets=invoker.secrets, http=FakeHttp(TimeoutError("timed out")))
    assert out == templates.explanation(DEVIATED, "hydraulic_pump", "EX-017") and out["source"] == "template"
    (line,) = log_lines(caplog, "explain_fallback")
    assert line["reason"] == "TimeoutError" and line["incident_id"] == INC and line["machine_id"] == "EX-017"


@pytest.mark.parametrize("status, body, reason", [
    (200, b"<html>502 Bad Gateway</html>", "json invalido"),
    (200, json.dumps(["no", "objeto"]).encode(), "respuesta no es un objeto"),
    (200, json.dumps({"probable_component": "Bomba"}).encode(), "faltan campos: hypotheses,suggested_parts"),
    (200, json.dumps({**GEMINI, "hypotheses": []}).encode(), "sin hipotesis"),
    (503, json.dumps(GEMINI).encode(), "http 503"),
])
def test_explain_falls_back_on_bad_json(ex017, ddb, invoker, caplog, status, body, reason):
    with caplog.at_level("INFO", logger="fplib"):
        out = explain_cause.handler(_explain_input(ex017, ddb), secrets=invoker.secrets, http=FakeHttp((status, body)))
    assert out == templates.explanation(DEVIATED, "hydraulic_pump", "EX-017")
    (line,) = log_lines(caplog, "explain_fallback")
    assert line["reason"] == reason


def test_explain_falls_back_when_the_token_cannot_be_obtained(ex017, ddb, caplog):
    secrets = FakeSecrets({"fleetpulse/gcp/sa-invoker": {"key": SA_KEY, "audience": AUDIENCE}})
    http = FakeHttp((200, json.dumps(GEMINI).encode()))
    with caplog.at_level("INFO", logger="fplib"):
        out = explain_cause.handler(_explain_input(ex017, ddb), secrets=secrets, http=http)  # llave invalida
    assert out["source"] == "template" and http.calls == []
    (line,) = log_lines(caplog, "explain_fallback")
    assert line["reason"] and "PRIVATE KEY" not in caplog.text and "NO-ES-UNA-LLAVE" not in caplog.text


# --- WaitForApproval -----------------------------------------------------------------------------------------------

def test_wait_for_approval_mails_the_approve_link_to_the_supervisor(ex017, ddb, caplog):
    seed_tech(ddb.state, "TEC-01", "hydraulics", LAT + 0.4 * KM_LAT, LON)
    ev = ranked(ex017, ddb)  # sin $.explanation (ExplainCause cayo en su Catch): usa la plantilla
    token = "AAAAKgAAAAIAAAAAAAAAAQ+6C4y/7ZgXt=="
    sns, ssm = FakeSns(), FakeSsm({URL_PARAM: "https://abc123.execute-api.us-east-1.amazonaws.com/"})
    with caplog.at_level("INFO", logger="fplib"):
        assert wait_for_approval.handler({"input": ev, "token": token}, sns=sns, ssm=ssm) is None
    (call,) = sns.calls
    assert call["TopicArn"] == "arn:aws:sns:us-east-1:000000000000:fp-notify"
    assert call["MessageAttributes"] == {"role": {"DataType": "String", "StringValue": "supervisor"}}
    assert call["Subject"] == f"[FleetPulse] Aprobar schedule EX-017 {INC}" and len(call["Subject"]) <= 100
    link = (f"https://abc123.execute-api.us-east-1.amazonaws.com/approvals/{quote(token, safe='')}"
            f"?tech=TEC-01&inc={INC}")
    assert "%2F" in link and "%2B" in link and "%3D" in link
    msg = call["Message"]
    assert link in msg and INC in msg and "EX-017" in msg and "schedule" in msg and "TEC-01" in msg
    assert "30 minutos" in msg and "hydraulic_pressure_bar" in msg  # diagnostico de la plantilla
    assert token not in caplog.text and quote(token, safe="") not in caplog.text  # el token es una credencial
    (line,) = log_lines(caplog, "aprobacion_solicitada")
    assert line["tech_id"] == "TEC-01" and line["incident_id"] == INC and line["machine_id"] == "EX-017"
    wait_for_approval.handler({"input": ev, "token": "otro"}, sns=sns, ssm=ssm)
    assert ssm.calls == [URL_PARAM]  # la URL de la API se lee una vez


# --- AssignTechnician con aprobacion ------------------------------------------------------------------------------

def _two_hydraulic_techs(ddb, **tec02):
    seed_tech(ddb.state, "TEC-01", "hydraulics", LAT + 0.4 * KM_LAT, LON)
    seed_tech(ddb.state, "TEC-02", "hydraulics", LAT + 1.5 * KM_LAT, LON, **tec02)


def test_assign_uses_approved_tech_and_approval_flag(ex017, ddb):
    _two_hydraulic_techs(ddb)
    ev = ranked(ex017, ddb)
    assert ev["ranking"]["top"][0]["tech_id"] == "TEC-01"
    out = assign({**ev, "approval": {"approval": "approved", "tech_id": "TEC-02"}}, ddb)
    assert out == {"tech_id": "TEC-02", "assigned_at": NOW_ISO, "approval": "approved", "tech_busy": False,
                   "severity": "schedule"}
    meta = get_meta(ddb.registry, INC)
    assert meta["tech_id"] == "TEC-02" and meta["approval"] == "approved"
    assert get_tech(ddb.state, "TEC-02")["current_incident"] == INC and get_tech(ddb.state, "TEC-01")["available"]
    (env,) = outbox_events(ddb.outbox, "incident.assigned")
    assert env["payload"]["tech_id"] == "TEC-02" and env["payload"]["approval"] == "approved"


def test_assign_after_approval_timeout_takes_the_ranking(ex017, ddb):
    _two_hydraulic_techs(ddb)
    out = assign({**ranked(ex017, ddb), "approval": {"approval": "timeout", "tech_id": None}}, ddb)
    assert out["tech_id"] == "TEC-01" and out["approval"] == "timeout" and out["tech_busy"] is False
    (env,) = outbox_events(ddb.outbox, "incident.assigned")
    assert env["payload"]["approval"] == "timeout"


def test_assign_and_render_use_the_severity_escalated_while_waiting(ex017, ddb):
    """S1: el incidente se clasifica schedule y espera la aprobacion; otra ejecucion lo escala a immediate. Al
    reanudar, los avisos dicen immediate y el operador recibe la orden de detener la maquina."""
    _two_hydraulic_techs(ddb)
    ev = ranked(ex017, ddb)
    ddb.registry.update_item(Key={"PK": f"INCIDENT#{INC}", "SK": "META"}, UpdateExpression="SET severity = :s",
                             ExpressionAttributeValues={":s": "immediate"})  # lo que hace CheckExisting
    ev = {**ev, "approval": {"approval": "approved", "tech_id": "TEC-01"}}
    ev["assignment"] = assign(ev, ddb)
    assert ev["assignment"]["severity"] == "immediate" and ev["classified"]["severity"] == "schedule"
    msgs = render_messages.handler(ev)
    assert msgs["subject"] == f"[FleetPulse] immediate EX-017 {INC}"
    assert msgs["operator"] == templates.operator_message("EX-017", "immediate")


def test_assign_approved_tech_already_busy_takes_next_free_and_stays_approved(ex017, ddb, caplog):
    other = "INC-EX-019-20261115T1430"
    _two_hydraulic_techs(ddb, available=False, current_incident=other)
    ev = {**ranked(ex017, ddb), "approval": {"approval": "approved", "tech_id": "TEC-02"}}
    with caplog.at_level("INFO", logger="fplib"):
        out = assign(ev, ddb)
    assert out["tech_id"] == "TEC-01" and out["approval"] == "approved" and out["tech_busy"] is False
    assert get_tech(ddb.state, "TEC-02")["current_incident"] == other
    (line,) = log_lines(caplog, "tecnico_aprobado_ocupado")
    assert line["tech_id"] == "TEC-02" and line["incident_id"] == INC


def test_assign_approved_tech_taken_after_the_read_takes_next_free(ex017, ddb, monkeypatch):
    """Lectura vieja: TEC-02 se leyo libre pero otra ejecucion lo reservo antes de la transaccion."""
    _two_hydraulic_techs(ddb)
    ev = {**ranked(ex017, ddb), "approval": {"approval": "approved", "tech_id": "TEC-02"}}
    stale, real_get = get_tech(ddb.state, "TEC-02"), technicians.get
    other = "INC-EX-019-20261115T1430"
    ddb.state.update_item(Key={"PK": "TECH#TEC-02", "SK": "META"},
                          UpdateExpression="SET available = :f, current_incident = :i",
                          ExpressionAttributeValues={":f": False, ":i": other})
    monkeypatch.setattr(technicians, "get", lambda state, tid: stale if tid == "TEC-02" else real_get(state, tid))
    out = assign(ev, ddb)
    assert out["tech_id"] == "TEC-01" and out["approval"] == "approved" and out["tech_busy"] is False
    assert get_tech(ddb.state, "TEC-02")["current_incident"] == other  # no se piso la reserva ajena
    assert get_tech(ddb.state, "TEC-01")["current_incident"] == INC
    assert len(outbox_events(ddb.outbox, "incident.assigned")) == 1


# --- ReserveParts --------------------------------------------------------------------------------------------------

def _reserve(ev, ddb):
    return reserve_parts.handler(ev, registry=ddb.registry, outbox=ddb.outbox)


def test_reserve_parts_emits_requested_from_bom(ex017, ddb):
    seed_tech(ddb.state, "TEC-01", "hydraulics", LAT + 0.4 * KM_LAT, LON)
    ev = assigned(ex017, ddb)
    lines = [{"part_id": "PRT-04", "qty": 1}, {"part_id": "PRT-03", "qty": 2}]  # BOM EX / hydraulic_pump
    assert _reserve(ev, ddb) == {"requested": lines}
    (env,) = outbox_events(ddb.outbox, "part.reserve.requested", contract="part")
    assert env["aggregate_id"] == "RSV-" + INC and env["aggregate_type"] == "part_reservation"
    assert env["source"] == "aws.incident-flow" and env["incident_id"] == INC and env["machine_id"] == "EX-017"
    assert env["payload"] == {"zone_id": "NORTE", "failure_mode": "hydraulic_pump", "lines": lines}
    (item,) = [i for i in ev_items(ddb.registry, INC) if i["event_type"] == "part.reserve.requested"]
    assert json.loads(item["payload"]) == env
    # reintento del paso: misma respuesta y ningun segundo pedido (registry reservaria dos veces)
    assert _reserve(ev, ddb) == {"requested": lines}
    assert len(outbox_events(ddb.outbox, "part.reserve.requested", contract="part")) == 1


@pytest.mark.parametrize("suggested, lines", [
    (["PRT-07", "PRT-07"], [{"part_id": "PRT-07", "qty": 1}]),
    ([], []),
])
def test_reserve_parts_without_bom_uses_suggested_parts_or_skips(ex017, ddb, suggested, lines):
    ev = assigned({**ex017, "failure_mode": "final_drive"}, ddb)  # EX no tiene BOM para final_drive
    explanation = {**templates.explanation(DEVIATED, "final_drive", "EX-017"), "suggested_parts": suggested}
    assert _reserve({**ev, "explanation": explanation}, ddb) == {"requested": lines}
    envs = outbox_events(ddb.outbox, "part.reserve.requested", contract="part")
    assert [e["payload"]["lines"] for e in envs] == ([lines] if lines else [])
