"""Lambda `whoami`: ruta `GET /whoami` protegida por el authorizer JWT de Cognito.
Devuelve las claims que el API valido y el alcance por parque (fplib.authz): `tenant_id` (None si es transversal),
`tenants` (parques que puede ver) y `groups`. El BFF de la consola la usa para decidir que parque leer: la firma del
token la valida API Gateway, no la consola. Nunca registra ni devuelve el token. Sin claims responde 401."""
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
        "tenant_id": authz.tenant_of(event),
        "tenants": list(authz.scope(event)),
        "global": authz.is_global(event),
        "groups": authz.groups_of(event),
    })
