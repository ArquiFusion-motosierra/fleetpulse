"""Pruebas de handler.py con moto DynamoDB, eventos de stream tipados y un sfn falso."""
import json
from datetime import timedelta
from decimal import Decimal

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from moto import mock_aws

from fplib import clock
from scoring import baselines, handler

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
            "engine_status": "operating", **{v: Decimal(str(MU[v])) for v in MU}, **over}


def drift_batch(batch="B1", start="2026-11-14T10:01:00Z", machine="EX-017"):
    t0 = clock.parse(start)
    # presion z -5 y aceite z +3: 1.0 * (5 - 2) + 0.9 * (3 - 2) = 3.9 (immediate)
    return [reading(clock.iso(t0.replace(second=i)), machine, batch, hydraulic_pressure_bar=Decimal("250"),
                    hydraulic_oil_temp_c=Decimal("92")) for i in range(10)]


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
    assert float(s["score"]) == pytest.approx(3.9) and s["severity"] == "immediate" and s["batch_id"] == "B1"
    assert float(s["z"]["hydraulic_pressure_bar"]["self"]) == pytest.approx(-5.0)
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


def test_failing_group_does_not_block_others_and_reraises(table):
    seed_meta(table, "EX-001")
    seed_meta(table, "EX-017")

    class Boom(FakeSfn):
        def start_execution(self, **kw):
            if "EX-001" in kw["name"]:
                raise RuntimeError("sfn down")
            return super().start_execution(**kw)

    items = drift_batch("B1", "2026-11-14T10:01:00Z", "EX-001") + drift_batch("B2", "2026-11-14T10:02:00Z")
    sfn = Boom()
    with pytest.raises(RuntimeError):
        handler.handler(event(items), table=table, sfn=sfn)
    assert table.get_item(Key={"PK": "MACHINE#EX-017", "SK": "S#2026-11-14T10:02:09Z"}).get("Item")
    assert [c["name"] for c in sfn.calls] == ["INC-EX-017-20261114T1000"]


def test_meta_missing_optional_fields_still_starts(table):
    seed_meta(table)
    table.update_item(Key={"PK": "MACHINE#EX-017", "SK": "META"}, UpdateExpression="REMOVE serial, hours_total")
    sfn = FakeSfn()
    handler.handler(event(drift_batch()), table=table, sfn=sfn)
    inp = json.loads(sfn.calls[0]["input"])
    assert inp["serial"] == "" and inp["hours_total"] == 0


def test_batch_without_operating_readings_is_not_scored(table):
    seed_meta(table)
    items = [reading(f"2026-11-14T10:01:{i:02d}Z", engine_status="idle" if i % 2 else "off",
                     hydraulic_pressure_bar=Decimal("105")) for i in range(10)]
    sfn = FakeSfn()
    out = handler.handler(event(items), table=table, sfn=sfn)
    assert out == {"scored": 0, "started": []}
    assert not table.query(KeyConditionExpression=Key("PK").eq("MACHINE#EX-017") & Key("SK").begins_with("S#"))["Items"]
    assert sfn.calls == []


def _history(t, n_operating, n_other=0, start="2026-11-14T06:00:00Z"):
    """`n_operating` lecturas operating (+-1 sigma alrededor de MU) y `n_other` idle/off intercaladas, 1 por minuto."""
    t0 = clock.parse(start)
    rows = []
    for i in range(n_operating + n_other):
        over = {v: Decimal(str(MU[v] + (i % 3 - 1) * SIGMA[v])) for v in MU}
        if i < n_other:
            over["engine_status"] = "idle" if i % 2 else "off"
        rows.append(reading(clock.iso(t0 + timedelta(minutes=i)), **over))
    for r in rows:
        t.put_item(Item=r)
    return rows


def test_bootstrap_persists_baseline_from_200_operating_readings(table):
    seed_meta(table, baseline=False)
    rows = _history(table, 200, 30)
    sfn = FakeSfn()
    out = handler.handler(event(rows[-10:]), table=table, sfn=sfn)
    assert out["scored"] == 1
    meta = table.get_item(Key={"PK": "MACHINE#EX-017", "SK": "META"})["Item"]
    assert meta["baseline_n"] == 200
    assert set(meta["baseline"]) == set(MU)
    assert all({"mu", "sigma"} <= set(b) for b in meta["baseline"].values())
    assert table.query(KeyConditionExpression=Key("PK").eq("MACHINE#EX-017") & Key("SK").begins_with("S#"))["Items"]
    # segunda llamada: lee lo persistido, no recalcula (aunque las lecturas ya no esten)
    for r in rows:
        table.delete_item(Key={"PK": r["PK"], "SK": r["SK"]})
    own, _ = baselines.for_machine(table, "EX-017", "CAT320")
    assert own["engine_rpm"]["mu"] == pytest.approx(float(meta["baseline"]["engine_rpm"]["mu"]))


def test_bootstrap_waits_for_200_operating_readings(table):
    seed_meta(table, baseline=False)
    rows = _history(table, 199, 60)
    out = handler.handler(event(rows[-10:]), table=table, sfn=FakeSfn())
    assert out["scored"] == 0
    assert "baseline" not in table.get_item(Key={"PK": "MACHINE#EX-017", "SK": "META"})["Item"]


def test_bootstrap_applies_the_relative_sigma_floor(table):
    seed_meta(table, baseline=False)
    t0 = clock.parse("2026-11-14T06:00:00Z")
    for i in range(200):  # valores constantes: sigma muestral 0
        table.put_item(Item=reading(clock.iso(t0 + timedelta(minutes=i))))
    own, _ = baselines.for_machine(table, "EX-017", "CAT320")
    assert own["engine_rpm"]["sigma"] == pytest.approx(0.05 * MU["engine_rpm"])


def test_bootstrap_never_overwrites_a_seeded_baseline(table, monkeypatch):
    """seed-baselines.py escribe entre la lectura de META y la escritura del arranque: gana la sembrada."""
    seed_meta(table, baseline=False)
    _history(table, 200)
    seeded = {v: {"mu": Decimal(MU[v]), "sigma": Decimal("7")} for v in MU}
    real_query = table.query

    def query_then_seed(**kw):
        out = real_query(**kw)
        baselines.persist(table, "EX-017", {v: {"mu": float(MU[v]), "sigma": 7.0} for v in MU}, 1440)
        return out

    monkeypatch.setattr(table, "query", query_then_seed)
    own, _ = baselines.for_machine(table, "EX-017", "CAT320")
    meta = table.get_item(Key={"PK": "MACHINE#EX-017", "SK": "META"})["Item"]
    assert meta["baseline"] == seeded and meta["baseline_n"] == 1440
    assert own["engine_rpm"]["sigma"] == 7.0


def test_persist_requires_meta(table):
    from botocore.exceptions import ClientError
    with pytest.raises(ClientError):
        baselines.persist(table, "EX-999", {"engine_rpm": {"mu": 1.0, "sigma": 1.0}}, 1)
    assert "Item" not in table.get_item(Key={"PK": "MACHINE#EX-999", "SK": "META"})


class _PagedTable:
    """Devuelve las lecturas en paginas de 300 (como el tope de 1 MB de DynamoDB) y registra cada query."""

    def __init__(self, n):
        self.items = [{"SK": f"R#{i:05d}", "engine_status": "operating"} for i in range(n)]
        self.calls = []

    def query(self, **kw):
        self.calls.append(kw)
        start = int(kw["ExclusiveStartKey"]["SK"][2:]) + 1 if "ExclusiveStartKey" in kw else 0
        page = self.items[start:start + min(300, kw["Limit"])]
        out = {"Items": page}
        if start + len(page) < len(self.items):
            out["LastEvaluatedKey"] = {"SK": page[-1]["SK"]}
        return out


def test_recent_readings_paginates_up_to_lookback():
    t = _PagedTable(2500)
    rows = baselines.recent_readings(t, "MACHINE#EX-017")
    assert len(rows) == baselines.LOOKBACK == 2000
    assert [c["Limit"] for c in t.calls] == [2000, 1700, 1400, 1100, 800, 500, 200]
    assert all(c["ScanIndexForward"] is False for c in t.calls)
    short = _PagedTable(450)
    assert len(baselines.recent_readings(short, "MACHINE#EX-017")) == 450 and len(short.calls) == 2
