"""Lambda `whoami`: ruta de prueba `GET /whoami` protegida por el authorizer JWT de Cognito.
Devuelve las claims que el API valido; nunca registra ni devuelve el token. Sin claims responde 401."""
from __future__ import annotations

import json

from fplib import authz


def _resp(code: int, body: dict) -> dict:
    return {"statusCode": code, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def handler(event, context=None):
    c = authz.claims(event)
    if not c:
        return _resp(401, {"error": "unauthorized"})
    return _resp(200, {
        "sub": c.get("sub"),
        "email": c.get("email"),
        "role": c.get("custom:role") or None,
        "tech_id": c.get("custom:tech_id") or None,
    })
