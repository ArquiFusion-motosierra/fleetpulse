import json
import os

import boto3
import jsonschema
import pytest
import yaml
from moto import mock_aws

from reg import api, apply, consumer, seed, store

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def load(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return yaml.safe_load(f) if rel.endswith(".yaml") else json.load(f)


IDS = load("contracts/ids.yaml")
OPENED = load("contracts/examples/incident/opened.json")
ASSIGNED = load("contracts/examples/incident/assigned.json")
CLOSED = load("contracts/examples/incident/closed.json")
RESERVE = load("contracts/examples/part/reserve-requested.json")
INC = OPENED["incident_id"]


def _gsi(name):
    return {"IndexName": name, "Projection": {"ProjectionType": "ALL"},
            "KeySchema": [{"AttributeName": f"{name}PK", "KeyType": "HASH"},
                          {"AttributeName": f"{name}SK", "KeyType": "RANGE"}]}


@pytest.fixture
def ddb(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        c = boto3.client("dynamodb", region_name="us-east-1")
        attrs = [{"AttributeName": n, "AttributeType": "S"} for n in
                 ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK")]
        c.create_table(TableName="fp-registry", BillingMode="PAY_PER_REQUEST", AttributeDefinitions=attrs,
                       KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                       GlobalSecondaryIndexes=[_gsi("GSI1"), _gsi("GSI2")])
        c.create_table(TableName="fp-state", BillingMode="PAY_PER_REQUEST", AttributeDefinitions=attrs[:2],
                       KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}])
        c.create_table(TableName="fp-inbox", BillingMode="PAY_PER_REQUEST",
                       AttributeDefinitions=[{"AttributeName": "message_id", "AttributeType": "S"}],
                       KeySchema=[{"AttributeName": "message_id", "KeyType": "HASH"}])
        c.create_table(TableName="fp-outbox", BillingMode="PAY_PER_REQUEST",
                       AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}],
                       KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}])
        seed.seed(c, IDS)
        yield c


def evt(base, **over):
    e = json.loads(json.dumps(base))
    e.update(over)
    return e


def stock(c, part, zone):
    s = store.get(c, "STOCK", f"{part}#{zone}")
    return s["on_hand"], s["reserved"]


def test_seed_loads_catalog_and_is_idempotent(ddb):
    assert seed.counts(ddb) == {"machines": 100, "stock_rows": 36}
    ddb.update_item(TableName="fp-registry", Key=store.key("STOCK", "PRT-01#NORTE"),
                    UpdateExpression="SET reserved = :r", ExpressionAttributeValues=store.vals(r=3))
    seed.seed(ddb, IDS)
    assert stock(ddb, "PRT-01", "NORTE") == (12, 3)  # no pisa lo reservado


def test_opened_then_duplicate(ddb):
    assert apply.apply(ddb, OPENED)["status"] == "applied"
    assert apply.apply(ddb, OPENED)["status"] == "duplicate"
    inc = store.get(ddb, f"INCIDENT#{INC}", "META")
    assert inc["status"] == "open" and inc["severity"] == "schedule"
    m = store.get(ddb, "MACHINE#EX-017", "META")
    assert m["severity"] == "schedule" and m["open_incident_id"] == INC


def test_reserve_falls_back_to_nearest_zone_then_shortage(ddb):
    apply.apply(ddb, OPENED)
    r1 = apply.apply(ddb, RESERVE)["reservation"]
    assert [(x["part_id"], x["zone_id"]) for x in r1["reserved"]] == [("PRT-04", "NORTE"), ("PRT-03", "NORTE")]
    assert stock(ddb, "PRT-04", "NORTE") == (1, 1)
    # Otra reserva de bomba: Norte ya no tiene, va a Centro (la mas cercana)
    r2 = apply.apply(ddb, evt(RESERVE, event_id="11111111-1111-4111-8111-111111111111"))["reservation"]
    assert r2["reserved"][0]["zone_id"] == "CENTRO"
    for i in range(2, 4):
        apply.apply(ddb, evt(RESERVE, event_id=f"1111111{i}-1111-4111-8111-111111111111"))
    r5 = apply.apply(ddb, evt(RESERVE, event_id="99999999-1111-4111-8111-111111111111"))["reservation"]
    assert r5["missing"] == [{"part_id": "PRT-04", "qty": 1}] and r5["reorder_suggested"]
    assert api.list_reorders(ddb)[0]["source"] == "shortage"


def test_reserve_retry_does_not_double_book(ddb):
    apply.apply(ddb, OPENED)
    first = apply.reserve(ddb, RESERVE)  # simula que la Lambda murio antes de marcar el inbox
    again = apply.apply(ddb, RESERVE)["reservation"]
    assert first == again
    assert stock(ddb, "PRT-03", "NORTE") == (6, 2)


def test_close_consumes_reserved_parts_and_frees_technician(ddb):
    for e in (OPENED, RESERVE, ASSIGNED):
        apply.apply(ddb, e)
    assert store.get(ddb, "TECH#TEC-01", "META")["available"] is False
    apply.apply(ddb, CLOSED)
    assert stock(ddb, "PRT-04", "NORTE") == (0, 0)
    assert stock(ddb, "PRT-03", "NORTE") == (4, 0)
    assert store.get(ddb, "TECH#TEC-01", "META")["available"] is True
    m = store.get(ddb, "MACHINE#EX-017", "META")
    assert m["severity"] == "ok" and "open_incident_id" not in m
    d = api.incident_detail(ddb, INC)
    assert d["status"] == "closed" and [t["type"] for t in d["timeline"]] == [
        "incident.opened", "incident.assigned", "part.reserve.requested", "incident.closed"]
    assert {r["status"] for r in d["reservations"]} == {"consumed"}


def test_closed_before_opened_keeps_closed(ddb):
    apply.apply(ddb, CLOSED)
    apply.apply(ddb, ASSIGNED)
    apply.apply(ddb, OPENED)
    assert store.get(ddb, f"INCIDENT#{INC}", "META")["status"] == "closed"


def test_consumer_writes_contract_valid_follow_ups(ddb):
    rec = {"messageId": "m1", "body": json.dumps({"Type": "Notification", "Message": json.dumps(RESERVE)})}
    assert consumer.handler({"Records": [rec]}, client=ddb) == {"batchItemFailures": []}
    items = [store.des(i) for i in ddb.scan(TableName="fp-outbox")["Items"]]
    schema = load("contracts/part.schema.json")
    for i in items:
        jsonschema.validate(json.loads(i["payload"]), schema)
    assert [i["type"] for i in items] == ["part.reserved"]


def test_consumer_reports_only_bad_message(ddb):
    good = {"messageId": "ok", "body": json.dumps(OPENED)}
    bad = {"messageId": "bad", "body": "{not json"}
    assert consumer.handler({"Records": [good, bad]}, client=ddb) == {"batchItemFailures": [{"itemIdentifier": "bad"}]}


class Secrets:
    def get_secret_value(self, SecretId):
        return {"SecretString": json.dumps({"api_key": "k"})}


def call(ddb, route, key="k", **path):
    api._cache.clear()
    r = api.handler({"routeKey": route, "headers": {"X-Api-Key": key}, "pathParameters": path or None},
                    client=ddb, secrets=Secrets())
    return r["statusCode"], json.loads(r["body"])


def test_read_api(ddb):
    apply.apply(ddb, OPENED)
    ddb.put_item(TableName="fp-state", Item=store.ser({"PK": "MACHINE#EX-017", "SK": "META", "zone": "NORTE",
                                                      "lat": -13.9011, "lon": -71.5622, "status": "operating"}))
    code, body = call(ddb, "GET /machines")
    assert code == 200 and len(body["machines"]) == 100
    ex = next(m for m in body["machines"] if m["machine_id"] == "EX-017")
    assert ex["status"] == "operating" and ex["severity"] == "schedule" and ex["lat"] == -13.9011
    code, body = call(ddb, "GET /machines/{id}", id="EX-017")
    assert code == 200 and body["incidents"][0]["incident_id"] == INC
    assert call(ddb, "GET /incidents")[1]["incidents"][0]["incident_id"] == INC
    assert len(call(ddb, "GET /stock")[1]["stock"]) == 36
    assert call(ddb, "GET /incidents/{id}", id="INC-EX-001-20261115T1430")[0] == 404
    assert call(ddb, "GET /stock", key="wrong")[0] == 401
