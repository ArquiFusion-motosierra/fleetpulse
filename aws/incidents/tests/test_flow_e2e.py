"""Punta a punta de incident-flow (T14): S1 y S2 del documento de diseno corren por la ASL real
(`tests/incidents_asl.py`) con las Lambdas reales, moto DynamoDB y dobles de SNS, SSM, Secrets Manager y Cloud Run.
La aprobacion entra por `api/approvals` con el enlace del correo y el cierre por `api/close`, como en vivo.

S1: EX-017 (bomba hidraulica gastada) cruza `schedule` (espera la aprobacion por correo) y luego `immediate` (escala
el incidente abierto). Tecnico de hidraulica, reserva de bomba y sellos, cierre que libera al tecnico.
S2: EX-003, VQ-011, VQ-014 y TO-002 (combustible contaminado) en la misma celda y hora: un solo grupo, Gemini
apunta al lote L-2311 y direccion recibe el aviso de cada uno.
"""
import json
import re
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from incidents_asl import Sns, StateMachine
from incidents_support import (
    DEVIATED,
    INC,
    KM_LAT,
    LAT,
    LON,
    NOW,
    FakeSecrets,
    FakeSsm,
    ev_items,
    get_meta,
    get_tech,
    outbox_events,
    seed_tech,
)

from api import approvals, close
from flow import (
    assign_technician,
    check_existing,
    classify,
    correlate_zone,
    create_incident,
    explain_cause,
    rank_technicians,
    render_messages,
    reserve_parts,
    wait_for_approval,
)
from fplib import clock, geohash, ids

API = "https://abc123.execute-api.us-east-1.amazonaws.com"
AUDIENCE = "https://insights-abc123-ue.a.run.app"
CONSOLE_KEY = "console-key-123"
GH = geohash.encode(LAT, LON, 6)
GROUP = f"GRP-{GH}-20261115T14"
S2_MACHINES = ("EX-003", "VQ-011", "VQ-014", "TO-002")
FUEL_DEVIATED = [{"var": "fuel_rate_lph", "value": 41.2, "z": 2.9}, {"var": "engine_rpm", "value": 1710.0, "z": 1.6},
                 {"var": "coolant_temp_c", "value": 94.1, "z": 1.4}]
APPROVAL_PATH = ["ClassifySeverity", "CheckExistingIncident", "AlreadyOpen?", "CorrelateZone", "CreateIncident",
                 "ExplainCause", "RankTechnicians", "NeedsApproval", "WaitForApproval"]
AFTER_APPROVAL = ["AssignTechnician", "IncidentClosed?", "RenderMessages", "NotifyByRole", "ReserveParts", "Done"]
DIRECT_PATH = APPROVAL_PATH[:-1] + AFTER_APPROVAL


def _gemini(url, body, headers, timeout):
    """Cloud Run `insights /explain`: con L-2311 en el contexto de un incidente agrupado, la causa comun."""
    req = json.loads(body)
    if req["failure_mode"] == "hydraulic_pump":
        out = {"hypotheses": ["Desgaste de la bomba hidraulica principal", "Fuga interna en sellos"],
               "probable_component": "Bomba hidraulica principal", "suggested_parts": ["PRT-04", "PRT-03"],
               "confidence": 0.82}
    else:
        out = {"hypotheses": ["Combustible contaminado: consumo alto y rpm inestable"],
               "probable_component": "Filtro de combustible e inyectores", "suggested_parts": ["PRT-01", "PRT-05"],
               "confidence": 0.77}
        if "L-2311" in req["context"].get("fuel_lots", []):
            out["common_cause"] = "Lote de combustible L-2311 cargado en la estacion EST-NORTE"
    return 200, json.dumps(out).encode()


@pytest.fixture
def world(ddb, monkeypatch):
    """incident-flow armado como en vivo: los seis tecnicos de ids.yaml en el centroide de su zona, secretos de la
    API y de Cloud Run, y la maquina de estados que tambien hace de cliente de Step Functions para las APIs."""
    zones = ids.zones()
    for t in ids.technicians():
        z = zones[t["zone_id"]]
        seed_tech(ddb.state, t["tech_id"], t["specialty"], z["lat"], z["lon"], zone=t["zone_id"])
    monkeypatch.setattr(explain_cause.gcp_auth, "id_token", lambda key, audience, now=None: "id-tok-123")
    monkeypatch.setattr(wait_for_approval, "_url", None)
    secrets = FakeSecrets({"fleetpulse/gcp/sa-invoker": {"key": {"client_email": "x"}, "audience": AUDIENCE},
                           "fleetpulse/api/console-key": {"api_key": CONSOLE_KEY},
                           "fleetpulse/api/ingest-key": {"api_key": "ingest-key-456"}})
    sns, ssm = Sns(), FakeSsm({"/fleetpulse/api/url": API + "/"})
    w = SimpleNamespace(ddb=ddb, sns=sns, secrets=secrets, http=_gemini)
    tables = {"registry": ddb.registry, "outbox": ddb.outbox}
    lambdas = {
        "ClassifyArn": classify.handler,
        "CheckExistingArn": lambda e: check_existing.handler(e, **tables),
        "CorrelateZoneArn": lambda e: correlate_zone.handler(e, state=ddb.state),
        "CreateIncidentArn": lambda e: create_incident.handler(e, **tables),
        "ExplainCauseArn": lambda e: explain_cause.handler(e, secrets=secrets, http=lambda *a: w.http(*a)),
        "RankTechniciansArn": lambda e: rank_technicians.handler(e, state=ddb.state),
        "WaitForApprovalArn": lambda e: wait_for_approval.handler(e, sns=sns, ssm=ssm),
        "RenderMessagesArn": render_messages.handler,
        "AssignTechnicianArn": lambda e: assign_technician.handler(e, state=ddb.state, **tables),
        "ReservePartsArn": lambda e: reserve_parts.handler(e, **tables),
    }
    w.sfn = StateMachine(lambdas, sns)
    return w


def _at(monkeypatch, minutes: float):
    monkeypatch.setattr(clock, "now", lambda: NOW + timedelta(minutes=minutes))


def _mails(world, incident_id, role=None):
    return [c for c in world.sns.calls if incident_id in c["Subject"]
            and (role is None or c["MessageAttributes"]["role"]["StringValue"] == role)]


def _click_approval(world, incident_id, method="GET"):
    """El supervisor abre el enlace del correo de WaitForApproval; API Gateway lo entrega a `api/approvals`."""
    (mail,) = [c for c in _mails(world, incident_id, "supervisor") if c["Subject"].startswith("[FleetPulse] Aprobar")]
    (link,) = re.findall(r"https://\S+/approvals/\S+", mail["Message"])
    url = urlsplit(link)
    assert f"{url.scheme}://{url.netloc}" == API
    token = url.path.removeprefix("/approvals/")
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    event = {"version": "2.0", "rawPath": url.path, "headers": {}, "queryStringParameters": query,
             "pathParameters": {"token": token}, "requestContext": {"http": {"method": method, "path": url.path}},
             "body": None, "isBase64Encoded": False}
    return approvals.handler(event, sfn=world.sfn)


def _close(world, incident_id, closed_by, parts_used):
    body = {"closed_by": closed_by, "parts_used": parts_used, "cause_summary": "Reparado"}
    event = {"version": "2.0", "rawPath": f"/incidents/{incident_id}/close", "headers": {"x-api-key": CONSOLE_KEY},
             "pathParameters": {"incident_id": incident_id}, "queryStringParameters": None,
             "requestContext": {"http": {"method": "POST"}}, "body": json.dumps(body), "isBase64Encoded": False}
    ddb = world.ddb
    return close.handler(event, state=ddb.state, registry=ddb.registry, outbox=ddb.outbox, secrets=world.secrets,
                         sfn=world.sfn)


def _envs(world, event_type, incident_id, contract="incident"):
    return [e for e in outbox_events(world.ddb.outbox, event_type, contract) if e["incident_id"] == incident_id]


# --- S1: bomba hidraulica gastada en EX-017 ------------------------------------------------------------------------

def test_s1_schedule_waits_for_email_approval_escalates_then_assigns_reserves_and_closes(world, ex017, monkeypatch):
    sfn, ddb = world.sfn, world.ddb
    # minuto 8: cruza schedule -> incidente abierto y correo de aprobacion al supervisor
    ex = sfn.start(ex017)
    assert ex.status == "WAITING" and ex.visited == APPROVAL_PATH
    meta = get_meta(ddb.registry, INC)
    assert meta["status"] == "open" and meta["severity"] == "schedule" and "tech_id" not in meta
    (opened,) = _envs(world, "incident.opened", INC)
    assert opened["payload"]["group_id"] is None and opened["payload"]["deviated_vars"] == DEVIATED
    (ask,) = _mails(world, INC, "supervisor")
    assert "TEC-01" in ask["Message"] and "Desgaste de la bomba hidraulica" in ask["Message"]  # hidraulica y explicacion de Gemini
    assert get_tech(ddb.state, "TEC-01")["available"] is True  # nadie se reserva antes de aprobar

    # minuto 15: cruza immediate -> la nueva ejecucion escala el incidente abierto y avisa; no abre otro
    _at(monkeypatch, 7)
    worse = {**ex017, "incident_id": "INC-EX-017-20261115T1445", "score": 3.62, "severity": "immediate"}
    esc = sfn.start(worse)
    assert esc.status == "SUCCEEDED"
    assert esc.visited[-3:] == ["Escalated?", "NotifyEscalation", "EscalatedOrIgnored"]
    assert get_meta(ddb.registry, worse["incident_id"]) is None
    assert get_meta(ddb.registry, INC)["severity"] == "immediate"
    (escalated,) = _envs(world, "incident.escalated", INC)
    assert escalated["payload"] == {"from_severity": "schedule", "to_severity": "immediate", "score": 3.62}
    roles = {c["MessageAttributes"]["role"]["StringValue"] for c in world.sns.calls if "escalado" in c["Subject"]}
    assert roles == {"supervisor", "management", "operator"}
    (stop,) = [c for c in world.sns.calls if "escalado" in c["Subject"] and
               c["MessageAttributes"]["role"]["StringValue"] == "operator"]
    assert stop["Message"] == "EX-017: detenga la maquina en un lugar seguro y espere al tecnico."
    assert ex.status == "WAITING"

    # minuto 17: el supervisor hace clic en el enlace -> SendTaskSuccess reanuda la ejecucion
    _at(monkeypatch, 9)
    resp = _click_approval(world, INC)
    assert resp["statusCode"] == 200 and f"Aprobado: TEC-01 asignado a {INC}" in resp["body"]
    assert ex.status == "SUCCEEDED" and ex.visited == APPROVAL_PATH + AFTER_APPROVAL
    tech = get_tech(ddb.state, "TEC-01")
    assert tech["available"] is False and tech["current_incident"] == INC
    (assigned,) = _envs(world, "incident.assigned", INC)
    assert assigned["payload"]["tech_id"] == "TEC-01" and assigned["payload"]["approval"] == "approved"
    assert assigned["payload"]["explanation"]["source"] == "gemini"
    # los avisos salen con la severidad vigente (immediate), no con la del cruce: el operador detiene la maquina
    notified = {c["MessageAttributes"]["role"]["StringValue"]: c for c in _mails(world, INC)
                if c["Subject"].startswith("[FleetPulse] immediate")}
    assert set(notified) == {"technician", "supervisor", "management", "operator"}
    assert notified["operator"]["Message"] == "EX-017: detenga la maquina en un lugar seguro y espere al tecnico."
    assert "Asignado a: TEC-01" in notified["technician"]["Message"]
    assert "aprobacion approved" in notified["supervisor"]["Message"]
    # reserva de bomba y sellos (BOM de EX para hydraulic_pump)
    (rsv,) = _envs(world, "part.reserve.requested", INC, contract="part")
    assert rsv["aggregate_id"] == f"RSV-{INC}"
    assert rsv["payload"]["lines"] == [{"part_id": "PRT-04", "qty": 1}, {"part_id": "PRT-03", "qty": 2}]
    assert ex.output["reservation"] == {"requested": rsv["payload"]["lines"]}
    # doble clic: ya procesada, la ejecucion no cambia
    assert "ya fue procesada" in _click_approval(world, INC)["body"]

    # el tecnico cierra desde la consola: libera al tecnico y emite incident.closed
    _at(monkeypatch, 87)
    resp = _close(world, INC, "TEC-01", [{"part_id": "PRT-04", "zone_id": "NORTE", "qty": 1}])
    assert resp["statusCode"] == 200 and json.loads(resp["body"])["downtime_h"] == 1.45
    tech = get_tech(ddb.state, "TEC-01")
    assert tech["available"] is True and tech["current_incident"] is None
    (closed,) = _envs(world, "incident.closed", INC)
    assert closed["payload"]["parts_used"] == [{"part_id": "PRT-04", "zone_id": "NORTE", "qty": 1}]
    meta = get_meta(ddb.registry, INC)
    assert meta["status"] == "closed" and meta["GSI2PK"] == "STATUS#closed"
    assert sfn.stops == [ex.arn] and ex.status == "SUCCEEDED"  # StopExecution sobre una ejecucion ya terminada
    # linea de tiempo de la consola (assigned y reserve.requested comparten el instante)
    assert sorted(i["event_type"] for i in ev_items(ddb.registry, INC)) == [
        "incident.assigned", "incident.closed", "incident.escalated", "incident.opened", "part.reserve.requested"]

    # la misma falla al dia siguiente: incidente nuevo, marcado como recurrente
    _at(monkeypatch, 60 * 24)
    again = sfn.start({**ex017, "incident_id": "INC-EX-017-20261116T1430", "score": 3.7})
    assert again.status == "SUCCEEDED" and again.visited == DIRECT_PATH
    assert again.output["incident"]["recurrence_flag"] is True


def test_s1_approval_timeout_assigns_the_best_ranked_tech(world, ex017):
    ex = world.sfn.start(ex017)
    ex.time_out()  # nadie aprueba en 30 minutos
    assert ex.status == "SUCCEEDED"
    assert ex.visited == APPROVAL_PATH + ["ApprovalTimedOut"] + AFTER_APPROVAL
    (assigned,) = _envs(world, "incident.assigned", INC)
    assert assigned["payload"]["tech_id"] == "TEC-01" and assigned["payload"]["approval"] == "timeout"
    assert "ya fue procesada" in _click_approval(world, INC)["body"]  # el enlace vencio


def test_s1_approval_mail_failing_assigns_without_approval(world, ex017):
    world.sns.fail_on = lambda call: call["Subject"].startswith("[FleetPulse] Aprobar")
    ex = world.sfn.start(ex017)
    assert ex.status == "SUCCEEDED" and ex.invocations["WaitForApprovalArn"] == 4  # 1 + 3 reintentos
    assert ex.visited == APPROVAL_PATH + ["ApprovalUnavailable"] + AFTER_APPROVAL
    (assigned,) = _envs(world, "incident.assigned", INC)
    assert assigned["payload"]["approval"] == "auto"


def test_s1_closed_from_the_console_while_waiting_stops_the_execution(world, ex017):
    ex = world.sfn.start(ex017)
    assert ex.status == "WAITING"
    resp = _close(world, INC, "supervisor@example.com", [])
    assert resp["statusCode"] == 200 and ex.status == "ABORTED" and world.sfn.stops == [ex.arn]
    assert "ya fue procesada" in _click_approval(world, INC)["body"]
    assert all(get_tech(world.ddb.state, t["tech_id"])["available"] is True for t in ids.technicians())
    assert _envs(world, "incident.assigned", INC) == []
    assert _envs(world, "part.reserve.requested", INC, contract="part") == []
    (closed,) = _envs(world, "incident.closed", INC)
    assert closed["payload"]["closed_by"] == "supervisor@example.com"


def test_s1_cloud_run_down_uses_the_template_and_still_assigns(world, ex017):
    world.http = lambda *a: (503, b"Service Unavailable")
    ex = world.sfn.start({**ex017, "score": 3.7})  # immediate: sin aprobacion
    assert ex.status == "SUCCEEDED" and ex.visited == DIRECT_PATH
    (assigned,) = _envs(world, "incident.assigned", INC)
    assert assigned["payload"]["explanation"]["source"] == "template"


def test_a_required_step_failing_alerts_and_fails_the_execution(world, ex017):
    ex = world.sfn.start({**ex017, "deviated_vars": [], "z": {}})  # CreateIncident no puede abrir el incidente
    assert ex.status == "FAILED" and ex.error == "IncidentFlowFailed"
    assert ex.invocations["CreateIncidentArn"] == 4 and ex.visited[-3:] == ["CreateIncident", "PublishAlert",
                                                                           "Failed"]
    (alert,) = [c for c in world.sns.calls if c["TopicArn"].endswith(":fp-alerts")]
    assert alert["Subject"] == f"[FleetPulse] incident-flow fallo {INC}"
    assert alert["Message"].startswith(f"incident-flow {INC} fallo: ValueError - ")


# --- S2: combustible contaminado en cuatro maquinas de la zona norte ------------------------------------------------

def _s2_input(machine_id, score, k):
    lat, lon = LAT + 0.05 * k * KM_LAT, LON + 0.0003 * k  # a pocos metros: misma celda geohash6
    gh = geohash.encode(lat, lon, 6)
    assert gh == GH
    model = machine_id.split("-")[0]
    return {"incident_id": f"INC-{machine_id}-20261115T1430", "machine_id": machine_id, "model": model,
            "serial": "", "zone_id": "NORTE", "lat": lat, "lon": lon, "geohash6": gh, "ts": "2026-11-15T14:37:59Z",
            "batch_id": "B-20261115T143759Z-5e6f7a8b", "score": score,
            "severity": classify.severity(score), "z": {d["var"]: {"self": d["z"], "peer": None} for d in FUEL_DEVIATED},
            "deviated_vars": [dict(d) for d in FUEL_DEVIATED], "failure_mode": "fuel_contamination",
            "hours_total": 5000}


def test_s2_four_machines_same_cell_are_grouped_with_the_common_fuel_lot(world, monkeypatch):
    sfn, ddb = world.sfn, world.ddb
    # lo que deja aws/scoring (CELL# de cada anomalia) y POST /context del simulador (recarga con L-2311 al inicio)
    for k, mid in enumerate(S2_MACHINES):
        ddb.state.put_item(Item={"PK": f"CELL#{GH}", "SK": f"A#2026-11-15T14:3{k}:00Z#{mid}", "machine_id": mid,
                                 "score": Decimal("2.4"), "severity": "schedule", "deviated_vars": ["fuel_rate_lph"]})
        ddb.state.put_item(Item={"PK": "CTX#NORTE", "SK": f"2026-11-15T14:0{k}:00Z#CTX-{k}", "type": "fuel.refill",
                                 "ts": f"2026-11-15T14:0{k}:00Z", "zone_id": "NORTE", "machine_id": mid,
                                 "station_id": "EST-NORTE", "fuel_lot": "L-2311", "liters": Decimal("320")})
    scores = {"EX-003": 3.8, "VQ-011": 3.6, "VQ-014": 2.4, "TO-002": 2.2}
    runs = {mid: sfn.start(_s2_input(mid, scores[mid], k)) for k, mid in enumerate(S2_MACHINES)}
    assert [runs[m].status for m in S2_MACHINES] == ["SUCCEEDED", "SUCCEEDED", "WAITING", "WAITING"]
    assert _click_approval(world, runs["VQ-014"].name)["statusCode"] == 200
    runs["TO-002"].time_out()
    assert all(r.status == "SUCCEEDED" for r in runs.values())

    techs, approvals_seen = set(), {}
    for mid, ex in runs.items():
        inc = ex.name
        (opened,) = _envs(world, "incident.opened", inc)
        assert opened["payload"]["group_id"] == GROUP  # las cuatro en el mismo grupo
        (assigned,) = _envs(world, "incident.assigned", inc)
        explanation = assigned["payload"]["explanation"]
        assert explanation["source"] == "gemini" and "L-2311" in explanation["common_cause"]
        techs.add(assigned["payload"]["tech_id"])
        approvals_seen[mid] = assigned["payload"]["approval"]
        assert get_tech(ddb.state, assigned["payload"]["tech_id"])["current_incident"] == inc
        (mgmt,) = [c for c in _mails(world, inc, "management") if "escalado" not in c["Subject"]]
        assert GROUP in mgmt["Message"]  # agrupado: direccion se entera aunque sea schedule
        assert f"Agrupado con otras maquinas de la zona: {GROUP}" in _mails(world, inc, "supervisor")[-1]["Message"]
        (rsv,) = _envs(world, "part.reserve.requested", inc, contract="part")
        assert rsv["payload"]["lines"] == [{"part_id": "PRT-01", "qty": 1}, {"part_id": "PRT-05", "qty": 1}]
    assert len(techs) == 4  # cada incidente con su propio tecnico, ninguno pisado
    assert approvals_seen == {"EX-003": "auto", "VQ-011": "auto", "VQ-014": "approved", "TO-002": "timeout"}
    first = _envs(world, "incident.assigned", runs["EX-003"].name)[0]
    assert first["payload"]["tech_id"] == "TEC-03"  # motor, zona norte

    for ex in runs.values():
        tech_id = _envs(world, "incident.assigned", ex.name)[0]["payload"]["tech_id"]
        assert _close(world, ex.name, tech_id, [{"part_id": "PRT-01", "zone_id": "NORTE", "qty": 1}])[
            "statusCode"] == 200
    assert all(get_tech(ddb.state, t["tech_id"])["available"] is True for t in ids.technicians())
    assert len(outbox_events(ddb.outbox, "incident.closed")) == 4


# --- scripts/smoke_incidents.py: lo que arma el smoke en vivo corre igual por el flujo ------------------------------

def _smoke():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[3] / "scripts" / "smoke_incidents.py"
    spec = importlib.util.spec_from_file_location("smoke_incidents", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _history(ex, *, exited=False):
    """Eventos de get_execution_history que deja un WaitForApproval (forma real de Step Functions)."""
    params = {"FunctionName": "arn:aws:lambda:us-east-1:000000000000:function:fp-incidents-dev-waitForApproval",
              "Payload": {"input": ex._waiting[2], "token": ex.token}}
    events = [{"type": "TaskStateEntered", "stateEnteredEventDetails": {"name": "WaitForApproval"}},
              {"type": "TaskScheduled", "taskScheduledEventDetails": {
                  "resourceType": "lambda", "resource": "invoke.waitForTaskToken", "region": "us-east-1",
                  "parameters": json.dumps(params)}},
              {"type": "TaskStarted", "taskStartedEventDetails": {"resourceType": "lambda",
                                                                  "resource": "invoke.waitForTaskToken"}}]
    before_submit = list(events)
    events.append({"type": "TaskSubmitted", "taskSubmittedEventDetails": {"resourceType": "lambda",
                                                                          "resource": "invoke.waitForTaskToken"}})
    if exited:
        events.append({"type": "TaskStateExited", "stateExitedEventDetails": {"name": "WaitForApproval"}})
    return before_submit, events


def test_smoke_s1_input_waits_and_its_approval_link_is_the_one_in_the_mail(world, monkeypatch):
    smoke = _smoke()
    _at(monkeypatch, 0)
    pos = {"zone_id": "NORTE", "lat": LAT, "lon": LON}
    ex = world.sfn.start(smoke.build_input("EX-017", pos, 2.4, "hydraulic_pump", smoke.S1, NOW))
    assert ex.name == INC and ex.status == "WAITING"
    before_submit, history = _history(ex)
    assert smoke.waiting_token(before_submit) is None  # el correo aun no salio
    tok = smoke.waiting_token(history)
    assert tok == {"token": ex.token, "tech_id": "TEC-01"}
    (mail,) = _mails(world, INC, "supervisor")
    assert smoke.approval_link(API, tok["token"], tok["tech_id"], INC) in mail["Message"]
    assert smoke.waiting_token(_history(ex, exited=True)[1]) is None
    # la ejecucion que escala usa la ventana siguiente: otro nombre, mismo incidente abierto
    worse = smoke.build_input("EX-017", pos, 3.7, "hydraulic_pump", smoke.S1, NOW, window=NOW + timedelta(minutes=15))
    assert worse["incident_id"] == "INC-EX-017-20261115T1445"
    assert world.sfn.start(worse).visited[-1] == "EscalatedOrIgnored"


def test_smoke_s2_inputs_group_the_four_machines(world):
    smoke = _smoke()
    z = ids.zones()["NORTE"]
    gh = geohash.encode(z["lat"], z["lon"], 6)
    for mid in smoke.S2_SCORES:
        world.ddb.state.put_item(Item={"PK": f"CELL#{gh}", "SK": f"A#2026-11-15T14:37:00Z#{mid}", "machine_id": mid,
                                       "score": Decimal("2.4"), "severity": "schedule"})
    spot = {"zone_id": "NORTE", "lat": z["lat"], "lon": z["lon"], "geohash6": gh}
    runs = [world.sfn.start(smoke.build_input(mid, spot, score, "fuel_contamination", smoke.S2, NOW))
            for mid, score in smoke.S2_SCORES.items()]
    assert [r.status for r in runs] == ["SUCCEEDED", "SUCCEEDED", "WAITING", "WAITING"]
    assert {get_meta(world.ddb.registry, r.name)["group_id"] for r in runs} == {f"GRP-{gh}-20261115T14"}
