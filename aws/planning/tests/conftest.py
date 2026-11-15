"""Fixtures de daily-planning: entorno, reloj fijo y tablas moto (fp-state, fp-registry, fp-outbox, fp-inbox)."""
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws
from planning_support import NOW, make_table

from fplib import clock

ENV = (("AWS_ACCESS_KEY_ID", "x"), ("AWS_SECRET_ACCESS_KEY", "x"), ("AWS_DEFAULT_REGION", "us-east-1"),
       ("STATE_TABLE", "fp-state"), ("REGISTRY_TABLE", "fp-registry"), ("OUTBOX_TABLE", "fp-outbox"),
       ("INBOX_TABLE", "fp-inbox"))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k, v in ENV:
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(clock, "now", lambda: NOW)


@pytest.fixture
def ddb():
    with mock_aws():
        res = boto3.resource("dynamodb", region_name="us-east-1")
        yield SimpleNamespace(
            client=boto3.client("dynamodb", region_name="us-east-1"),
            state=make_table(res, "fp-state", ("GSI1",)),
            registry=make_table(res, "fp-registry", ("GSI1", "GSI2")),
            outbox=make_table(res, "fp-outbox", hash_key="event_id"),
            inbox=make_table(res, "fp-inbox", hash_key="message_id"))
