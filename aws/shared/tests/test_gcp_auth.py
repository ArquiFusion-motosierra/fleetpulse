"""Pruebas de fplib.gcp_auth (movido desde aws/incidents/lib): cache de 50 min, llave como texto JSON y firma real
contra un endpoint de tokens falso. Sin red."""
import base64
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from fplib import gcp_auth

NOW = datetime(2026, 11, 15, 14, 38, 5, tzinfo=timezone.utc)
AUDIENCE = "https://insights-abc123-ue.a.run.app"
SA_KEY = {"type": "service_account", "client_email": "fp-invoker@fleetpulse-g19-2026.iam.gserviceaccount.com",
          "private_key": "-----BEGIN PRIVATE KEY-----\nNO-ES-UNA-LLAVE\n-----END PRIVATE KEY-----\n"}


@pytest.fixture(autouse=True)
def _cache():
    gcp_auth.clear_cache()
    yield
    gcp_auth.clear_cache()


def test_gcp_auth_caches_the_id_token_for_50_minutes(monkeypatch):
    fetched = []
    monkeypatch.setattr(gcp_auth, "_fetch", lambda key, aud: fetched.append(aud) or f"tok-{len(fetched)}")
    assert gcp_auth.id_token(SA_KEY, AUDIENCE, now=NOW) == "tok-1"
    assert gcp_auth.id_token(SA_KEY, AUDIENCE, now=NOW + timedelta(minutes=49)) == "tok-1"
    assert gcp_auth.id_token(SA_KEY, AUDIENCE, now=NOW + timedelta(minutes=51)) == "tok-2"
    assert gcp_auth.id_token(SA_KEY, "https://otra.run.app", now=NOW + timedelta(minutes=51)) == "tok-3"


def test_gcp_auth_accepts_the_key_as_a_json_string(monkeypatch):
    seen = []
    monkeypatch.setattr(gcp_auth, "_fetch", lambda key, aud: seen.append(key) or "tok")
    assert gcp_auth.id_token(json.dumps(SA_KEY), AUDIENCE, now=NOW) == "tok"
    assert gcp_auth.id_token(SA_KEY, AUDIENCE, now=NOW) == "tok"  # misma entrada de cache
    assert seen == [SA_KEY]


def _b64(obj) -> str:
    raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _claims(jwt: str) -> dict:
    part = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


@pytest.mark.filterwarnings("ignore:datetime.datetime.utcfromtimestamp:DeprecationWarning")  # dentro de google-auth
def test_gcp_auth_signs_with_the_service_account_and_bounds_the_token_call(monkeypatch):
    """IDTokenCredentials real (firma con una llave RSA generada) contra un endpoint de tokens falso."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from google.auth.transport import requests as google_requests

    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    key = {**SA_KEY, "private_key": pem, "private_key_id": "k1", "token_uri": "https://oauth2.googleapis.com/token"}
    google_jwt = ".".join([_b64({"alg": "RS256", "typ": "JWT"}),
                           _b64({"aud": AUDIENCE, "exp": int(time.time()) + 3600}), _b64(b"firma")])
    calls = []

    def endpoint(url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        calls.append({"url": url, "method": method, "body": body, "timeout": timeout})
        return SimpleNamespace(status=200, headers={}, data=json.dumps({"id_token": google_jwt}).encode())

    monkeypatch.setattr(google_requests, "Request", lambda *a, **kw: endpoint)
    assert gcp_auth.id_token(key, AUDIENCE) == google_jwt
    (call,) = calls
    assert call["url"] == "https://oauth2.googleapis.com/token" and call["method"] == "POST"
    assert call["timeout"] == gcp_auth.TOKEN_TIMEOUT_S
    assertion = parse_qs(call["body"].decode())["assertion"][0]
    claims = _claims(assertion)
    assert claims["target_audience"] == AUDIENCE and claims["iss"] == SA_KEY["client_email"]
