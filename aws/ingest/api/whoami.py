"""Lambda `whoami`: ruta de prueba `GET /whoami` protegida por el authorizer JWT de Cognito.
Devuelve las claims que el API valido; nunca registra ni devuelve el token."""
from __future__ import annotations

import json

from fplib import authz


def handler(event, context=None):
    body = {
        "sub": authz.claims(event).get("sub"),
        "email": authz.claims(event).get("email"),
        "role": authz.role_of(event),
        "tech_id": authz.tech_id_of(event),
    }
    return {"statusCode": 200, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}
