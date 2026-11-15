import datetime as dt
import os

import pytest

os.environ.update(
    SRC_BUCKET="fp-raw-123",
    DST_BUCKET="fp-landing-test",
    GCP_PROJECT="proj",
    DATASET="fleetpulse",
    STATE_TABLE="fp-state",
    SA_SECRET="fleetpulse/gcp/sa-exporter",
)

from exp import handler  # noqa: E402


def test_hour_of():
    assert handler.hour_of("telemetry/dt=2026-11-15/hr=14/x.ndjson.gz") == "2026-11-15T14"
    assert handler.hour_of("events/dt=2026-11-15/x.json") is None


def test_due_hours_skips_current_and_before_watermark():
    keys = [
        "telemetry/dt=2026-11-15/hr=12/a.ndjson.gz",
        "telemetry/dt=2026-11-15/hr=13/b.ndjson.gz",
        "telemetry/dt=2026-11-15/hr=14/c.ndjson.gz",
    ]
    # watermark en 12, hora actual 14 -> solo 13 (12 ya hecha, 14 aun abierta)
    assert handler.due_hours(keys, "2026-11-15T12", "2026-11-15T14") == ["2026-11-15T13"]


class FakeBody:
    def __init__(self, data):
        self._d = data

    def read(self):
        return self._d


class FakeS3:
    def __init__(self, keys):
        self._keys = keys
        self.gotten = []

    def list_objects_v2(self, **kw):
        return {"Contents": [{"Key": k} for k in self._keys], "IsTruncated": False}

    def get_object(self, Bucket, Key):
        self.gotten.append(Key)
        return {"Body": FakeBody(b"gzdata")}


class FakeGcp:
    def __init__(self):
        self.uploads = []
        self.loads = []

    def upload(self, bucket, name, data):
        self.uploads.append((bucket, name))

    def load(self, project, dataset, table, uris):
        self.loads.append((table, list(uris)))


class FakeTable:
    def __init__(self, last_hour=None):
        self.item = {"last_hour": last_hour} if last_hour else None

    def get_item(self, Key):
        return {"Item": self.item} if self.item else {}

    def put_item(self, Item):
        self.item = Item


def test_handler_exports_new_closed_hours_and_advances_watermark():
    keys = [
        "telemetry/dt=2026-11-15/hr=12/a.ndjson.gz",
        "telemetry/dt=2026-11-15/hr=13/b.ndjson.gz",
        "telemetry/dt=2026-11-15/hr=13/c.ndjson.gz",
        "telemetry/dt=2026-11-15/hr=14/d.ndjson.gz",
    ]
    s3, gcp, table = FakeS3(keys), FakeGcp(), FakeTable(last_hour="2026-11-15T12")
    now = dt.datetime(2026, 11, 15, 14, 30, tzinfo=dt.timezone.utc)

    out = handler.handler(s3=s3, gcp=gcp, table=table, now=now)

    assert out == {"hours": 1, "exported": ["2026-11-15T13"]}
    assert s3.gotten == [
        "telemetry/dt=2026-11-15/hr=13/b.ndjson.gz",
        "telemetry/dt=2026-11-15/hr=13/c.ndjson.gz",
    ]
    assert len(gcp.uploads) == 2 and len(gcp.loads) == 1
    assert gcp.loads[0][0] == "telemetry"
    assert table.item["last_hour"] == "2026-11-15T13"


def test_handler_noop_when_nothing_new():
    keys = ["telemetry/dt=2026-11-15/hr=14/d.ndjson.gz"]
    s3, gcp, table = FakeS3(keys), FakeGcp(), FakeTable(last_hour="2026-11-15T13")
    now = dt.datetime(2026, 11, 15, 14, 5, tzinfo=dt.timezone.utc)

    out = handler.handler(s3=s3, gcp=gcp, table=table, now=now)

    assert out == {"hours": 0, "exported": []}
    assert gcp.loads == [] and table.item["last_hour"] == "2026-11-15T13"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
