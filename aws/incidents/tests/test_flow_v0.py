"""Pruebas de incident-flow v0 con moto DynamoDB (fp-state, fp-registry, fp-outbox) y la entrada que arma `score`."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from moto import mock_aws

from flow import assign_technician, check_existing, classify, create_incident, rank_technicians, render_messages
from fplib import clock, geohash, schema
from lib import registry

INC = "INC-EX-017-20261115T1430"
NOW = datetime(2026, 11, 15, 14, 38, 5, tzinfo=timezone.utc)
NOW_ISO = "2026-11-15T14:38:05Z"
LAT, LON = -13.9003, -71.5611
KM_LAT = 1 / 111.195  # grados de latitud por km
DEVIATED = [
    {"var": "hydraulic_pressure_bar", "value": 287.4, "z": -2.6},
    {"var": "hydraulic_oil_temp_c", "value": 71.9, "z": 2.1},
    {"var": "vibration_mm_s", "value": 9.8, "z": 1.9},
]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "x"), ("AWS_SECRET_ACCESS_KEY", "x"), ("AWS_DEFAULT_REGION", "us-east-1"),
                 ("STATE_TABLE", "fp-state"), ("REGISTRY_TABLE", "fp-registry"), ("OUTBOX_TABLE", "fp-outbox")):
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(clock, "now", lambda: NOW)


@pytest.fixture
def ex017():
    """Entrada de la ejecucion tal como la arma `aws/scoring/scoring/handler.py` para EX-017."""
    return {
        "incident_id": INC, "machine_id": "EX-017", "model": "EX", "serial": "AFCEX01721", "zone_id": "NORTE",
        "lat": LAT, "lon": LON, "geohash6": geohash.encode(LAT, LON, 6), "ts": "2026-11-15T14:37:59Z",
        "batch_id": "B-20261115T143759Z-1a2b3c4d", "score": 2.31, "severity": "schedule",
        "z": {"hydraulic_pressure_bar": {"self": -2.6, "peer": -1.8}, "hydraulic_oil_temp_c": {"self": 2.1, "peer": 1.4},
              "vibration_mm_s": {"self": 1.9, "peer": None}},
        "deviated_vars": [dict(d) for d in DEVIATED], "failure_mode": "hydraulic_pump", "hours_total": 2762,
    }


def _table(res, name, gsis):
    attrs = [("PK", "S"), ("SK", "S")] + [(f"{g}{k}", "S") for g in gsis for k in ("PK", "SK")]
    return res.create_table(
        TableName=name, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": t} for a, t in attrs],
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        GlobalSecondaryIndexes=[{"IndexName": g, "Projection": {"ProjectionType": "ALL"},
                                 "KeySchema": [{"AttributeName": f"{g}PK", "KeyType": "HASH"},
                                               {"AttributeName": f"{g}SK", "KeyType": "RANGE"}]} for g in gsis])


@pytest.fixture
def ddb():
    with mock_aws():
        res = boto3.resource("dynamodb", region_name="us-east-1")
        outbox = res.create_table(
            TableName="fp-outbox", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}])
        yield SimpleNamespace(state=_table(res, "fp-state", ("GSI1",)),
                              registry=_table(res, "fp-registry", ("GSI1", "GSI2")), outbox=outbox)


def _events(outbox, event_type):
    """Sobres en fp-outbox de un tipo, cada uno validado contra su contrato."""
    envs = [json.loads(i["payload"]) for i in outbox.scan()["Items"] if i["type"] == event_type]
    for env in envs:
        schema.validate("incident", env)
    return envs


def _ev_items(reg, incident_id):
    return reg.query(KeyConditionExpression=Key("PK").eq(f"INCIDENT#{incident_id}") & Key("SK").begins_with("EV#"))[
        "Items"]


def _meta(reg, incident_id):
    return reg.get_item(Key={"PK": f"INCIDENT#{incident_id}", "SK": "META"}).get("Item")


def _seed_incident(reg, incident_id, opened_at, status="open", **attrs):
    reg.put_item(Item={
        "PK": f"INCIDENT#{incident_id}", "SK": "META", "incident_id": incident_id, "machine_id": "EX-017",
        "status": status, "opened_at": opened_at, "GSI1PK": "MACHINE#EX-017", "GSI1SK": f"INC#{opened_at}",
        "GSI2PK": f"STATUS#{status}", "GSI2SK": opened_at, **attrs})


def _seed_tech(state, tech_id, specialty, lat, lon, available=True, zone="NORTE", **attrs):
    state.put_item(Item={
        "PK": f"TECH#{tech_id}", "SK": "META", "tech_id": tech_id, "name": f"Tecnico {tech_id}",
        "specialty": specialty, "zone_id": zone, "lat": Decimal(str(lat)), "lon": Decimal(str(lon)),
        "available": available, "GSI1PK": f"ZONE#{zone}", "GSI1SK": f"TECH#{tech_id}", **attrs})


def _tech(state, tech_id):
    return state.get_item(Key={"PK": f"TECH#{tech_id}", "SK": "META"})["Item"]


def _assign(ev, ddb):
    return assign_technician.handler(ev, state=ddb.state, registry=ddb.registry, outbox=ddb.outbox)


def _classified(ev):
    return {**ev, "classified": classify.handler(ev)}


def _created(ev, ddb):
    ev = _classified(ev)
    return {**ev, "incident": create_incident.handler(ev, registry=ddb.registry, outbox=ddb.outbox)}


def _ranked(ev, ddb):
    ev = _created(ev, ddb)
    return {**ev, "ranking": rank_technicians.handler(ev, state=ddb.state)}


def _assigned(ev, ddb):
    ev = _ranked(ev, ddb)
    return {**ev, "assignment": _assign(ev, ddb)}


def test_classify_schedule_needs_approval(ex017):
    assert classify.handler(ex017) == {"severity": "schedule", "needs_approval": True}
    assert classify.handler({**ex017, "score": 3.62}) == {"severity": "immediate", "needs_approval": False}


def test_create_incident_writes_registry_and_outbox_atomically(ex017, ddb):
    out = create_incident.handler(_classified(ex017), registry=ddb.registry, outbox=ddb.outbox)
    assert out == {"incident_id": INC, "opened_at": NOW_ISO, "recurrence_flag": False, "group_id": None,
                   "zone_id": "NORTE", "deviated_vars": DEVIATED}
    meta = _meta(ddb.registry, INC)
    assert meta["status"] == "open" and meta["severity"] == "schedule" and meta["failure_mode"] == "hydraulic_pump"
    assert meta["score"] == Decimal("2.31") and meta["zone_id"] == "NORTE" and meta["recurrence_flag"] is False
    assert (meta["GSI1PK"], meta["GSI1SK"]) == ("MACHINE#EX-017", f"INC#{NOW_ISO}")
    assert (meta["GSI2PK"], meta["GSI2SK"]) == ("STATUS#open", NOW_ISO)
    (env,) = _events(ddb.outbox, "incident.opened")
    assert env["source"] == "aws.incident-flow" and env["incident_id"] == INC and env["machine_id"] == "EX-017"
    assert env["payload"]["deviated_vars"] == DEVIATED and env["payload"]["recurrence_flag"] is False
    assert env["payload"]["group_id"] is None and env["payload"]["opened_at"] == NOW_ISO
    (ev,) = _ev_items(ddb.registry, INC)
    assert ev["SK"] == f"EV#{NOW_ISO}#{env['event_id']}" and ev["event_type"] == "incident.opened"
    assert json.loads(ev["payload"]) == env


def test_create_incident_rerun_is_noop(ex017, ddb):
    ev = _classified(ex017)
    first = create_incident.handler(ev, registry=ddb.registry, outbox=ddb.outbox)
    again = create_incident.handler(ev, registry=ddb.registry, outbox=ddb.outbox)
    assert again == first
    assert len(_events(ddb.outbox, "incident.opened")) == 1 and len(_ev_items(ddb.registry, INC)) == 1


def test_create_incident_tolerates_sparse_meta(ex017, ddb):
    sparse = {**ex017, "serial": "", "zone_id": "", "hours_total": 0}
    out = create_incident.handler(_classified(sparse), registry=ddb.registry, outbox=ddb.outbox)
    assert out["zone_id"] == "NORTE"  # zona de EX-017 en ids.yaml
    (env,) = _events(ddb.outbox, "incident.opened")
    assert env["payload"]["zone_id"] == "NORTE"



def test_create_incident_derives_deviated_vars_from_z_when_empty(ex017, ddb, caplog):
    # score >= 2.0 sin ninguna |z| >= 1.0: score trae deviated_vars vacio
    z = {"hydraulic_pressure_bar": {"self": -0.9, "peer": -0.95}, "coolant_temp_c": {"self": 0.8, "peer": None},
         "vibration_mm_s": {"self": None, "peer": 0.7}, "engine_rpm": {"self": None, "peer": None}}
    ev = _classified({**ex017, "deviated_vars": [], "z": z})
    with caplog.at_level("INFO", logger="fplib"):
        out = create_incident.handler(ev, registry=ddb.registry, outbox=ddb.outbox)
    derived = [{"var": "hydraulic_pressure_bar", "value": -0.95, "z": -0.95}]
    assert out["incident_id"] == INC and out["deviated_vars"] == derived
    (env,) = _events(ddb.outbox, "incident.opened")
    assert env["payload"]["deviated_vars"] == derived
    assignment = {"tech_id": "TEC-01", "assigned_at": NOW_ISO, "approval": "auto", "tech_busy": False}
    msgs = render_messages.handler({**ev, "incident": out, "ranking": {"top": []}, "assignment": assignment})
    assert "hydraulic_pressure_bar" in msgs["technician"] and "sin variables desviadas" not in msgs["supervisor"]
    assert "TEC-01" in msgs["supervisor"]  # fuera del ranking: se nombra por id
    (line,) = [json.loads(r.getMessage()) for r in caplog.records if "deviated_vars_derived" in r.getMessage()]
    assert line["incident_id"] == INC and line["machine_id"] == "EX-017" and line["var"] == "hydraulic_pressure_bar"


@pytest.mark.parametrize("z", [{}, {"engine_rpm": {"self": None, "peer": None}}])
def test_create_incident_without_deviated_vars_or_z_fails_clearly(ex017, ddb, z):
    ev = _classified({**ex017, "deviated_vars": [], "z": z})
    with pytest.raises(ValueError, match="deviated_vars"):
        create_incident.handler(ev, registry=ddb.registry, outbox=ddb.outbox)
    assert ddb.outbox.scan()["Items"] == [] and _meta(ddb.registry, INC) is None

def test_create_incident_sets_recurrence_when_same_mode_closed_recently(ex017, ddb):
    _seed_incident(ddb.registry, "INC-EX-017-20261113T0900", "2026-11-13T09:05:00Z", status="closed",
                   failure_mode="hydraulic_pump", closed_at=clock.iso(NOW - timedelta(days=2)))
    out = create_incident.handler(_classified(ex017), registry=ddb.registry, outbox=ddb.outbox)
    assert out["recurrence_flag"] is True
    assert _meta(ddb.registry, INC)["recurrence_flag"] is True
    (env,) = _events(ddb.outbox, "incident.opened")
    assert env["payload"]["recurrence_flag"] is True


def test_recurrence_ignores_other_modes_old_closures_and_open_incidents(ddb):
    since = NOW - timedelta(days=7)
    _seed_incident(ddb.registry, "INC-EX-017-20261101T0900", "2026-11-01T09:05:00Z", status="closed",
                   failure_mode="hydraulic_pump", closed_at="2026-11-02T10:00:00Z")
    _seed_incident(ddb.registry, "INC-EX-017-20261113T0900", "2026-11-13T09:05:00Z", status="closed",
                   failure_mode="engine_overheat", closed_at=clock.iso(NOW - timedelta(days=1)))
    _seed_incident(ddb.registry, "INC-EX-017-20261114T0900", "2026-11-14T09:05:00Z", failure_mode="hydraulic_pump")
    assert registry.recent_closed_same_mode(ddb.registry, "EX-017", "hydraulic_pump", since) is False
    assert registry.recent_closed_same_mode(ddb.registry, "EX-017", "engine_overheat", since) is True


def test_check_existing_escalates_open_incident(ex017, ddb):
    old = "INC-EX-017-20261115T1400"
    _seed_incident(ddb.registry, old, "2026-11-15T14:05:00Z", severity="schedule", score=Decimal("2.2"))
    ev = _classified({**ex017, "score": 3.62})
    out = check_existing.handler(ev, registry=ddb.registry, outbox=ddb.outbox)
    assert out == {"found": True, "incident_id": old, "escalated": True}
    assert _meta(ddb.registry, old)["severity"] == "immediate"
    (env,) = _events(ddb.outbox, "incident.escalated")
    assert env["incident_id"] == old and env["aggregate_id"] == old
    assert env["payload"] == {"from_severity": "schedule", "to_severity": "immediate", "score": 3.62}
    (item,) = _ev_items(ddb.registry, old)
    assert item["event_type"] == "incident.escalated"
    # reintento del paso: la severidad ya subio, no hay un segundo evento
    assert check_existing.handler(ev, registry=ddb.registry, outbox=ddb.outbox)["escalated"] is False
    assert len(_events(ddb.outbox, "incident.escalated")) == 1


def test_check_existing_same_severity_is_ignored_and_closed_is_not_found(ex017, ddb):
    _seed_incident(ddb.registry, "INC-EX-017-20261110T0900", "2026-11-10T09:05:00Z", status="closed",
                   severity="immediate")
    ev = _classified(ex017)
    assert check_existing.handler(ev, registry=ddb.registry, outbox=ddb.outbox) == {
        "found": False, "incident_id": None, "escalated": False}
    _seed_incident(ddb.registry, "INC-EX-017-20261115T1400", "2026-11-15T14:05:00Z", severity="schedule")
    assert check_existing.handler(ev, registry=ddb.registry, outbox=ddb.outbox) == {
        "found": True, "incident_id": "INC-EX-017-20261115T1400", "escalated": False}
    assert ddb.outbox.scan()["Items"] == []



def test_check_existing_stale_read_does_not_escalate(ex017, ddb, monkeypatch):
    old = "INC-EX-017-20261115T1400"
    _seed_incident(ddb.registry, old, "2026-11-15T14:05:00Z", severity="immediate")
    stale = {**_meta(ddb.registry, old), "severity": "schedule"}  # otra ejecucion ya lo escalo
    monkeypatch.setattr(registry, "open_incident_for", lambda table, machine_id: stale)
    out = check_existing.handler(_classified({**ex017, "score": 3.62}), registry=ddb.registry, outbox=ddb.outbox)
    assert out == {"found": True, "incident_id": old, "escalated": False}
    assert ddb.outbox.scan()["Items"] == [] and _ev_items(ddb.registry, old) == []

def test_rank_prefers_specialty_and_distance(ex017, ddb):
    _seed_tech(ddb.state, "TEC-01", "hydraulics", LAT + 1.9 * KM_LAT, LON)
    _seed_tech(ddb.state, "TEC-03", "engine", LAT - 1.0 * KM_LAT, LON)
    _seed_tech(ddb.state, "TEC-02", "hydraulics", LAT + 0.3 * KM_LAT, LON, available=False, zone="CENTRO")
    top = rank_technicians.handler(_created(ex017, ddb), state=ddb.state)["top"]
    assert [t["tech_id"] for t in top] == ["TEC-01", "TEC-03", "TEC-02"]
    assert set(top[0]) == {"tech_id", "name", "distance_km", "specialty_match", "score"}
    assert top[0]["specialty_match"] is True and top[0]["distance_km"] == pytest.approx(1.9, abs=0.01)
    assert top[0]["score"] == pytest.approx(2 * (1 / 1.9) * 2, rel=0.01)  # schedule x 1/km x especialidad
    assert top[1]["score"] == pytest.approx(2 * (1 / 1.0), rel=0.01)
    assert top[2]["score"] == 0  # no disponible


def test_rank_falls_back_to_ids_yaml_when_no_tech_items(ex017, ddb):
    top = rank_technicians.handler(_created(ex017, ddb), state=ddb.state)["top"]
    assert top[0]["tech_id"] == "TEC-01" and top[0]["name"] == "Rosa Quispe"  # hidraulica en NORTE
    assert top[0]["specialty_match"] is True and top[0]["score"] > 0
    assert all(t["tech_id"].startswith("TEC-") for t in top)


def test_assign_marks_tech_busy_and_emits_assigned(ex017, ddb):
    _seed_tech(ddb.state, "TEC-01", "hydraulics", LAT + 0.4 * KM_LAT, LON)
    ev = _ranked(ex017, ddb)
    out = assign_technician.handler(ev, state=ddb.state, registry=ddb.registry, outbox=ddb.outbox)
    assert out == {"tech_id": "TEC-01", "assigned_at": NOW_ISO, "approval": "auto", "tech_busy": False}
    tech = ddb.state.get_item(Key={"PK": "TECH#TEC-01", "SK": "META"})["Item"]
    assert tech["available"] is False and tech["current_incident"] == INC
    meta = _meta(ddb.registry, INC)
    assert meta["tech_id"] == "TEC-01" and meta["approval"] == "auto" and meta["assigned_at"] == NOW_ISO
    assert meta["status"] == "open" and meta["explanation"]["source"] == "template"
    (env,) = _events(ddb.outbox, "incident.assigned")
    p = env["payload"]
    assert p["tech_id"] == "TEC-01" and p["approval"] == "auto" and p["distance_km"] == pytest.approx(0.4, abs=0.01)
    assert p["explanation"]["source"] == "template" and p["explanation"]["suggested_parts"] == ["PRT-04", "PRT-03"]
    assert [i["event_type"] for i in _ev_items(ddb.registry, INC)].count("incident.assigned") == 1


def test_assign_rerun_is_noop(ex017, ddb):
    _seed_tech(ddb.state, "TEC-01", "hydraulics", LAT + 0.4 * KM_LAT, LON)
    ev = _ranked(ex017, ddb)
    first = assign_technician.handler(ev, state=ddb.state, registry=ddb.registry, outbox=ddb.outbox)
    again = assign_technician.handler(ev, state=ddb.state, registry=ddb.registry, outbox=ddb.outbox)
    assert again == first
    assert len(_events(ddb.outbox, "incident.assigned")) == 1


def test_messages_are_distinct_per_role(ex017, ddb):
    _seed_tech(ddb.state, "TEC-01", "hydraulics", LAT + 0.4 * KM_LAT, LON)
    msgs = render_messages.handler(_assigned(ex017, ddb))
    roles = ("technician", "supervisor", "management", "operator")
    assert set(msgs) == {*roles, "subject"}
    texts = [msgs[r] for r in roles]
    assert all(isinstance(t, str) and t for t in texts) and len(set(texts)) == 4
    assert msgs["subject"] == f"[FleetPulse] schedule EX-017 {INC}" and len(msgs["subject"]) <= 100
    assert "hydraulic_pressure_bar" in msgs["technician"] and "-2.6" in msgs["technician"]
    assert "PRT-04" in msgs["technician"] and "-13.9003" in msgs["technician"]
    assert "TEC-01" in msgs["supervisor"] and "schedule" in msgs["supervisor"] and "auto" in msgs["supervisor"]
    assert "TEC-01" in msgs["technician"] and "ocupado" not in msgs["supervisor"]
    assert "\n" not in msgs["management"] and "\n" not in msgs["operator"]


def test_assign_without_incident_fails_loudly(ex017, ddb):
    ev = {**_classified(ex017), "incident": {"incident_id": INC, "zone_id": "NORTE", "deviated_vars": DEVIATED},
          "ranking": {"top": [{"tech_id": "TEC-01", "name": "Rosa Quispe", "distance_km": 0.4,
                               "specialty_match": True, "score": 10.0}]}}
    with pytest.raises(RuntimeError, match=INC):
        assign_technician.handler(ev, state=ddb.state, registry=ddb.registry, outbox=ddb.outbox)
    assert ddb.outbox.scan()["Items"] == []


def test_assign_all_busy_keeps_tech_link_and_flags_incident(ex017, ddb):
    other = "INC-EX-003-20261115T1400"
    _seed_tech(ddb.state, "TEC-01", "hydraulics", LAT + 0.4 * KM_LAT, LON, available=False, current_incident=other)
    _seed_tech(ddb.state, "TEC-03", "engine", LAT - 1.0 * KM_LAT, LON, available=False,
               current_incident="INC-EX-005-20261115T1400")
    ev = _ranked(ex017, ddb)
    assert ev["ranking"]["top"][0]["tech_id"] == "TEC-01" and ev["ranking"]["top"][0]["score"] == 0
    out = _assign(ev, ddb)
    assert out == {"tech_id": "TEC-01", "assigned_at": NOW_ISO, "approval": "auto", "tech_busy": True}
    msgs = render_messages.handler({**ev, "assignment": out})
    assert "ocupado" in msgs["supervisor"] and "ocupado" in msgs["technician"]
    tech = _tech(ddb.state, "TEC-01")
    assert tech["available"] is False and tech["current_incident"] == other  # no se pisa el enlace previo
    meta = _meta(ddb.registry, INC)
    assert meta["tech_id"] == "TEC-01" and meta["tech_busy"] is True
    (env,) = _events(ddb.outbox, "incident.assigned")
    assert env["payload"]["tech_id"] == "TEC-01"
    assert _assign(ev, ddb) == out and len(_events(ddb.outbox, "incident.assigned")) == 1  # reintento


def test_second_incident_claims_next_available_tech(ex017, ddb):
    _seed_tech(ddb.state, "TEC-01", "hydraulics", LAT + 0.4 * KM_LAT, LON)
    _seed_tech(ddb.state, "TEC-02", "hydraulics", LAT + 1.5 * KM_LAT, LON)
    inc_b = "INC-EX-019-20261115T1430"
    first = _ranked(ex017, ddb)
    second = _ranked({**ex017, "machine_id": "EX-019", "incident_id": inc_b}, ddb)
    assert first["ranking"]["top"][0]["tech_id"] == second["ranking"]["top"][0]["tech_id"] == "TEC-01"
    assert _assign(first, ddb)["tech_id"] == "TEC-01"
    out = _assign(second, ddb)  # su ranking ya esta viejo: TEC-01 lo tomo la primera ejecucion
    assert out["tech_id"] == "TEC-02" and out["tech_busy"] is False
    assert _tech(ddb.state, "TEC-01")["current_incident"] == INC
    assert _tech(ddb.state, "TEC-02")["current_incident"] == inc_b and _tech(ddb.state, "TEC-02")["available"] is False
    assert _meta(ddb.registry, inc_b)["tech_id"] == "TEC-02"
    (env,) = [e for e in _events(ddb.outbox, "incident.assigned") if e["incident_id"] == inc_b]
    assert env["payload"]["tech_id"] == "TEC-02" and env["payload"]["distance_km"] == pytest.approx(1.5, abs=0.01)
    msgs = render_messages.handler({**second, "assignment": out})  # nombra al asignado, no al primero del ranking
    assert "TEC-02" in msgs["supervisor"] and "TEC-01" not in msgs["supervisor"] and "TEC-02" in msgs["technician"]
    assert "1.5 km" in msgs["supervisor"] and "ocupado" not in msgs["supervisor"]


def test_render_uses_approve_url_when_present(ex017, ddb):
    url = "https://api.example/approvals/tok?tech=TEC-01"
    msgs = render_messages.handler({**_assigned(ex017, ddb), "approve_url": url})
    assert url in msgs["supervisor"]
    assert url not in render_messages.handler(_assigned({**ex017, "incident_id": "INC-EX-017-20261115T1445"},
                                                        ddb))["supervisor"]


def test_assign_with_ids_yaml_fallback_creates_no_tech_item(ex017, ddb):
    ev = _ranked(ex017, ddb)  # fp-state sin TECH#: ranking desde ids.yaml
    out = _assign(ev, ddb)
    assert out["tech_id"] == "TEC-01" and out["tech_busy"] is False
    assert ddb.state.get_item(Key={"PK": "TECH#TEC-01", "SK": "META"}).get("Item") is None
    (env,) = _events(ddb.outbox, "incident.assigned")
    assert env["payload"]["tech_id"] == "TEC-01"
