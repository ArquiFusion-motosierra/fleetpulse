"""Pruebas de fplib: sin red ni credenciales (moto para DynamoDB, fakes para Secrets Manager)."""
import json
import uuid
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from fplib import auth, clock, envelope, geohash, ids, inbox, outbox, schema, secrets

REPO = Path(__file__).resolve().parents[3]
INC = "INC-EX-017-20261115T1430"


class FakeSecrets:
    def __init__(self, values):
        self.values, self.calls = values, 0

    def get_secret_value(self, SecretId):
        self.calls += 1
        if SecretId not in self.values:
            raise KeyError(SecretId)
        return {"SecretString": self.values[SecretId]}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    secrets.clear_cache()
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def _incident_env(payload=None):
    if payload is None:
        payload = json.loads((REPO / "contracts/examples/envelope/incident-opened.json").read_text())["payload"]
    return envelope.build("incident.opened", "aws.incident-flow", "incident", INC, payload,
                          incident_id=INC, machine_id="EX-017")


def test_window15_and_incident_id():
    ts = clock.parse("2026-11-15T14:38:05Z")
    assert clock.window15(ts) == "20261115T1430"
    assert ids.incident_id("EX-017", ts) == INC
    assert clock.iso(ts) == "2026-11-15T14:38:05Z"


def test_group_id():
    ts = clock.parse("2026-11-15T14:38:05Z")
    assert ids.group_id("6ma7ju", ts) == "GRP-6ma7ju-20261115T14"


def test_geohash_known_point():
    assert geohash.encode(57.64911, 10.40744, 6) == "u4pruy"
    assert len(geohash.encode(-13.9003, -71.5611, 6)) == 6


def test_envelope_validates_incident_opened():
    env = _incident_env()
    assert env["schema_version"] == 1 and uuid.UUID(env["event_id"]).version == 4


def test_envelope_rejects_bad_payload():
    with pytest.raises(schema.ContractError) as e:
        _incident_env({})
    assert isinstance(e.value.path, list)


def test_envelope_rejects_unknown_event_type():
    with pytest.raises(schema.ContractError):
        envelope.build("nope.x", "aws.incident-flow", "incident", INC, {})


def _tables():
    ddb = boto3.client("dynamodb")
    for name, key in (("fp-inbox", "message_id"), ("fp-outbox", "event_id")):
        ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                         AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}])
    return ddb


@mock_aws
def test_inbox_claim_once_and_outbox_conditional():
    ddb = _tables()
    assert inbox.claim("fp-inbox", "batch:B-1", "ingest") is True
    assert inbox.claim("fp-inbox", "batch:B-1", "ingest") is False
    env = _incident_env()
    outbox.put_event("fp-outbox", env)
    with pytest.raises(ClientError):
        outbox.put_event("fp-outbox", env)
    item = ddb.get_item(TableName="fp-outbox", Key={"event_id": {"S": env["event_id"]}})["Item"]
    assert json.loads(item["payload"]["S"]) == env
    assert item["type"]["S"] == "incident.opened" and item["incident_id"]["S"] == INC and "ttl" in item


@mock_aws
def test_outbox_transact_items():
    ddb = _tables()
    env = _incident_env()
    ddb.transact_write_items(TransactItems=[outbox.transact_items(env)])
    assert "Item" in ddb.get_item(TableName="fp-outbox", Key={"event_id": {"S": env["event_id"]}})


def test_bom_and_specialty():
    assert ids.bom_for("EX", "hydraulic_pump") == [{"part_id": "PRT-04", "qty": 1}, {"part_id": "PRT-03", "qty": 2}]
    assert ids.specialty_for("hydraulic_pump") == "hydraulics"
    assert ids.machines()["EX-017"]["zone_id"] == "NORTE"
    assert len(ids.technicians()) == 6 and set(ids.zones()) == {"NORTE", "CENTRO", "SUR"}


def test_check_api_key_constant_time_and_case_insensitive():
    fake = FakeSecrets({"fleetpulse/api/ingest-key": '{"api_key":"k"}'})
    assert auth.check_api_key({"X-Api-Key": "k"}, "fleetpulse/api/ingest-key", client=fake) is True
    assert auth.check_api_key({"x-api-key": "nope"}, "fleetpulse/api/ingest-key", client=fake) is False
    assert auth.check_api_key({}, "fleetpulse/api/ingest-key", client=fake) is False
    assert auth.check_api_key({"x-api-key": "k"}, "missing", client=fake) is False


def test_secrets_cached():
    fake = FakeSecrets({"fleetpulse/api/ingest-key": '{"api_key":"k"}'})
    assert secrets.get_json("fleetpulse/api/ingest-key", client=fake)["api_key"] == "k"
    assert secrets.get_json("fleetpulse/api/ingest-key", client=fake)["api_key"] == "k" and fake.calls == 1


def test_log_event_is_one_json_line(caplog):
    from fplib import log
    with caplog.at_level("INFO", logger="fplib"):
        log.event("hola", machine_id="EX-017", n=1)
    assert json.loads(caplog.records[0].getMessage()) == {"level": "INFO", "msg": "hola", "machine_id": "EX-017", "n": 1}
