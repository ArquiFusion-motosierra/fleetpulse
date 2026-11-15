"""scripts/backfill_tenant.py: que se estampa en cada tipo de item (moto, sin red)."""
import os
import sys

import boto3
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import backfill_tenant as bf  # noqa: E402


def test_update_for_each_kind():
    inc = bf.update_for({"PK": "INCIDENT#INC-EX-017-20261115T1430", "SK": "META", "machine_id": "EX-017",
                         "opened_at": "2026-11-15T14:31:00Z"})
    assert inc["ExpressionAttributeValues"] == {":t": "mineria", ":g3pk": "TENANT#mineria",
                                                ":g3sk": "INC#2026-11-15T14:31:00Z"}
    rpt = bf.update_for({"PK": "REPORT#RPT-CS-003-20261115T144012", "SK": "META", "machine_id": "CS-003",
                         "reported_at": "2026-11-15T14:40:12Z"})
    assert rpt["ExpressionAttributeValues"][":g3pk"] == "TENANT#agro"
    assert bf.update_for({"PK": "STOCK#PRT-22#PLANTA-2", "SK": "META"})["ExpressionAttributeValues"] == {
        ":t": "industria"}
    assert bf.update_for({"PK": "TECH#TEC-11", "SK": "META"})["ExpressionAttributeValues"] == {":t": "agro"}
    assert bf.update_for({"PK": "INCIDENT#X", "SK": "EV#2026"}) is None  # eventos: no
    assert bf.update_for({"PK": "MACHINE#EX-017", "SK": "R#2026"}) is None  # lecturas: no
    assert bf.update_for({"PK": "INCIDENT#X", "SK": "META", "tenant_id": "agro"}) is None  # ya estampado


def test_run_is_idempotent_and_dry_run_writes_nothing(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "x"), ("AWS_SECRET_ACCESS_KEY", "x"), ("AWS_DEFAULT_REGION", "us-east-1")):
        monkeypatch.setenv(k, v)
    with mock_aws():
        res = boto3.resource("dynamodb", region_name="us-east-1")
        t = res.create_table(TableName="fp-registry", BillingMode="PAY_PER_REQUEST",
                             KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                                        {"AttributeName": "SK", "KeyType": "RANGE"}],
                             AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                                   {"AttributeName": "SK", "AttributeType": "S"}])
        t.put_item(Item={"PK": "INCIDENT#INC-EX-017-20261115T1430", "SK": "META", "machine_id": "EX-017",
                         "opened_at": "2026-11-15T14:31:00Z"})
        t.put_item(Item={"PK": "STOCK#PRT-17#PACKING", "SK": "META"})
        assert bf.run(t, dry_run=True) == {"mineria": 1, "agro": 1}
        assert "tenant_id" not in t.get_item(Key={"PK": "STOCK#PRT-17#PACKING", "SK": "META"})["Item"]
        assert bf.run(t, dry_run=False) == {"mineria": 1, "agro": 1}
        meta = t.get_item(Key={"PK": "INCIDENT#INC-EX-017-20261115T1430", "SK": "META"})["Item"]
        assert meta["tenant_id"] == "mineria" and meta["GSI3PK"] == "TENANT#mineria"
        assert bf.run(t, dry_run=False) == {}
