"""Fixtures de aws/reports: entorno y reloj fijo, tablas moto (fp-state, fp-registry, fp-outbox), Cloud Run, Secrets
Manager y Step Functions con dobles (sin red).

Nombres `reports_*` y sin autouse a proposito: CI corre todas las carpetas tests/ en una sola sesion y el rootdir
queda en el pytest.ini de un servicio, asi que los conftest de otros servicios se registran como globales; un
`_env` o `ddb` con el mismo nombre que los de aws/incidents pisaria los suyos. Cada modulo de prueba los aliasa.
"""
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws
from reports_support import AUDIENCE, NOW, SA_KEY, T33_EXAMPLE, FakeHttp, FakeSecrets, FakeSfn, make_table

from fplib import clock, gcp_auth
from fplib import secrets as fp_secrets

ENV = (("AWS_ACCESS_KEY_ID", "x"), ("AWS_SECRET_ACCESS_KEY", "x"), ("AWS_DEFAULT_REGION", "us-east-1"),
       ("STATE_TABLE", "fp-state"), ("REGISTRY_TABLE", "fp-registry"), ("OUTBOX_TABLE", "fp-outbox"),
       ("INVOKER_SECRET", "fleetpulse/gcp/sa-invoker"),
       ("STATE_MACHINE_ARN", "arn:aws:states:us-east-1:000000000000:stateMachine:incident-flow"))


@pytest.fixture
def reports_env(monkeypatch):
    for k, v in ENV:
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(clock, "now", lambda: NOW)
    fp_secrets.clear_cache()
    gcp_auth.clear_cache()
    yield
    fp_secrets.clear_cache()
    gcp_auth.clear_cache()


@pytest.fixture
def reports_ddb(reports_env):
    with mock_aws():
        res = boto3.resource("dynamodb", region_name="us-east-1")
        outbox = res.create_table(
            TableName="fp-outbox", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}])
        yield SimpleNamespace(state=make_table(res, "fp-state", ("GSI1",)),
                              registry=make_table(res, "fp-registry", ("GSI1", "GSI2")), outbox=outbox)


@pytest.fixture
def reports_invoker(reports_env, monkeypatch):
    """Secreto fleetpulse/gcp/sa-invoker y un ID token fijo (fplib.gcp_auth tiene su propia prueba)."""
    calls = []

    def fake_id_token(sa_key, audience, now=None):
        calls.append((sa_key["client_email"], audience))
        return "id-tok-123"

    monkeypatch.setattr(gcp_auth, "id_token", fake_id_token)
    return SimpleNamespace(secrets=FakeSecrets({"fleetpulse/gcp/sa-invoker": {"key": SA_KEY,
                                                                              "audience": AUDIENCE + "/"}}),
                           token_calls=calls)


@pytest.fixture
def reports_svc(reports_ddb, reports_invoker):
    """Invoca el handler `reports` con tablas moto y dobles; `svc.http` / `svc.sfn` se pueden reemplazar."""
    import json

    from api import reports

    ddb = reports_ddb
    ns = SimpleNamespace(ddb=ddb, http=FakeHttp((200, json.dumps(T33_EXAMPLE).encode())), sfn=FakeSfn(),
                         secrets=reports_invoker.secrets)

    def call(event):
        return reports.handler(event, registry=ddb.registry, state=ddb.state, outbox=ddb.outbox, sfn=ns.sfn,
                               secrets=ns.secrets, http=ns.http)

    ns.call = call
    return ns
