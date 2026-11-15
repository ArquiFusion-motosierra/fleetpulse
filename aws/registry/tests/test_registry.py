import json
import os

import boto3
import jsonschema
import pytest
import yaml
from moto import mock_aws

from reg import api, consumer, seed, store
from reg import apply as reg_apply

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def load(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return yaml.safe_load(f) if rel.endswith(".yaml") else json.load(f)


IDS = load("contracts/ids.yaml")
OPENED = load("contracts/examples/incident/opened.json")
CLOSED = load("contracts/examples/incident/closed.json")
RESERVE = load("contracts/examples/part/reserve-requested.json")
KPI = load("contracts/examples/kpi.daily/day.json")
INC = OPENED["incident_id"]
PK_SK = [{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}]


def _gsi(name):
    return {"IndexName": name, "Projection": {"ProjectionType": "ALL"},
            "KeySchema": [{"AttributeName": f"{name}PK", "KeyType": "HASH"},
                          {"AttributeName": f"{name}SK", "KeyType": "RANGE"}]}


def _attrs(*names):
    return [{"AttributeName": n, "AttributeType": "S"} for n in names]


@pytest.fixture
def ddb():
    with mock_aws():
        c = boto3.client("dynamodb", region_name="us-east-1")
        # Mismas tablas que crea fp-core (plan de Benjamin, Task 2)
        c.create_table(TableName="fp-registry", BillingMode="PAY_PER_REQUEST", KeySchema=PK_SK,
                       AttributeDefinitions=_attrs("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK"),
                       GlobalSecondaryIndexes=[_gsi("GSI1"), _gsi("GSI2")])
        c.create_table(TableName="fp-state", BillingMode="PAY_PER_REQUEST", KeySchema=PK_SK,
                       AttributeDefinitions=_attrs("PK", "SK", "GSI1PK", "GSI1SK"), GlobalSecondaryIndexes=[_gsi("GSI1")])
        c.create_table(TableName="fp-inbox", BillingMode="PAY_PER_REQUEST", AttributeDefinitions=_attrs("message_id"),
                       KeySchema=[{"AttributeName": "message_id", "KeyType": "HASH"}])
        c.create_table(TableName="fp-outbox", BillingMode="PAY_PER_REQUEST", AttributeDefinitions=_attrs("event_id"),
                       KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}])
        seed.seed(c, IDS)
        yield c


def benjamin_opens_incident(c, e=OPENED):
    """Lo que hace create_incident en incident-flow: META + EV# (y el outbox, que aqui no importa)."""
    p = e["payload"]
    c.put_item(TableName="fp-registry", Item=store.ser({
        "PK": f"INCIDENT#{e['incident_id']}", "SK": "META", "status": "open", "severity": p["severity"],
        "score": p["score"], "failure_mode": p["failure_mode"], "machine_id": e["machine_id"], "zone_id": p["zone_id"],
        "lat": p["lat"], "lon": p["lon"], "opened_at": p["opened_at"], "recurrence_flag": p["recurrence_flag"],
        "tech_id": "TEC-01", "GSI1PK": f"MACHINE#{e['machine_id']}", "GSI1SK": f"INC#{p['opened_at']}",
        "GSI2PK": "STATUS#open", "GSI2SK": p["opened_at"]}))
    c.put_item(TableName="fp-registry", Item=store.ser({
        "PK": f"INCIDENT#{e['incident_id']}", "SK": f"EV#{e['occurred_at']}#{e['event_id']}",
        "event_type": e["event_type"], "source": e["source"], "payload": json.dumps(e)}))


def stock(c, part, zone):
    s = store.get(c, f"STOCK#{part}#{zone}", "META")
    return s["on_hand"], s["reserved"], s["available"]


def evt(base, **over):
    e = json.loads(json.dumps(base))
    e.update(over)
    return e


def test_seed_loads_catalog_and_keeps_dynamic_fields(ddb):
    assert seed.counts(ddb, IDS) == {"machines": 100, "stock_rows": 36}
    ddb.update_item(TableName="fp-registry", Key=store.key("STOCK#PRT-01#NORTE", "META"),
                    UpdateExpression="SET reserved = :r", ExpressionAttributeValues=store.vals(r=3))
    ddb.update_item(TableName="fp-state", Key=store.key("MACHINE#EX-017", "META"),
                    UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues=store.vals(s="schedule"))
    seed.seed(ddb, IDS)
    assert stock(ddb, "PRT-01", "NORTE")[1] == 3
    assert store.get(ddb, "MACHINE#EX-017", "META", table="fp-state")["status"] == "schedule"


def test_incident_events_are_left_to_incident_flow(ddb):
    assert reg_apply.apply(ddb, OPENED)["status"] == "ignored"
    assert "Item" not in ddb.get_item(TableName="fp-inbox", Key={"message_id": {"S": f"registry:{OPENED['event_id']}"}})


def test_reserve_nearest_zone_then_shortage(ddb):
    benjamin_opens_incident(ddb)
    r1 = reg_apply.apply(ddb, RESERVE)
    assert r1 == {"status": "applied", "follow_up": ["part.reserved"]}
    assert stock(ddb, "PRT-04", "NORTE") == (1, 1, 0)
    r2 = reg_apply.reserve(ddb, evt(RESERVE, event_id="11111111-1111-4111-8111-111111111111",
                                    incident_id="INC-EX-001-20261115T1430"))
    assert r2["reserved"][0]["zone_id"] == "CENTRO"  # Norte ya no tiene bomba: la mas cercana
    for i, mid in enumerate(("EX-002", "EX-004")):
        reg_apply.reserve(ddb, evt(RESERVE, event_id=f"2222222{i}-1111-4111-8111-111111111111",
                                   incident_id=f"INC-{mid}-20261115T1430"))
    r5 = reg_apply.reserve(ddb, evt(RESERVE, event_id="99999999-1111-4111-8111-111111111111",
                                    incident_id="INC-EX-005-20261115T1430"))
    assert r5["missing"] == [{"part_id": "PRT-04", "qty": 1}] and r5["reorder_suggested"]
    assert api.list_reorders(ddb)[0]["source"] == "shortage"


def test_reserve_retry_does_not_double_book(ddb):
    first = reg_apply.reserve(ddb, RESERVE)  # la Lambda murio antes de marcar el inbox
    again = reg_apply.apply(ddb, RESERVE)
    assert again["status"] == "applied"
    assert stock(ddb, "PRT-03", "NORTE") == (6, 2, 4)
    assert first["reserved"][1]["reservation_id"].endswith("#1")
    assert reg_apply.apply(ddb, RESERVE)["status"] == "duplicate"


def test_follow_ups_go_to_outbox_with_ev_item_and_match_contract(ddb):
    benjamin_opens_incident(ddb)
    rec = {"messageId": "m1", "body": json.dumps({"Type": "Notification", "Message": json.dumps(RESERVE)})}
    assert consumer.handler({"Records": [rec]}, client=ddb) == {"batchItemFailures": []}
    items = [store.des(i) for i in ddb.scan(TableName="fp-outbox")["Items"]]
    assert set(items[0]) >= {"event_id", "type", "aggregate_id", "incident_id", "payload", "created_at", "ttl"}
    jsonschema.validate(json.loads(items[0]["payload"]), load("contracts/part.schema.json"))
    timeline = api.incident_detail(ddb, INC)["timeline"]
    assert {t["type"] for t in timeline} == {"incident.opened", "part.reserved"}  # EV# de ambos escritores
    ev = next(i for i in store.query_all(ddb, TableName="fp-registry", KeyConditionExpression="PK = :p",
                                         ExpressionAttributeValues=store.vals(p=f"INCIDENT#{INC}"))
              if i.get("event_type") == "part.reserved")
    jsonschema.validate(json.loads(ev["payload"]), load("contracts/part.schema.json"))  # sobre completo
    assert next(t for t in timeline if t["type"] == "part.reserved")["payload"]["reservations"]


def test_close_consumes_reserved_parts(ddb):
    reg_apply.apply(ddb, RESERVE)
    assert reg_apply.apply(ddb, CLOSED)["status"] == "applied"
    assert stock(ddb, "PRT-04", "NORTE") == (0, 0, 0)
    assert stock(ddb, "PRT-03", "NORTE") == (4, 0, 4)
    rsv = store.query_all(ddb, TableName="fp-registry", KeyConditionExpression="PK = :p",
                          ExpressionAttributeValues=store.vals(p=f"RSV#{INC}"))
    assert {r["status"] for r in rsv} == {"consumed"}
    assert reg_apply.apply(ddb, CLOSED)["status"] == "duplicate"
    assert stock(ddb, "PRT-04", "NORTE") == (0, 0, 0)


def test_kpis_stored(ddb):
    reg_apply.apply(ddb, KPI)
    assert {r["machine_id"] for r in api.kpis(ddb, "2026-11-14")} == {"CF-042", "MN-003"}


def test_maintenance_orders_listed_soonest_first(ddb):
    order = load("contracts/examples/maintenance.order/preventive.json")
    later = evt(order, event_id="6b6c7d8e-9f0a-4b1c-8d2e-3f4a5b6c7d8e", machine_id="EX-017",
                aggregate_id="ORD-EX-017-20261118", payload={**order["payload"], "due_at": "2026-11-18T13:00:00Z"})
    assert reg_apply.apply(ddb, later)["status"] == "applied"
    assert reg_apply.apply(ddb, order)["status"] == "applied"
    assert reg_apply.apply(ddb, order)["status"] == "duplicate"
    assert [o["machine_id"] for o in api.list_orders(ddb)] == ["TO-006", "EX-017"]


def test_consumer_reports_only_bad_message(ddb):
    good = {"messageId": "ok", "body": json.dumps(RESERVE)}
    bad = {"messageId": "bad", "body": "{not json"}
    assert consumer.handler({"Records": [good, bad]}, client=ddb) == {"batchItemFailures": [{"itemIdentifier": "bad"}]}


class Secrets:
    def get_secret_value(self, SecretId):
        return {"SecretString": json.dumps({"api_key": "k"})}


def call(ddb, route, key="k", **path):
    from fplib import secrets
    secrets.clear_cache()
    r = api.handler({"routeKey": route, "headers": {"X-Api-Key": key}, "pathParameters": path or None},
                    client=ddb, secrets=Secrets())
    return r["statusCode"], json.loads(r["body"])


def test_read_api_on_benjamin_keys(ddb):
    benjamin_opens_incident(ddb)
    reg_apply.apply(ddb, RESERVE)
    ddb.update_item(TableName="fp-state", Key=store.key("MACHINE#EX-017", "META"),
                    UpdateExpression="SET #s = :s, lat = :lat, engine_status = :e", ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues=store.vals(s="schedule", lat=-13.9011, e="operating"))
    code, body = call(ddb, "GET /machines")
    assert code == 200 and len(body["machines"]) == 100
    ex = next(m for m in body["machines"] if m["machine_id"] == "EX-017")
    assert (ex["severity"], ex["status"], ex["lat"], ex["open_incident_id"]) == ("schedule", "operating", -13.9011, INC)
    code, body = call(ddb, "GET /machines/{id}", id="EX-017")
    assert body["incidents"][0]["incident_id"] == INC and body["open_incident_id"] == INC
    assert [i["incident_id"] for i in call(ddb, "GET /incidents")[1]["incidents"]] == [INC]
    d = call(ddb, "GET /incidents/{id}", id=INC)[1]
    assert {r["part_id"] for r in d["reservations"]} == {"PRT-03", "PRT-04"}
    stock_rows = call(ddb, "GET /stock")[1]["stock"]
    assert len(stock_rows) == 36 and next(s for s in stock_rows if s["part_id"] == "PRT-04"
                                          and s["zone_id"] == "NORTE")["available"] == 0
    assert call(ddb, "GET /incidents/{id}", id="INC-EX-001-20261115T1430")[0] == 404
    assert call(ddb, "GET /stock", key="wrong")[0] == 401
