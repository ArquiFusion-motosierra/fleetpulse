"""Pruebas del handler whoami: sin red."""
import json

from api import whoami


def test_con_claims():
    ev = {"requestContext": {"authorizer": {"jwt": {"claims": {
        "sub": "u1", "email": "t@example.com", "custom:role": "technician", "custom:tech_id": "TEC-01"}}}}}
    r = whoami.handler(ev)
    assert r["statusCode"] == 200
    assert json.loads(r["body"]) == {"sub": "u1", "email": "t@example.com", "role": "technician", "tech_id": "TEC-01",
                                     "tenant_id": "mineria", "tenants": ["mineria"], "global": False, "groups": []}


def test_parque_y_jefe_de_mantenimiento():
    def ev(**claims):
        return {"requestContext": {"authorizer": {"jwt": {"claims": {"sub": "u", **claims}}}}}
    agro = json.loads(whoami.handler(ev(**{"custom:role": "admin", "cognito:groups": "[tenant-agro]"}))["body"])
    assert agro["tenant_id"] == "agro" and agro["tenants"] == ["agro"] and agro["groups"] == ["tenant-agro"]
    head = json.loads(whoami.handler(ev(**{"custom:role": "maintenance_head",
                                           "cognito:groups": "[maintenance-head]"}))["body"])
    assert head["tenant_id"] is None and head["global"] is True
    assert set(head["tenants"]) == {"mineria", "agro", "industria"}


def test_sin_authorizer():
    r = whoami.handler({})
    assert r["statusCode"] == 401
