"""Autorizacion por rol a partir de las claims JWT que API Gateway (authorizer Cognito) deja en el evento."""
from __future__ import annotations


class Forbidden(Exception):
    """El usuario no tiene un rol permitido. El mensaje nombra los roles exigidos, nunca el token."""


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
