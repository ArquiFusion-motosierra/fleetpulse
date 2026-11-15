"""scripts/backfill_domain_events.py: EV# de fp-registry -> load jobs, con fp-inbox compartido con gcp-events (moto)."""
import json
import os
import sys
from pathlib import Path

import boto3
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import backfill_domain_events as bde  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples"


class FakeGcp:
    def __init__(self):
        self.uploads, self.loads = [], []

    def upload(self, bucket, name, data):
        self.uploads.append(name)

    def load(self, project, dataset, table, uris):
        self.loads.append((table, uris))


def _tables(ddb):
    ddb.create_table(TableName="fp-registry", BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                     AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                           {"AttributeName": "SK", "AttributeType": "S"}])
    ddb.create_table(TableName="fp-inbox", BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "message_id", "KeyType": "HASH"}],
                     AttributeDefinitions=[{"AttributeName": "message_id", "AttributeType": "S"}])


def _ev(ddb, env):
    ddb.put_item(TableName="fp-registry", Item={
        "PK": {"S": f"INCIDENT#{env['incident_id']}"}, "SK": {"S": f"EV#{env['occurred_at']}#{env['event_id']}"},
        "event_type": {"S": env["event_type"]}, "payload": {"S": json.dumps(env)}})


def test_dry_run_counts_then_load_is_idempotent(monkeypatch, capsys):
    for k, v in (("AWS_ACCESS_KEY_ID", "x"), ("AWS_SECRET_ACCESS_KEY", "x"), ("AWS_DEFAULT_REGION", "us-east-1")):
        monkeypatch.setenv(k, v)
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        _tables(ddb)
        for p in ("incident/opened.json", "incident/closed.json", "part/shortage.json"):
            _ev(ddb, json.loads((EXAMPLES / p).read_text()))
        ddb.put_item(TableName="fp-registry", Item={  # EV# viejo: type + payload sin sobre
            "PK": {"S": "INCIDENT#INC-EX-001-20261114T1000"}, "SK": {"S": "EV#2026-11-14T10:00:00Z#x"},
            "type": {"S": "incident.opened"}, "payload": {"S": "{\"score\": 2.5}"}})
        ddb.put_item(TableName="fp-registry", Item={"PK": {"S": "STOCK#PRT-04#NORTE"}, "SK": {"S": "META"}})
        gcp = FakeGcp()

        dry = bde.main(["--dry-run"], ddb=ddb, gcp=gcp)
        assert dry["pending"] == 3 and dry["skipped"] == {"EV# viejo sin sobre": 1}
        assert dry["by_type"] == {"incident.opened": 1, "incident.closed": 1, "part.shortage": 1}
        assert gcp.uploads == [] and ddb.scan(TableName="fp-inbox")["Count"] == 0

        real = bde.main([], ddb=ddb, gcp=gcp)
        assert len(real["objects"]) == 1 and gcp.loads == [("domain_events", [f"gs://fp-landing-g19-2026/"
                                                                              f"{real['objects'][0]}"])]
        assert ddb.scan(TableName="fp-inbox")["Count"] == 3

        again = bde.main([], ddb=ddb, gcp=gcp)
        assert again["pending"] == 0 and again["already_loaded"] == 3 and len(gcp.loads) == 1
    capsys.readouterr()
