import json

import pytest

from pub.handler import handler

TOPIC = "arn:aws:sns:us-east-1:123456789012:fp-domain-events"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DOMAIN_TOPIC_ARN", TOPIC)


class FakeSns:
    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)

    def publish(self, **kwargs):
        if len(self.calls) + 1 in self.fail_on:
            self.calls.append(kwargs)
            raise RuntimeError("sns down")
        self.calls.append(kwargs)
        return {"MessageId": "m"}


def _record(event_id, *, name="INSERT", incident_id="inc-1", aggregate_type="incident", payload=None):
    envelope = payload or json.dumps({"event_id": event_id, "aggregate_type": aggregate_type})
    image = {
        "event_id": {"S": event_id},
        "type": {"S": "incident.opened"},
        "aggregate_id": {"S": "agg-1"},
        "payload": {"S": envelope},
        "created_at": {"S": "2026-11-14T10:00:00Z"},
        "ttl": {"N": "1800000000"},
    }
    if incident_id is not None:
        image["incident_id"] = {"S": incident_id}
    return {"eventID": f"es-{event_id}", "eventName": name, "dynamodb": {"NewImage": image}}


def test_publishes_insert_with_attributes():
    sns = FakeSns()
    rec = _record("evt-1")
    out = handler({"Records": [rec]}, sns=sns)
    assert out == {"batchItemFailures": []}
    (call,) = sns.calls
    assert call["TopicArn"] == TOPIC
    assert call["Message"] == rec["dynamodb"]["NewImage"]["payload"]["S"]
    assert call["MessageAttributes"] == {
        "event_type": {"DataType": "String", "StringValue": "incident.opened"},
        "aggregate_type": {"DataType": "String", "StringValue": "incident"},
        "incident_id": {"DataType": "String", "StringValue": "inc-1"},
    }


def test_omits_incident_id_when_absent_or_empty():
    sns = FakeSns()
    handler({"Records": [_record("evt-1", incident_id=None), _record("evt-2", incident_id="")]}, sns=sns)
    assert all("incident_id" not in c["MessageAttributes"] for c in sns.calls)
    assert len(sns.calls) == 2


def test_ignores_modify_and_remove():
    sns = FakeSns()
    out = handler({"Records": [_record("evt-1", name="MODIFY"), _record("evt-2", name="REMOVE")]}, sns=sns)
    assert out == {"batchItemFailures": []}
    assert sns.calls == []


def test_partial_failure_reports_event_id():
    sns = FakeSns(fail_on={2})
    recs = [_record("evt-1"), _record("evt-2"), _record("evt-3")]
    out = handler({"Records": recs}, sns=sns)
    assert out == {"batchItemFailures": [{"itemIdentifier": "es-evt-2"}]}
    assert len(sns.calls) == 3  # el fallo no corta el resto del lote


def test_bad_payload_is_reported_not_raised():
    sns = FakeSns()
    out = handler({"Records": [_record("evt-1", payload="not json")]}, sns=sns)
    assert out == {"batchItemFailures": [{"itemIdentifier": "es-evt-1"}]}
    assert sns.calls == []
