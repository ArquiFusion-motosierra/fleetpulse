"""Autorizacion por rol y por parque (tenant) a partir de las claims JWT que API Gateway (authorizer Cognito) deja en
el evento.

Tenant: sale de los grupos de Cognito `tenant-<id>` en la claim
`cognito:groups`; solo un administrador IAM mete usuarios en grupos. El rol `maintenance_head` (o el grupo
`maintenance-head`) es transversal y ve todos los parques. Un usuario sin grupo de tenant es del parque por defecto
(`mineria`): asi siguen andando los usuarios creados antes del multi-tenancy.
"""
from __future__ import annotations

from fplib import ids

GLOBAL_ROLE = "maintenance_head"
GLOBAL_GROUP = "maintenance-head"
TENANT_GROUP_PREFIX = "tenant-"


class Forbidden(Exception):
    """El usuario no tiene un rol permitido. El mensaje nombra los roles exigidos, nunca el token."""


class TenantForbidden(Forbidden):
    """El usuario pide datos de un parque que no es el suyo."""


def claims(event: dict) -> dict:
    ctx = (event or {}).get("requestContext") or {}
    jwt = (ctx.get("authorizer") or {}).get("jwt") or {}
    return dict(jwt.get("claims") or {})


def role_of(event: dict) -> str | None:
    return claims(event).get("custom:role") or None


def tech_id_of(event: dict) -> str | None:
    return claims(event).get("custom:tech_id") or None


def user_of(event: dict) -> str:
    c = claims(event)
    return c.get("email") or c.get("sub") or ""


def require_role(event: dict, *roles: str) -> str:
    role = role_of(event)
    if role is None or role not in roles:
        raise Forbidden("se requiere uno de los roles: " + ", ".join(roles))
    return role


def groups_of(event: dict) -> list[str]:
    """`cognito:groups` como lista. La HTTP API aplana los arreglos de la claim como `"[a b]"`; la REST API y los
    tests los pasan como lista o como texto separado por comas."""
    raw = claims(event).get("cognito:groups")
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(g) for g in raw if g]
    text = str(raw).strip().strip("[]")
    return [g for g in text.replace(",", " ").split() if g]


def is_global(event: dict) -> bool:
    """Jefe de mantenimiento: ve y gestiona todos los parques."""
    return role_of(event) == GLOBAL_ROLE or GLOBAL_GROUP in groups_of(event)


def tenant_of(event: dict) -> str | None:
    """Parque del usuario; None si es transversal. Sin grupo de tenant: el parque por defecto."""
    if is_global(event):
        return None
    known = ids.tenants()
    for g in groups_of(event):
        if g.startswith(TENANT_GROUP_PREFIX) and g[len(TENANT_GROUP_PREFIX):] in known:
            return g[len(TENANT_GROUP_PREFIX):]
    return ids.default_tenant()


def scope(event: dict, requested: str | None = None) -> tuple[str, ...]:
    """Parques que esta peticion puede ver. `requested` (ej. `?tenant=agro`, o varios separados por coma) acota.

    Usuario de parque: solo el suyo; pedir otro lanza TenantForbidden. Transversal: lo pedido o todos."""
    known = tuple(ids.tenants())
    wanted = tuple(t for t in (requested or "").split(",") if t)
    unknown = [t for t in wanted if t not in known]
    if unknown:
        raise TenantForbidden("parque desconocido: " + ", ".join(unknown))
    own = tenant_of(event)
    if own is None:
        return wanted or known
    if any(t != own for t in wanted):
        raise TenantForbidden(f"solo puedes ver el parque {own}")
    return (own,)


def can_access(event: dict, resource_tenant: str | None) -> bool:
    own = tenant_of(event)
    return own is None or (resource_tenant or ids.default_tenant()) == own


def require_tenant_access(event: dict, resource_tenant: str | None) -> None:
    """TenantForbidden si el recurso es de otro parque (un recurso sin tenant es del parque por defecto)."""
    if not can_access(event, resource_tenant):
        raise TenantForbidden("el recurso pertenece a otro parque")
