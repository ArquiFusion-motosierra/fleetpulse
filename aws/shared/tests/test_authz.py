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


# ---------- multi-tenancy ----------

def test_usuario_sin_grupo_es_del_parque_por_defecto():
    ev = _ev(**{"custom:role": "admin"})
    assert authz.tenant_of(ev) == "mineria"
    assert authz.scope(ev) == ("mineria",)
    assert not authz.is_global(ev)


@pytest.mark.parametrize("groups", ["[tenant-agro]", ["tenant-agro"], "tenant-agro", "[otro tenant-agro]"])
def test_grupo_de_tenant_en_todos_los_formatos(groups):
    ev = _ev(**{"custom:role": "admin", "cognito:groups": groups})
    assert authz.tenant_of(ev) == "agro"
    assert authz.scope(ev) == ("agro",)
    assert authz.scope(ev, "agro") == ("agro",)


def test_admin_de_parque_no_ve_otro_parque():
    ev = _ev(**{"custom:role": "admin", "cognito:groups": "[tenant-agro]"})
    with pytest.raises(authz.TenantForbidden):
        authz.scope(ev, "mineria")
    with pytest.raises(authz.TenantForbidden):
        authz.require_tenant_access(ev, "industria")
    with pytest.raises(authz.Forbidden):  # subclase: los handlers que mapean Forbidden a 403 no cambian
        authz.require_tenant_access(ev, None)  # sin tenant = mineria
    authz.require_tenant_access(ev, "agro")


def test_grupo_de_tenant_desconocido_cae_al_por_defecto():
    ev = _ev(**{"custom:role": "operator", "cognito:groups": "[tenant-luna]"})
    assert authz.tenant_of(ev) == "mineria"


@pytest.mark.parametrize("claims", [
    {"custom:role": "maintenance_head"},
    {"custom:role": "supervisor", "cognito:groups": "[maintenance-head]"},
])
def test_jefe_de_mantenimiento_ve_todo(claims):
    ev = _ev(**claims)
    assert authz.is_global(ev)
    assert authz.tenant_of(ev) is None
    assert set(authz.scope(ev)) == {"mineria", "agro", "industria"}
    assert authz.scope(ev, "industria") == ("industria",)
    assert authz.scope(ev, "agro,industria") == ("agro", "industria")
    authz.require_tenant_access(ev, "agro")


def test_parque_desconocido_se_rechaza():
    with pytest.raises(authz.TenantForbidden):
        authz.scope(_ev(**{"custom:role": "maintenance_head"}), "luna")
