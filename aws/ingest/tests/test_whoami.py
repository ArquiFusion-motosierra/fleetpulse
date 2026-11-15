"""Pruebas del handler whoami: sin red."""
import json

from api import whoami


def test_con_claims():
    ev = {"requestContext": {"authorizer": {"jwt": {"claims": {
        "sub": "u1", "email": "t@example.com", "custom:role": "technician", "custom:tech_id": "TEC-01"}}}}}
    r = whoami.handler(ev)
    assert r["statusCode"] == 200
    assert json.loads(r["body"]) == {"sub": "u1", "email": "t@example.com", "role": "technician", "tech_id": "TEC-01"}


def test_sin_authorizer():
    r = whoami.handler({})
    assert r["statusCode"] == 401
