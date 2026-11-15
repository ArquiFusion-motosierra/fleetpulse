"""Ayudas HTTP (API Gateway HTTP API, payload v2) comunes a las Lambdas `reports` y `admin`: respuestas JSON, cuerpo,
errores y el despacho por `routeKey` con 401 (sin claims del autorizador) y 403 (`fplib.authz.Forbidden`).
Funciones puras salvo el log de acceso denegado; nunca registran el token ni el cuerpo.
"""
from __future__ import annotations

import base64
import json

from fplib import authz, log
from lib import store


class HttpError(Exception):
    """Respuesta de error ya decidida: `status` y cuerpo JSON."""

    def __init__(self, status: int, body: dict):
        super().__init__(body.get("error", ""))
        self.status, self.body = status, body


def resp(code: int, body: dict) -> dict:
    return {"statusCode": code, "headers": {"content-type": "application/json", "cache-control": "no-store"},
            "body": json.dumps(store.from_ddb(body), default=str)}


def body(event: dict) -> dict:
    """Cuerpo JSON como objeto; `HttpError(400)` si no lo es."""
    raw = event.get("body") or ""
    try:
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        out = json.loads(raw)
    except ValueError:
        raise HttpError(400, {"error": "invalid JSON body"}) from None
    if not isinstance(out, dict):
        raise HttpError(400, {"error": "body must be a JSON object"})
    return out


def dispatch(event: dict, routes: dict, deps) -> dict:
    """Ejecuta la ruta de `routeKey`: 404 si no existe, 401 sin claims, 403 si la ruta lanza Forbidden."""
    route = event.get("routeKey", "")
    fn = routes.get(route)
    if fn is None:
        return resp(404, {"error": "route not found"})
    if not authz.claims(event):
        return resp(401, {"error": "unauthorized"})
    try:
        return fn(event, deps)
    except authz.Forbidden as exc:
        log.event("acceso_denegado", route=route, role=authz.role_of(event), detail=str(exc),
                  **(event.get("pathParameters") or {}))
        return resp(403, {"error": "forbidden", "detail": str(exc)})
    except HttpError as exc:
        return resp(exc.status, exc.body)
