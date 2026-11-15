"""Pruebas de fplib.authz: puras, sin AWS."""
import pytest

from fplib import authz


def _ev(**claims):
    return {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}


def test_sin_authorizer():
    for ev in ({}, {"requestContext": {}}, {"requestContext": {"authorizer": {}}}):
        assert authz.claims(ev) == {}
        assert authz.role_of(ev) is None
        assert authz.tech_id_of(ev) is None
        assert authz.user_of(ev) == ""
        with pytest.raises(authz.Forbidden):
            authz.require_role(ev, "admin")


def test_rol_permitido():
    ev = _ev(**{"custom:role": "supervisor", "email": "s@example.com", "sub": "abc"})
    assert authz.require_role(ev, "admin", "supervisor") == "supervisor"
    assert authz.user_of(ev) == "s@example.com"


def test_rol_incorrecto():
    ev = _ev(**{"custom:role": "operator", "sub": "abc"})
    with pytest.raises(authz.Forbidden) as e:
        authz.require_role(ev, "admin", "supervisor")
    assert "admin" in str(e.value) and "supervisor" in str(e.value)
    assert authz.user_of(ev) == "abc"


def test_tech_id():
    ev = _ev(**{"custom:role": "technician", "custom:tech_id": "TEC-01"})
    assert authz.tech_id_of(ev) == "TEC-01"
