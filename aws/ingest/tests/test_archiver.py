"""Pruebas de consumidores/archiver.py con moto S3 y eventos de stream tipados."""
import gzip
import json
from decimal import Decimal

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from moto import mock_aws

from consumidores import archiver
from lib import storage

BUCKET = "fp-raw-123456789012"
SER = TypeSerializer()


@pytest.fixture
def s3(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "x"), ("AWS_SECRET_ACCESS_KEY", "x"), ("AWS_DEFAULT_REGION", "us-east-1"),
                 ("BUCKET", BUCKET)):
        monkeypatch.setenv(k, v)
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def image(item):
    return {"eventName": "INSERT", "dynamodb": {"NewImage": {k: SER.serialize(v) for k, v in item.items()}}}


def reading(ts, machine="EX-017"):
    return image({"PK": f"MACHINE#{machine}", "SK": f"R#{ts}", "machine_id": machine, "ts": ts,
                  "lat": Decimal("-13.9003"), "engine_rpm": Decimal("1850"), "fault_codes": [], "ttl": Decimal("1")})


def readings_event(stamps):
    return {"Records": [reading(t) for t in stamps]}


def test_archiver_writes_one_gz_object_per_hour(s3):
    event = readings_event(["2026-11-15T14:35:00Z", "2026-11-15T14:36:00Z", "2026-11-15T15:01:00Z"])
    assert archiver.handler(event, s3=s3) == {"objects": 2, "records": 3}
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]]
    assert any(k.startswith("telemetry/dt=2026-11-15/hr=14/") for k in keys)
    assert any(k.startswith("telemetry/dt=2026-11-15/hr=15/") for k in keys)
    assert all(k.endswith(".ndjson.gz") for k in keys)
    k14 = next(k for k in keys if "hr=14" in k)
    lines = gzip.decompress(s3.get_object(Bucket=BUCKET, Key=k14)["Body"].read()).splitlines()
    first = json.loads(lines[0])
    assert len(lines) == 2 and first["machine_id"] == "EX-017" and first["lat"] == -13.9003
    assert first["engine_rpm"] == 1850 and isinstance(first["engine_rpm"], int) and "PK" not in first


def test_archiver_ignores_non_reading_items(s3):
    meta = image({"PK": "MACHINE#EX-017", "SK": "META", "machine_id": "EX-017"})
    assert archiver.handler({"Records": [meta]}, s3=s3) == {"objects": 0, "records": 0}
    assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET)


def test_archiver_ignores_deletes(s3):
    rec = reading("2026-11-15T14:35:00Z")
    rec["eventName"] = "REMOVE"
    assert archiver.handler({"Records": [rec]}, s3=s3) == {"objects": 0, "records": 0}


def test_s3_failure_raises_so_batch_retries(s3):
    class Boom:
        def put_object(self, **kw):
            raise RuntimeError("s3 down")
    with pytest.raises(RuntimeError):
        archiver.handler(readings_event(["2026-11-15T14:35:00Z"]), s3=Boom())


def test_object_key_format():
    key = storage.object_key("2026-11-15T14", "2026-11-15T14:35:00Z")
    assert key.startswith("telemetry/dt=2026-11-15/hr=14/20261115T143500Z-") and key.endswith(".ndjson.gz")
    assert len(key.rsplit("-", 1)[1]) == len("xxxxxxxx.ndjson.gz")


def test_ndjson_gz_roundtrip():
    out = gzip.decompress(storage.ndjson_gz([{"a": Decimal("1.5"), "b": Decimal("2")}, {"c": 1}])).decode()
    assert out == '{"a":1.5,"b":2}\n{"c":1}\n'
