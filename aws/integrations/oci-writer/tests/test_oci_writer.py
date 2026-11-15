import io
import json
import os
import urllib.error
from datetime import datetime, timezone

import jsonschema

from ociw import handler
from ociw.ords import OrdsClient

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
EXAMPLES = os.path.join(ROOT, "contracts", "examples")


def load(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return json.load(f)


class Resp(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOrds:
    """ORDS en memoria: token OAuth y POST v1/events con inbox por event_id."""

    def __init__(self, reservation=None, expire_first_token=False):
        self.processed = set()
        self.calls = []
        self.tokens = 0
        self.reservation = reservation
        self.expire_first_token = expire_first_token

    def __call__(self, req, timeout):
        assert timeout == 8.0
        self.calls.append(req.full_url)
        if req.full_url.endswith("oauth/token"):
            assert req.headers["Authorization"].startswith("Basic ")
            self.tokens += 1
            return Resp(200, {"access_token": f"tok{self.tokens}", "expires_in": 3600})
        if self.expire_first_token and req.headers["Authorization"] == "Bearer tok1":
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))
        event = json.loads(req.data)
        if event["event_id"] in self.processed:
            return Resp(200, {"status": "duplicate", "event_id": event["event_id"], "reservation": None})
        self.processed.add(event["event_id"])
        res = self.reservation if event["event_type"] == "part.reserve.requested" else None
        return Resp(200, {"status": "applied", "event_id": event["event_id"], "reservation": res})


class Secrets:
    def get_secret_value(self, SecretId):
        assert SecretId == "fleetpulse/oci/ords"
        return {"SecretString": json.dumps({"base_url": "https://x.adb.us-ashburn-1.oraclecloudapps.com/ords/fleet",
                                            "client_id": "cid", "client_secret": "csecret"})}


class Table:
    def __init__(self):
        self.items = []

    def put_item(self, Item, ConditionExpression):
        self.items.append(Item)


def sqs(*events, sns=False):
    recs = []
    for i, e in enumerate(events):
        body = json.dumps({"Type": "Notification", "Message": json.dumps(e)}) if sns else json.dumps(e)
        recs.append({"messageId": f"m{i}", "body": body})
    return {"Records": recs}


def test_applies_event_and_resending_same_event_id_is_duplicate():
    fake = FakeOrds()
    client = OrdsClient(secrets=Secrets(), opener=fake)
    opened = load("contracts/examples/incident/opened.json")
    assert handler.handler(sqs(opened), client=client, table=Table()) == {"batchItemFailures": []}
    assert handler.process({"messageId": "x", "body": json.dumps(opened)}, client)["status"] == "duplicate"
    assert fake.tokens == 1  # token cacheado
    assert fake.calls[-1] == "https://x.adb.us-ashburn-1.oraclecloudapps.com/ords/fleet/v1/events"


def test_sns_wrapped_body_is_unwrapped():
    fake = FakeOrds()
    out = handler.handler(sqs(load("contracts/examples/incident/assigned.json"), sns=True),
                          client=OrdsClient(secrets=Secrets(), opener=fake), table=Table())
    assert out == {"batchItemFailures": []} and len(fake.processed) == 1


def test_reservation_produces_contract_valid_follow_up_events():
    reservation = {"reserved": [{"reservation_id": 7, "part_id": "PRT-03", "zone_id": "NORTE", "qty": 2}],
                   "missing": [{"part_id": "PRT-04", "qty": 1}], "reorder_suggested": True}
    table = Table()
    handler.handler(sqs(load("contracts/examples/part/reserve-requested.json")),
                    client=OrdsClient(secrets=Secrets(), opener=FakeOrds(reservation)), table=table)
    schema = load("contracts/part.schema.json")
    types = []
    for item in table.items:
        e = json.loads(item["payload"])
        jsonschema.validate(e, schema)
        types.append(e["event_type"])
    assert types == ["part.reserved", "part.shortage"]


def test_expired_token_is_renewed_once():
    fake = FakeOrds(expire_first_token=True)
    out = handler.handler(sqs(load("contracts/examples/incident/opened.json")),
                          client=OrdsClient(secrets=Secrets(), opener=fake), table=Table())
    assert out == {"batchItemFailures": []} and fake.tokens == 2


def test_ords_error_reports_only_failed_message():
    class Broken(FakeOrds):
        def __call__(self, req, timeout):
            if req.full_url.endswith("v1/events") and json.loads(req.data)["event_type"] == "incident.closed":
                raise urllib.error.HTTPError(req.full_url, 400, "Bad", {}, io.BytesIO(b'{"error":"ORA-1"}'))
            return super().__call__(req, timeout)

    out = handler.handler(sqs(load("contracts/examples/incident/opened.json"),
                              load("contracts/examples/incident/closed.json")),
                          client=OrdsClient(secrets=Secrets(), opener=Broken()), table=Table())
    assert out == {"batchItemFailures": [{"itemIdentifier": "m1"}]}


def test_no_follow_up_without_reservation():
    e = load("contracts/examples/incident/opened.json")
    assert handler.result_events(e, None, datetime.now(timezone.utc)) == []
