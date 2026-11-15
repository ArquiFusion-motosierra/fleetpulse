"""Fixtures de aws/supply: entorno, reloj fijo y tablas moto (fp-registry con GSI1-3, fp-outbox).

Nombres `supply_*` y sin autouse a proposito: CI corre todas las carpetas tests/ en una sola sesion y un fixture con
el mismo nombre que el de otro servicio lo pisaria.
"""
import boto3
import pytest
from moto import mock_aws
from supply_support import NOW, make_tables

from fplib import clock

ENV = (("AWS_ACCESS_KEY_ID", "x"), ("AWS_SECRET_ACCESS_KEY", "x"), ("AWS_DEFAULT_REGION", "us-east-1"),
       ("REGISTRY_TABLE", "fp-registry"), ("OUTBOX_TABLE", "fp-outbox"),
       ("NOTIFY_TOPIC_ARN", "arn:aws:sns:us-east-1:000000000000:fp-notify"),
       ("SUPPLY_FLOW_ARN", "arn:aws:states:us-east-1:000000000000:stateMachine:supply-flow"), ("DEMO_WARP", "1"))


@pytest.fixture
def supply_env(monkeypatch):
    for k, v in ENV:
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(clock, "now", lambda: NOW)


@pytest.fixture
def supply_ddb(supply_env):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        make_tables(client)
        yield client
