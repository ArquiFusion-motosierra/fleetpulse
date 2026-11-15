"""T38: registry con stock ampliado, ledger de movimientos y faltante con zona y parque."""
import json
import os

import boto3
import jsonschema
import pytest
import yaml
from moto import mock_aws

from fplib.ids import merge_catalogs
from reg import api, seed, store
from reg import apply as reg_apply

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
IDS = merge_catalogs(os.path.join(ROOT, "contracts"))
with open(os.path.join(ROOT, "contracts", "supply.yaml"), encoding="utf-8") as f:
    SUPPLY = yaml.safe_load(f)
PK_SK = [{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}]


def load(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return json.load(f)


RESERVE = load("contracts/examples/part/reserve-requested.json")
CLOSED = load("contracts/examples/incident/closed.json")


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
        c.create_table(TableName="fp-registry", BillingMode="PAY_PER_REQUEST", KeySchema=PK_SK,
                       AttributeDefinitions=_attrs("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK", "GSI3PK", "GSI3SK"),
                       GlobalSecondaryIndexes=[_gsi("GSI1"), _gsi("GSI2"), _gsi("GSI3")])
        c.create_table(TableName="fp-state", BillingMode="PAY_PER_REQUEST", KeySchema=PK_SK,
                       AttributeDefinitions=_attrs("PK", "SK", "GSI1PK", "GSI1SK"), GlobalSecondaryIndexes=[_gsi("GSI1")])
        c.create_table(TableName="fp-inbox", BillingMode="PAY_PER_REQUEST", AttributeDefinitions=_attrs("message_id"),
                       KeySchema=[{"AttributeName": "message_id", "KeyType": "HASH"}])
        c.create_table(TableName="fp-outbox", BillingMode="PAY_PER_REQUEST", AttributeDefinitions=_attrs("event_id"),
                       KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}])
        yield c


def test_shortage_in_mining_carries_zone_and_tenant_and_never_takes_agro_stock(ddb):
    seed.seed(ddb, IDS, with_state=False, supply=SUPPLY, demo_supply=True)  # PRT-04 = 0 en mineria, 3 en agro
    assert reg_apply.apply(ddb, RESERVE)["follow_up"] == ["part.reserved", "part.shortage"]
    assert store.get(ddb, "STOCK#PRT-04#FUNDO-A", "META")["reserved"] == 0  # no cruza parques
    events = [json.loads(store.des(i)["payload"]) for i in ddb.scan(TableName="fp-outbox")["Items"]]
    shortage = next(e for e in events if e["event_type"] == "part.shortage")
    jsonschema.validate(shortage, load("contracts/part.schema.json"))
    assert shortage["payload"]["zone_id"] == "NORTE" and shortage["tenant_id"] == "mineria"
    reorder = store.query_all(ddb, TableName="fp-registry", KeyConditionExpression="PK = :p",
                              ExpressionAttributeValues=store.vals(p="REORDER#PRT-04#NORTE"))
    assert reorder and reorder[0]["tenant_id"] == "mineria"


def test_consumption_ledger_is_signed_with_ref_and_tenant(ddb):
    seed.seed(ddb, IDS, with_state=False, supply=SUPPLY)
    reg_apply.apply(ddb, RESERVE)
    reg_apply.apply(ddb, CLOSED)
    moves = store.query_all(ddb, TableName="fp-registry", KeyConditionExpression="PK = :p",
                            ExpressionAttributeValues=store.vals(p="MOVE#PRT-04#NORTE"))
    assert len(moves) == 1
    m = moves[0]
    assert (m["kind"], m["qty"], m["ref"], m["incident_id"], m["tenant_id"]) == (
        "consumption", -1, CLOSED["incident_id"], CLOSED["incident_id"], "mineria")


class Secrets:
    def get_secret_value(self, SecretId):
        return {"SecretString": json.dumps({"api_key": "k"})}


def test_stock_api_returns_supply_fields_and_respects_tenant(ddb):
    from fplib import secrets
    seed.seed(ddb, IDS, with_state=False, supply=SUPPLY)
    secrets.clear_cache()
    r = api.handler({"routeKey": "GET /stock", "headers": {"x-api-key": "k"},
                     "queryStringParameters": {"tenant": "agro"}}, client=ddb, secrets=Secrets())
    rows = json.loads(r["body"])["stock"]
    assert r["statusCode"] == 200 and rows and {x["tenant_id"] for x in rows} == {"agro"}
    assert {x["zone_id"] for x in rows} <= {"FUNDO-A", "FUNDO-B", "PACKING"}
    prt04 = next(x for x in rows if x["part_id"] == "PRT-04" and x["zone_id"] == "FUNDO-A")
    assert (prt04["on_hand"], prt04["on_order"], prt04["in_transit"], prt04["criticality"], prt04["safety_stock"]) == (
        3, 0, 0, "A", 1)


def test_stock_rows_without_supply_fields_read_as_zero(ddb):
    seed.seed(ddb, IDS, with_state=False)  # seed viejo, sin supply.yaml
    ddb.update_item(TableName="fp-registry", Key=store.key("STOCK#PRT-01#NORTE", "META"),
                    UpdateExpression="REMOVE on_order, in_transit, criticality, safety_stock")
    row = next(x for x in api.list_stock(ddb) if x["part_id"] == "PRT-01" and x["zone_id"] == "NORTE")
    assert (row["on_order"], row["in_transit"], row["criticality"], row["safety_stock"]) == (0, 0, "C", 3)
