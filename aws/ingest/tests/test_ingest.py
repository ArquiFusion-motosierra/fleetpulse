"""Pruebas de api/ingest.py: moto para DynamoDB, fake para Secrets Manager. Sin red."""
import copy
import json
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from moto import mock_aws

from api.ingest import handler

EXAMPLES = Path(__file__).resolve().parents[3] / "contracts" / "examples"


def load(rel):
    return json.loads((EXAMPLES / rel).read_text())


class FakeSecrets:
    def get_secret_value(self, SecretId):
        return {"SecretString": json.dumps({"api_key": "k"})}


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "x"), ("AWS_SECRET_ACCESS_KEY", "x"), ("AWS_DEFAULT_REGION", "us-east-1"),
                 ("STATE_TABLE", "fp-state"), ("INBOX_TABLE", "fp-inbox"),
                 ("API_KEY_SECRET", "fleetpulse/api/ingest-key")):
        monkeypatch.setenv(k, v)
    from fplib import secrets
    secrets.clear_cache()


@pytest.fixture
def deps():
    with mock_aws():
        ddb = boto3.client("dynamodb")
        ddb.create_table(
            TableName="fp-state", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK", "GSI1PK", "GSI1SK")],
            GlobalSecondaryIndexes=[{
                "IndexName": "GSI1", "Projection": {"ProjectionType": "ALL"},
                "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"},
                              {"AttributeName": "GSI1SK", "KeyType": "RANGE"}]}])
        ddb.create_table(TableName="fp-inbox", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "message_id", "KeyType": "HASH"}],
                         AttributeDefinitions=[{"AttributeName": "message_id", "AttributeType": "S"}])
        state = boto3.resource("dynamodb").Table("fp-state")
        yield {"state": state, "inbox": "fp-inbox", "secrets": FakeSecrets()}


def api_event(path, body, key="k"):
    headers = {"content-type": "application/json"}
    if key:
        headers["x-api-key"] = key
    return {"rawPath": path, "headers": headers, "body": json.dumps(body)}


def query(state, pk):
    return state.query(KeyConditionExpression=Key("PK").eq(pk))["Items"]


def get(state, pk, sk):
    return state.get_item(Key={"PK": pk, "SK": sk})["Item"]


def count_readings(state, machine):
    return sum(1 for i in query(state, f"MACHINE#{machine}") if i["SK"].startswith("R#"))


def test_rejects_missing_api_key(deps):
    assert handler(api_event("/telemetry", load("telemetry-batch/live.json"), key=None), **deps)["statusCode"] == 401


def test_rejects_wrong_api_key(deps):
    assert handler(api_event("/telemetry", load("telemetry-batch/live.json"), key="nope"), **deps)["statusCode"] == 401


def test_rejects_invalid_batch(deps):
    r = handler(api_event("/telemetry", {"batch_id": "x"}, key="k"), **deps)
    body = json.loads(r["body"])
    assert r["statusCode"] == 400 and "error" in body and isinstance(body["path"], list)
    assert r["headers"] == {"content-type": "application/json"}


def test_rejects_malformed_json(deps):
    ev = api_event("/telemetry", {})
    ev["body"] = "{nope"
    assert handler(ev, **deps)["statusCode"] == 400


def test_unknown_route_is_404(deps):
    assert handler(api_event("/nope", {}), **deps)["statusCode"] == 404


def test_accepts_batch_writes_readings_and_meta(deps):
    live = load("telemetry-batch/live.json")
    r = handler(api_event("/telemetry", live, key="k"), **deps)
    assert r["statusCode"] == 202
    assert json.loads(r["body"]) == {"status": "accepted", "batch_id": live["batch_id"], "readings": 10}
    state = deps["state"]
    items = query(state, "MACHINE#EX-017")
    readings = [i for i in items if i["SK"].startswith("R#")]
    assert len(readings) == 10 and all(i["batch_id"] == live["batch_id"] and i["ttl"] for i in readings)
    last = max(x["ts"] for x in live["readings"])
    meta = get(state, "MACHINE#EX-017", "META")
    assert meta["geohash6"] and meta["last_ts"] == last and meta["GSI1PK"] == "ZONE#NORTE"
    assert meta["GSI1SK"] == "MACHINE#EX-017" and meta["status"] == "ok" and meta["zone_id"] == "NORTE"
    newest = max(live["readings"], key=lambda x: x["ts"])
    assert meta["hours_total"] == Decimal(str(newest["cumulative_operating_hours"]))


def test_reading_ttl_is_ts_plus_three_days(deps):
    live = load("telemetry-batch/live.json")
    handler(api_event("/telemetry", live, key="k"), **deps)
    item = get(deps["state"], "MACHINE#EX-017", "R#2026-11-15T14:30:00Z")
    from fplib import clock
    assert int(item["ttl"]) == int(clock.parse("2026-11-15T14:30:00Z").timestamp()) + 3 * 86400


def test_ingest_duplicate_batch_is_ignored(deps):
    live = load("telemetry-batch/live.json")
    handler(api_event("/telemetry", live, key="k"), **deps)
    r = handler(api_event("/telemetry", live, key="k"), **deps)
    assert r["statusCode"] == 200 and json.loads(r["body"])["status"] == "duplicate"
    assert count_readings(deps["state"], "EX-017") == 10


def test_meta_never_moves_backwards(deps):
    live = load("telemetry-batch/live.json")
    handler(api_event("/telemetry", live, key="k"), **deps)
    original = get(deps["state"], "MACHINE#EX-017", "META")["last_ts"]
    older = copy.deepcopy(live)
    older["batch_id"] = "B-20261115T140000Z-0a0b0c0d"
    for rd in older["readings"]:
        rd["ts"] = rd["ts"].replace("T14:3", "T14:0")
    assert handler(api_event("/telemetry", older, key="k"), **deps)["statusCode"] == 202
    assert get(deps["state"], "MACHINE#EX-017", "META")["last_ts"] == original


def test_meta_status_not_overwritten(deps):
    live = load("telemetry-batch/live.json")
    handler(api_event("/telemetry", live, key="k"), **deps)
    deps["state"].update_item(Key={"PK": "MACHINE#EX-017", "SK": "META"},
                              UpdateExpression="SET #s = :w, baseline = :b",
                              ExpressionAttributeNames={"#s": "status"},
                              ExpressionAttributeValues={":w": "warning", ":b": Decimal("1")})
    newer = copy.deepcopy(live)
    newer["batch_id"] = "B-20261115T150000Z-0a0b0c0d"
    for rd in newer["readings"]:
        rd["ts"] = rd["ts"].replace("T14:3", "T15:3")
    handler(api_event("/telemetry", newer, key="k"), **deps)
    meta = get(deps["state"], "MACHINE#EX-017", "META")
    assert meta["status"] == "warning" and meta["baseline"] == 1 and meta["last_ts"].startswith("2026-11-15T15:3")


def test_batch_with_fault_is_accepted(deps):
    assert handler(api_event("/telemetry", load("telemetry-batch/with-fault.json"), key="k"), **deps)["statusCode"] == 202


def test_context_stored_under_zone(deps):
    items = [load("fuel.refill/refill.json"), load("fuel.refill/weather.json")]
    r = handler(api_event("/context", items, key="k"), **deps)
    assert r["statusCode"] == 202 and json.loads(r["body"]) == {"status": "accepted", "context": 2}
    stored = query(deps["state"], "CTX#NORTE")
    assert len(stored) == 2 and all(i["ttl"] for i in stored)
    assert {i["SK"] for i in stored} == {"2026-11-15T14:12:00Z#CTX-20261115T141200Z-a1b2c3",
                                         "2026-11-15T14:00:00Z#CTX-20261115T140000Z-d4e5f6"}


def test_context_rejects_invalid_item(deps):
    r = handler(api_event("/context", [{"type": "weather"}], key="k"), **deps)
    assert r["statusCode"] == 400 and "error" in json.loads(r["body"])
