"""Fixtures de incident-flow: entorno y reloj fijo, entrada de EX-017 y tablas moto (fp-state, fp-registry,
fp-outbox)."""
from types import SimpleNamespace

import boto3
import pytest
from incidents_support import DEVIATED, INC, LAT, LON, NOW, make_table
from moto import mock_aws

from fplib import clock, geohash
from fplib import secrets as fp_secrets

ENV = (("AWS_ACCESS_KEY_ID", "x"), ("AWS_SECRET_ACCESS_KEY", "x"), ("AWS_DEFAULT_REGION", "us-east-1"),
       ("STATE_TABLE", "fp-state"), ("REGISTRY_TABLE", "fp-registry"), ("OUTBOX_TABLE", "fp-outbox"),
       ("INVOKER_SECRET", "fleetpulse/gcp/sa-invoker"), ("API_URL_PARAM", "/fleetpulse/api/url"),
       ("NOTIFY_TOPIC_ARN", "arn:aws:sns:us-east-1:000000000000:fp-notify"))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k, v in ENV:
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(clock, "now", lambda: NOW)
    fp_secrets.clear_cache()
    yield
    fp_secrets.clear_cache()


@pytest.fixture
def ex017():
    """Entrada de la ejecucion tal como la arma `aws/scoring/scoring/handler.py` para EX-017."""
    return {
        "incident_id": INC, "machine_id": "EX-017", "model": "EX", "serial": "AFCEX01721", "zone_id": "NORTE",
        "lat": LAT, "lon": LON, "geohash6": geohash.encode(LAT, LON, 6), "ts": "2026-11-15T14:37:59Z",
        "batch_id": "B-20261115T143759Z-1a2b3c4d", "score": 2.31, "severity": "schedule",
        "z": {"hydraulic_pressure_bar": {"self": -2.6, "peer": -1.8}, "hydraulic_oil_temp_c": {"self": 2.1, "peer": 1.4},
              "vibration_mm_s": {"self": 1.9, "peer": None}},
        "deviated_vars": [dict(d) for d in DEVIATED], "failure_mode": "hydraulic_pump", "hours_total": 2762,
    }


@pytest.fixture
def ddb():
    with mock_aws():
        res = boto3.resource("dynamodb", region_name="us-east-1")
        outbox = res.create_table(
            TableName="fp-outbox", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}])
        yield SimpleNamespace(state=make_table(res, "fp-state", ("GSI1",)),
                              registry=make_table(res, "fp-registry", ("GSI1", "GSI2")), outbox=outbox)
