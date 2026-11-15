"""exp/events.py (gcp-events): sobre -> fila de domain_events, un lote -> un NDJSON y un load job, idempotencia
con fp-inbox y fallas parciales. GCP y DynamoDB falsos, sin red."""
import datetime as dt
import gzip
import json
import os
from pathlib import Path

import pytest

os.environ.update(
    DST_BUCKET="fp-landing-test",
    GCP_PROJECT="proj",
    DATASET="fleetpulse",
    INBOX_TABLE="fp-inbox",
    SA_SECRET="fleetpulse/gcp/sa-exporter",
)

from exp import events  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[4] / "contracts" / "examples"
NOW = dt.datetime(2026, 11, 15, 16, 30, tzinfo=dt.timezone.utc)


def example(path: str) -> dict:
    return json.loads((EXAMPLES / path).read_text())


class FakeGcp:
    def __init__(self, fail_load=False):
        self.uploads = []
        self.loads = []
        self.fail_load = fail_load

    def upload(self, bucket, name, data):
        self.uploads.append((bucket, name, data))

    def load(self, project, dataset, table, uris):
        if self.fail_load:
            raise RuntimeError("load job: boom")
        self.loads.append((project, dataset, table, list(uris)))

    def rows(self, i=0):
        return [json.loads(line) for line in gzip.decompress(self.uploads[i][2]).decode().splitlines()]


class FakeDdb:
    """fp-inbox en memoria con la forma del cliente de bajo nivel (batch_get_item / batch_write_item)."""

    def __init__(self, items=(), unprocessed_once=False):
        self.items = {k: {} for k in items}
        self.unprocessed_once = unprocessed_once
        self.writes = 0

    def batch_get_item(self, RequestItems):
        (table, req), = RequestItems.items()
        assert len(req["Keys"]) <= 100
        hits = [k for k in req["Keys"] if k["message_id"]["S"] in self.items]
        return {"Responses": {table: hits}}

    def batch_write_item(self, RequestItems):
        (table, puts), = RequestItems.items()
        assert len(puts) <= 25
        self.writes += 1
        rest = []
        if self.unprocessed_once:  # DynamoDB acepta solo el primero
            self.unprocessed_once = False
            puts, rest = puts[:1], puts[1:]
        for p in puts:
            item = p["PutRequest"]["Item"]
            self.items[item["message_id"]["S"]] = item
        return {"UnprocessedItems": {table: rest}} if rest else {}


def sqs(*envs, raw=True):
    recs = []
    for i, env in enumerate(envs):
        body = json.dumps(env) if raw else json.dumps({"Type": "Notification", "Message": json.dumps(env)})
        recs.append({"messageId": f"m{i}", "body": body})
    return {"Records": recs}


def test_incident_opened_row():
    env = example("incident/opened.json")
    row = events.to_row(env, "2026-11-15T16:30:00Z")
    assert set(row) == {f["name"] for f in json.loads(
        (EXAMPLES.parent.parent / "gcp/terraform/schemas/domain_events.json").read_text())}
    assert row["event_type"] == "incident.opened"
    assert row["incident_id"] == env["incident_id"] and row["machine_id"] == env["machine_id"]
    assert row["occurred_at"] == env["occurred_at"] and row["ingested_at"] == "2026-11-15T16:30:00Z"
    assert row["tenant_id"] is None  # sobre anterior al multi-tenancy: NULL = mineria


def test_incident_closed_keeps_parts_used_as_json_object():
    row = events.to_row(example("incident/closed.json"), "x")
    # columna JSON: el payload va como objeto para que JSON_QUERY_ARRAY(payload, '$.parts_used') funcione
    assert isinstance(row["payload"], dict)
    assert row["payload"]["parts_used"][0] == {"part_id": "PRT-04", "zone_id": "NORTE", "qty": 1}


@pytest.mark.parametrize("path", ["part/reserved.json", "part/shortage-with-zone.json", "part/reserve-requested.json"])
def test_part_events_carry_incident_and_machine(path):
    env = example(path)
    row = events.to_row(env, "x")
    assert row["event_type"].startswith("part.")
    assert row["incident_id"] == env["incident_id"] and row["aggregate_type"] == "part_reservation"
    if row["event_type"] == "part.shortage":
        assert row["payload"]["missing"]  # lo lee v_stockouts_daily


@pytest.mark.parametrize("path", ["supply/suggested.json", "supply/po-ordered.json", "supply/received.json"])
def test_supply_events_fill_tenant_and_aggregate(path):
    env = example(path)
    row = events.to_row(env, "x")
    assert row["tenant_id"] == "mineria"
    assert row["aggregate_type"] in ("purchase_order", "transfer") and row["aggregate_id"] == env["aggregate_id"]
    assert row["incident_id"] == (env.get("incident_id") or env["payload"].get("incident_id"))


def test_batch_is_one_ndjson_and_one_load_job():
    envs = [example(p) for p in ("incident/opened.json", "incident/closed.json", "part/shortage.json",
                                 "supply/suggested.json", "kpi.daily/day.json", "report/created-event.json")]
    gcp, ddb = FakeGcp(), FakeDdb()

    out = events.handler(sqs(*envs), gcp=gcp, ddb=ddb, now=NOW)

    assert out == {"batchItemFailures": []}
    assert len(gcp.uploads) == 1 and len(gcp.loads) == 1
    bucket, name, _ = gcp.uploads[0]
    assert bucket == "fp-landing-test" and name.startswith("events/dt=") and name.endswith(".ndjson.gz")
    assert gcp.loads[0] == ("proj", "fleetpulse", "domain_events", [f"gs://fp-landing-test/{name}"])
    assert [r["event_id"] for r in gcp.rows()] == [e["event_id"] for e in envs]
    assert len(ddb.items) == len(envs) and all(k.startswith("gcp-events:") for k in ddb.items)


def test_sns_envelope_without_raw_delivery_is_unwrapped():
    gcp = FakeGcp()
    events.handler(sqs(example("incident/opened.json"), raw=False), gcp=gcp, ddb=FakeDdb(), now=NOW)
    assert gcp.rows()[0]["event_type"] == "incident.opened"


def test_redelivery_is_skipped_and_duplicates_in_batch_collapse():
    opened, closed = example("incident/opened.json"), example("incident/closed.json")
    gcp, ddb = FakeGcp(), FakeDdb(items=[f"gcp-events:{opened['event_id']}"])

    out = events.handler(sqs(opened, closed, closed), gcp=gcp, ddb=ddb, now=NOW)

    assert out == {"batchItemFailures": []}
    assert [r["event_id"] for r in gcp.rows()] == [closed["event_id"]]

    # el mismo lote otra vez (reintento de SQS): ya todo esta en fp-inbox, ningun load job
    out = events.handler(sqs(opened, closed), gcp=gcp, ddb=ddb, now=NOW)
    assert out == {"batchItemFailures": []} and len(gcp.loads) == 1


def test_object_name_is_deterministic():
    rows = [events.to_row(example(p), "x") for p in ("incident/opened.json", "incident/closed.json")]
    assert events.object_name(rows) == events.object_name(list(reversed(rows)))
    assert events.ndjson_gz(rows) == events.ndjson_gz(rows)


def test_invalid_envelope_fails_only_its_message():
    bad = example("incident/opened.json") | {"event_id": "no-es-uuid"}
    gcp = FakeGcp()

    out = events.handler({"Records": [{"messageId": "ok", "body": json.dumps(example("incident/closed.json"))},
                                      {"messageId": "bad", "body": json.dumps(bad)},
                                      {"messageId": "junk", "body": "{no json"}]},
                         gcp=gcp, ddb=FakeDdb(), now=NOW)

    assert out == {"batchItemFailures": [{"itemIdentifier": "bad"}, {"itemIdentifier": "junk"}]}
    assert len(gcp.loads) == 1 and len(gcp.rows()) == 1


def test_load_failure_fails_every_valid_message_and_marks_nothing():
    gcp, ddb = FakeGcp(fail_load=True), FakeDdb()
    out = events.handler(sqs(example("incident/opened.json"), example("incident/closed.json")), gcp=gcp, ddb=ddb,
                         now=NOW)
    assert out == {"batchItemFailures": [{"itemIdentifier": "m0"}, {"itemIdentifier": "m1"}]}
    assert ddb.items == {}


def test_inbox_unprocessed_items_are_retried(monkeypatch):
    monkeypatch.setattr(events.time, "sleep", lambda s: None)
    ddb = FakeDdb(unprocessed_once=True)
    events.handler(sqs(example("incident/opened.json"), example("incident/closed.json")), gcp=FakeGcp(), ddb=ddb,
                   now=NOW)
    assert len(ddb.items) == 2 and ddb.writes == 2


def test_never_streaming_inserts():
    src = "".join(p.read_text() for p in (Path(events.__file__).parent).glob("*.py"))
    assert "insertAll" not in src and "tabledata" not in src and "insert_rows" not in src
    assert '"writeDisposition": "WRITE_APPEND"' in src
