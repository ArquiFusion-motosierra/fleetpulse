"""Pruebas de scripts/seed-baselines.py: historia simulada determinista y escritura idempotente en META."""
import importlib.util
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from scoring import baselines, score

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "seed-baselines.py"
END = datetime(2026, 11, 15, 14, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def seed():
    spec = importlib.util.spec_from_file_location("seed_baselines", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def table(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "x"), ("AWS_SECRET_ACCESS_KEY", "x"), ("AWS_DEFAULT_REGION", "us-east-1")):
        monkeypatch.setenv(k, v)
    with mock_aws():
        db = boto3.resource("dynamodb", region_name="us-east-1")
        yield db.create_table(
            TableName="fp-state", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}])


def test_default_end_is_the_current_hour(seed):
    assert seed.default_end(datetime(2026, 11, 15, 14, 37, 12, tzinfo=timezone.utc)) == END


def test_machine_baselines_is_deterministic_and_uses_operating_readings(seed):
    a = seed.machine_baselines(END, hours=1, step_min=1, machine_ids={"EX-017"})
    assert a == seed.machine_baselines(END, hours=1, step_min=1, machine_ids={"EX-017"})
    base, n = a["EX-017"]
    assert n >= baselines.MIN_READINGS and set(base) == set(score.VARS)
    assert all(b["sigma"] > 0 for b in base.values())
    # 09:00 Lima, EX-017 operando: rpm cerca de 1800 * (0.8 + 0.2 * lf), nunca el 750 del ralenti
    assert 1600 < base["engine_rpm"]["mu"] < 1900


def test_write_persists_only_where_meta_exists(seed, table):
    table.put_item(Item={"PK": "MACHINE#EX-017", "SK": "META", "machine_id": "EX-017", "status": "ok",
                         "baseline": {"engine_rpm": {"mu": Decimal("1"), "sigma": Decimal("1")}}})
    good = {v: {"mu": 10.0, "sigma": 2.0} for v in score.VARS}
    res = seed.write(table, {"EX-017": (good, 1440), "EX-018": (good, 1300), "VQ-031": (None, 90)})
    assert res == {"written": ["EX-017"], "no_meta": ["EX-018"], "insufficient": ["VQ-031"]}
    meta = table.get_item(Key={"PK": "MACHINE#EX-017", "SK": "META"})["Item"]
    assert meta["baseline_n"] == 1440 and meta["status"] == "ok"  # pisa la anterior, no toca lo demas
    assert meta["baseline"]["engine_rpm"] == {"mu": Decimal("10.0"), "sigma": Decimal("2.0")}
    assert "Item" not in table.get_item(Key={"PK": "MACHINE#EX-018", "SK": "META"})
    # idempotente: repetir escribe lo mismo
    seed.write(table, {"EX-017": (good, 1440)})
    assert table.get_item(Key={"PK": "MACHINE#EX-017", "SK": "META"})["Item"] == meta


def test_dry_run_does_not_touch_aws(seed, capsys, monkeypatch):
    monkeypatch.setattr(boto3, "resource", lambda *a, **k: pytest.fail("dry-run no debe crear clientes"))
    assert seed.main(["--dry-run", "--end", "2026-11-15T14:00:00Z", "--hours", "1", "--step-min", "30"]) == 0
    first, second = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    assert first["machines"] == 124  # los 3 parques de contracts/tenants.yaml
    assert first["window"] == ["2026-11-15T13:00:00+00:00", "2026-11-15T14:00:00+00:00"]
    assert second == {"dry_run": True, "would_write": 0}  # 2 lotes no llegan a MIN_READINGS
