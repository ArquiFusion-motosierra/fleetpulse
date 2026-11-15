"""Pruebas de handler.py con moto DynamoDB, eventos de stream tipados y un sfn falso."""
import json
from decimal import Decimal

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from moto import mock_aws

from fplib import clock
from scoring import handler

SER = TypeSerializer()
ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:incident-flow"
MU = {"coolant_temp_c": 90, "hydraulic_oil_temp_c": 80, "hydraulic_pressure_bar": 300, "vibration_mm_s": 5,
      "fuel_rate_lph": 20, "engine_rpm": 1800}
SIGMA = {"coolant_temp_c": 2, "hydraulic_oil_temp_c": 4, "hydraulic_pressure_bar": 10, "vibration_mm_s": 0.5,
         "fuel_rate_lph": 1, "engine_rpm": 50}
BASELINE = {v: {"mu": Decimal(MU[v]), "sigma": Decimal(str(SIGMA[v]))} for v in MU}


class FakeSfn:
    class exceptions:
        class ExecutionAlreadyExists(Exception):
            pass

    def __init__(self):
        self.calls = []
        self.names = set()

    def start_execution(self, **kw):
        self.calls.append(kw)
        if kw["name"] in self.names:
            raise self.exceptions.ExecutionAlreadyExists(kw["name"])
        self.names.add(kw["name"])
        return {"executionArn": "arn:x"}


@pytest.fixture
def table(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "x"), ("AWS_SECRET_ACCESS_KEY", "x"), ("AWS_DEFAULT_REGION", "us-east-1"),
                 ("STATE_TABLE", "fp-state"), ("STATE_MACHINE_ARN", ARN)):
        monkeypatch.setenv(k, v)
    with mock_aws():
        db = boto3.resource("dynamodb", region_name="us-east-1")
        yield db.create_table(
            TableName="fp-state", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}])


def seed_meta(t, machine="EX-017", baseline=True):
    item = {"PK": f"MACHINE#{machine}", "SK": "META", "machine_id": machine, "model": "CAT320", "serial": "S1",
            "zone_id": "Z1", "lat": Decimal("-13.9003"), "lon": Decimal("-70.1"), "geohash6": "6abcde",
            "status": "ok", "hours_total": Decimal("1200.5"), "last_ts": "2026-11-14T10:00:00Z"}
    if baseline:
        item["baseline"] = BASELINE
    t.put_item(Item=item)
    t.put_item(Item={"PK": "MODEL#CAT320", "SK": "BASELINE", "baseline": BASELINE})


def reading(ts, machine="EX-017", batch="B1", **over):
    return {"PK": f"MACHINE#{machine}", "SK": f"R#{ts}", "machine_id": machine, "ts": ts, "batch_id": batch,
            "engine_status": "on", **{v: Decimal(str(MU[v])) for v in MU}, **over}


def drift_batch(batch="B1", start="2026-11-14T10:01:00Z", machine="EX-017"):
    t0 = clock.parse(start)
    return [reading(clock.iso(t0.replace(second=i)), machine, batch, hydraulic_pressure_bar=Decimal("270"),
                    hydraulic_oil_temp_c=Decimal("90")) for i in range(10)]


def event(items):
    return {"Records": [{"eventName": "INSERT",
                         "dynamodb": {"NewImage": {k: SER.serialize(v) for k, v in it.items()}}} for it in items]}


def test_no_baseline_no_score(table):
    seed_meta(table, baseline=False)
    items = drift_batch()
    for it in items:
        table.put_item(Item=it)
    sfn = FakeSfn()
    out = handler.handler(event(items), table=table, sfn=sfn)
    assert out == {"scored": 0, "started": []}
    q = Key("PK").eq("MACHINE#EX-017") & Key("SK").begins_with("S#")
    assert not table.query(KeyConditionExpression=q)["Items"]
    assert sfn.calls == []


def test_scores_and_starts_execution_when_drift(table):
    seed_meta(table)
    sfn = FakeSfn()
    out = handler.handler(event(drift_batch()), table=table, sfn=sfn)
    last = "2026-11-14T10:01:09Z"
    iid = "INC-EX-017-20261114T1000"
    assert out == {"scored": 1, "started": [iid]}
    s = table.get_item(Key={"PK": "MACHINE#EX-017", "SK": f"S#{last}"})["Item"]
    assert float(s["score"]) == pytest.approx(5.25) and s["severity"] == "immediate" and s["batch_id"] == "B1"
    assert float(s["z"]["hydraulic_pressure_bar"]["self"]) == pytest.approx(-3.0)
    assert "ttl" in s and "means" in s
    cell = table.get_item(Key={"PK": "CELL#6abcde", "SK": f"A#{last}#EX-017"})["Item"]
    assert cell["machine_id"] == "EX-017" and cell["severity"] == "immediate" and "ttl" in cell
    assert table.get_item(Key={"PK": "MACHINE#EX-017", "SK": "META"})["Item"]["status"] == "immediate"
    (call,) = sfn.calls
    assert call["name"] == iid and call["stateMachineArn"] == ARN
    inp = json.loads(call["input"])
    assert inp["failure_mode"] == "hydraulic_pump"
    assert set(inp) == {"incident_id", "machine_id", "model", "serial", "zone_id", "lat", "lon", "geohash6", "ts",
                        "batch_id", "score", "severity", "z", "deviated_vars", "failure_mode", "hours_total"}
    assert inp["ts"] == last and inp["lat"] == -13.9003 and inp["hours_total"] == 1200.5


def test_healthy_batch_scores_ok_without_start(table):
    seed_meta(table)
    items = [reading(f"2026-11-14T10:01:{i:02d}Z") for i in range(10)]
    sfn = FakeSfn()
    out = handler.handler(event(items), table=table, sfn=sfn)
    assert out == {"scored": 1, "started": []}
    assert sfn.calls == []
    assert not table.query(KeyConditionExpression=Key("PK").eq("CELL#6abcde"))["Items"]
    assert table.get_item(Key={"PK": "MACHINE#EX-017", "SK": "META"})["Item"]["status"] == "ok"


def test_same_window_starts_one_execution(table):
    seed_meta(table)
    items = drift_batch("B1", "2026-11-14T10:01:00Z") + drift_batch("B2", "2026-11-14T10:06:00Z")
    sfn = FakeSfn()
    out = handler.handler(event(items), table=table, sfn=sfn)
    assert out["scored"] == 2
    assert out["started"] == ["INC-EX-017-20261114T1000"]
    assert len(sfn.calls) == 2
