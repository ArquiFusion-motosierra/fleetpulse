"""Lambda `approvals`: `GET|POST /approvals/{token+}?tech=<tech_id>&inc=<incident_id>` (HTTP API payload v2), el
enlace del correo de WaitForApproval. Responde HTML para el navegador del supervisor.

Llama `SendTaskSuccess(taskToken, output={"approval": "approved", "tech_id": <tech o null>})` y la ejecucion sigue
en AssignTechnician. El token llega url-encoded en el enlace; `unquote` lo restituye y no cambia un token que API
Gateway ya decodifico (base64 no lleva `%`). La ruta es greedy (`{token+}`) porque el token trae `/` (`%2F`).
`tech` debe ser un `TEC-NN` (si no, 400 sin tocar la ejecucion); `inc` solo se muestra y se registra. No hay API
key: el task token es la credencial y nunca se registra.
Idempotencia: un token vencido (30 min), ya usado o invalido (TaskTimedOut, TaskDoesNotExist, InvalidToken)
responde 200 "Esta aprobación ya fue procesada".
"""
from __future__ import annotations

import html
import json
import re
from urllib.parse import unquote

from botocore.config import Config

from fplib import log

TECH_RE = re.compile(r"^TEC-[0-9]{2}$")
INC_RE = re.compile(r"^INC-((?:EX|CF|VQ|MN|TO)-[0-9]{3})-[0-9]{8}T[0-9]{4}$")
DONE = "Esta aprobación ya fue procesada"
AWS_CONFIG = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 3})
_sfn = None


def _client():
    global _sfn
    if _sfn is None:
        import boto3
        _sfn = boto3.client("stepfunctions", config=AWS_CONFIG)
    return _sfn


def _page(code: int, text: str) -> dict:
    body = (f'<!doctype html><html lang="es"><head><meta charset="utf-8"><title>FleetPulse</title></head>'
            f"<body><p>{html.escape(text)}</p></body></html>")
    return {"statusCode": code, "headers": {"content-type": "text/html; charset=utf-8"}, "body": body}


def handler(event, context=None, sfn=None):
    token = unquote((event.get("pathParameters") or {}).get("token") or "")
    query = event.get("queryStringParameters") or {}
    tech, inc = query.get("tech") or None, query.get("inc") or ""
    match = INC_RE.match(inc)
    ctx = {"incident_id": inc if match else None, "machine_id": match.group(1) if match else None}
    if not token:
        return _page(400, "Enlace de aprobacion incompleto.")
    if tech is not None and not TECH_RE.match(tech):
        log.event("aprobacion_tecnico_invalido", **ctx)
        return _page(400, "Tecnico invalido en el enlace de aprobacion.")
    sfn = sfn or _client()
    output = json.dumps({"approval": "approved", "tech_id": tech})
    try:
        sfn.send_task_success(taskToken=token, output=output)
    except (sfn.exceptions.TaskTimedOut, sfn.exceptions.TaskDoesNotExist, sfn.exceptions.InvalidToken) as exc:
        log.event("aprobacion_ya_procesada", error=type(exc).__name__, **ctx)
        return _page(200, DONE)
    method = (event.get("requestContext") or {}).get("http", {}).get("method")
    log.event("aprobacion_recibida", tech_id=tech, method=method, **ctx)
    return _page(200, f"Aprobado: {tech or 'el mejor tecnico disponible'} asignado a "
                      f"{inc if match else 'el incidente'}.")
