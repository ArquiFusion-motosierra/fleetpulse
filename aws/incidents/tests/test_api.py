"""Pruebas de las APIs HTTP de fp-incidents (payload v2): aprobacion por correo (`api/approvals`) y cierre del
incidente (`api/close`). Moto DynamoDB y Secrets Manager; Step Functions con un doble que usa las excepciones
reales de botocore."""
import json
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
    assigned,
    ev_items,
    get_meta,
    get_tech,
    log_lines,
    outbox_events,
    seed_tech,
)

from api import approvals, close
from fplib import clock
from lib import registry

TOKEN = "AAAAKgAAAAIAAAAAAAAAAQ+6C4y/7ZgXt=="  # los task tokens traen +, / y =
CONSOLE_KEY, INGEST_KEY = "console-key-123", "ingest-key-456"
BODY = {"closed_by": "TEC-01", "parts_used": [{"part_id": "PRT-04", "zone_id": "NORTE", "qty": 1}],
        "cause_summary": "Bomba hidraulica cambiada"}


def _http(method, path, path_params, query=None, headers=None, body=None):
    return {"version": "2.0", "rawPath": path, "headers": headers or {}, "queryStringParameters": query,
            "pathParameters": path_params, "requestContext": {"http": {"method": method, "path": path}},
            "body": body, "isBase64Encoded": False}


# --- /approvals/{token} --------------------------------------------------------------------------------------------

def _approve(method="GET", token=quote(TOKEN, safe=""), tech="TEC-02", inc=INC):
    query = {k: v for k, v in (("tech", tech), ("inc", inc)) if v is not None} or None
    return _http(method, f"/approvals/{token}", {"token": token}, query)


def test_approval_sends_task_success(caplog):
    sfn = FakeSfn()
    with caplog.at_level("INFO", logger="fplib"):
        resp = approvals.handler(_approve(), sfn=sfn)
    (call,) = sfn.calls
    assert call["taskToken"] == TOKEN  # sin el url-encoding del enlace
    assert json.loads(call["output"]) == {"approval": "approved", "tech_id": "TEC-02"}
    assert resp["statusCode"] == 200 and resp["headers"]["content-type"].startswith("text/html")
    assert f"Aprobado: TEC-02 asignado a {INC}" in resp["body"]
    assert TOKEN not in caplog.text and quote(TOKEN, safe="") not in caplog.text
    (line,) = log_lines(caplog, "aprobacion_recibida")
    assert line["incident_id"] == INC and line["machine_id"] == "EX-017" and line["tech_id"] == "TEC-02"


def test_approval_accepts_post_and_an_already_decoded_token():
    sfn = FakeSfn()
    resp = approvals.handler(_approve(method="POST", token=TOKEN), sfn=sfn)
    assert resp["statusCode"] == 200 and sfn.calls[0]["taskToken"] == TOKEN


def test_approval_without_tech_lets_the_ranking_decide():
    sfn = FakeSfn()
    resp = approvals.handler(_approve(tech=None), sfn=sfn)
    assert json.loads(sfn.calls[0]["output"]) == {"approval": "approved", "tech_id": None}
    assert resp["statusCode"] == 200 and f"asignado a {INC}" in resp["body"]


@pytest.mark.parametrize("error", ["TaskTimedOut", "TaskDoesNotExist", "InvalidToken"])
def test_approval_is_idempotent(error, caplog):
    sfn = FakeSfn(fail=error)
    with caplog.at_level("INFO", logger="fplib"):
        resp = approvals.handler(_approve(), sfn=sfn)
    assert resp["statusCode"] == 200 and "Esta aprobación ya fue procesada" in resp["body"]
    (line,) = log_lines(caplog, "aprobacion_ya_procesada")
    assert line["error"] == error and line["incident_id"] == INC


def test_approval_rejects_a_bad_tech_without_touching_the_execution():
    sfn = FakeSfn()
    resp = approvals.handler(_approve(tech="<script>alert(1)</script>"), sfn=sfn)
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


def _close(ddb, keys, incident_id=INC, key=CONSOLE_KEY, body=BODY):
    headers = {"x-api-key": key} if key else {}
    raw = body if isinstance(body, str) else json.dumps(body)
    ev = _http("POST", f"/incidents/{incident_id}/close", {"incident_id": incident_id}, headers=headers, body=raw)
    return close.handler(ev, state=ddb.state, registry=ddb.registry, outbox=ddb.outbox, secrets=keys)


@pytest.mark.parametrize("key", [None, "wrong-key"])
def test_close_requires_console_key(ddb, keys, open_incident, key):
    resp = _close(ddb, keys, key=key)
    assert resp["statusCode"] == 401
    assert get_meta(ddb.registry, INC)["status"] == "open" and get_tech(ddb.state, "TEC-01")["available"] is False
    assert outbox_events(ddb.outbox, "incident.closed") == []


@pytest.mark.parametrize("key", [CONSOLE_KEY, INGEST_KEY])
def test_close_accepts_either_key_and_the_console_body(ddb, keys, open_incident, key):
    resp = _close(ddb, keys, key=key, body={"closed_by": "console", "cause_summary": ""})  # cuerpo de la consola
    assert resp["statusCode"] == 200
    (env,) = outbox_events(ddb.outbox, "incident.closed")
    assert env["payload"]["parts_used"] == [] and env["payload"]["closed_by"] == "console"


def test_close_updates_registry_frees_tech_and_emits_closed(ddb, keys, open_incident, monkeypatch, caplog):
    later = NOW + timedelta(hours=2, minutes=30, seconds=36)  # 2.51 h despues de opened_at
    monkeypatch.setattr(clock, "now", lambda: later)
    with caplog.at_level("INFO", logger="fplib"):
        resp = _close(ddb, keys)
    assert resp["statusCode"] == 200 and json.loads(resp["body"]) == {"incident_id": INC, "downtime_h": 2.51}
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
    resp = _close(ddb, keys)
    assert resp["statusCode"] == 409
    assert len(outbox_events(ddb.outbox, "incident.closed")) == 1


@pytest.mark.parametrize("incident_id", ["INC-EX-017-20261115T1500", "no-es-un-id"])
def test_close_unknown_incident_is_404(ddb, keys, open_incident, incident_id):
    assert _close(ddb, keys, incident_id=incident_id)["statusCode"] == 404
    assert outbox_events(ddb.outbox, "incident.closed") == []


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
