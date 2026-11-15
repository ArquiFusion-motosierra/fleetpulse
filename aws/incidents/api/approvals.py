"""Lambda `approvals`: `GET|POST /approvals/{token+}?tech=<tech_id>&inc=<incident_id>` (HTTP API payload v2), el
enlace del correo de WaitForApproval. Responde HTML para el navegador del supervisor.

GET solo muestra una pagina de confirmacion (incidente, maquina, severidad vigente del META en fp-registry, tecnico
propuesto y lo que pasa al aprobar) con un boton "Aprobar" que hace POST a la misma URL: los escaneres de correo
abren los enlaces antes que el supervisor y un GET que aprobara asignaria al tecnico sin que nadie lo decidiera. Si
el META ya tiene tecnico o esta cerrado, el GET responde "ya fue procesada" sin boton; si no se puede leer, muestra
el boton igual (decide Step Functions).
POST llama `SendTaskSuccess(taskToken, output={"approval": "approved", "tech_id": <tech o null>})` y la ejecucion
sigue en AssignTechnician; el cuerpo del formulario se ignora.
El token llega url-encoded en el enlace; `unquote` lo restituye y no cambia un token que API Gateway ya decodifico
(base64 no lleva `%`). La ruta es greedy (`{token+}`) porque el token trae `/` (`%2F`). `tech` debe ser un `TEC-NN`
(si no, 400 sin tocar la ejecucion); `inc` solo se muestra y se registra. No hay API key: el task token es la
credencial, nunca se registra ni se copia a la pagina (el formulario no lleva `action`) y la respuesta va con
`cache-control: no-store` y `referrer-policy: no-referrer`.
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
from lib import registry as fp_registry
from lib.approval_link import token_from_link

TECH_RE = re.compile(r"^TEC-[0-9]{2}$")
INC_RE = re.compile(r"^INC-((?:EX|CF|VQ|MN|TO|TA|CS|MC|CP|GE)-[0-9]{3})-[0-9]{8}T[0-9]{4}$")
DONE = "Esta aprobación ya fue procesada"
SEVERITY = {"immediate": "inmediata", "schedule": "programada"}
HEADERS = {"content-type": "text/html; charset=utf-8", "cache-control": "no-store", "referrer-policy": "no-referrer",
           "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                                      "frame-ancestors 'none'",
           "x-robots-tag": "noindex"}
STYLE = ("body{font-family:system-ui,sans-serif;max-width:32rem;margin:2rem auto;padding:0 1rem;color:#1b1b1b}"
         "dt{font-weight:600}dd{margin:0 0 .5rem}"
         "button{font-size:1.1rem;padding:.6rem 1.6rem;border:0;border-radius:.4rem;background:#0b6e4f;color:#fff}")
AWS_CONFIG = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 3})
_sfn = None


def _client():
    global _sfn
    if _sfn is None:
        import boto3
        _sfn = boto3.client("stepfunctions", config=AWS_CONFIG)
    return _sfn


def _html(code: int, content: str) -> dict:
    body = (f'<!doctype html><html lang="es"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>FleetPulse</title>'
            f"<style>{STYLE}</style></head><body>{content}</body></html>")
    return {"statusCode": code, "headers": dict(HEADERS), "body": body}


def _page(code: int, text: str) -> dict:
    return _html(code, f"<p>{html.escape(text)}</p>")


def _meta(registry, incident_id: str | None, ctx: dict) -> dict | None:
    if not incident_id:
        return None
    try:
        return fp_registry.get_incident(registry or fp_registry.table_from_env("REGISTRY_TABLE"), incident_id)
    except Exception as exc:  # sin el META igual se puede aprobar: el POST lo decide Step Functions
        log.error("aprobacion_meta_ilegible", error=type(exc).__name__, **ctx)
        return None


def _confirm(tech: str | None, inc: str, ctx: dict, meta: dict | None) -> dict:
    meta = meta or {}
    severity = meta.get("severity")
    rows = [("Incidente", inc or "—"), ("Máquina", ctx["machine_id"] or meta.get("machine_id") or "—"),
            ("Severidad", f"{severity} ({SEVERITY.get(severity, severity)})" if severity else "—"),
            ("Técnico propuesto", tech or "el mejor técnico disponible del ranking")]
    who = tech or "el mejor técnico disponible"
    content = (
        "<h1>Aprobar asignación</h1><dl>"
        + "".join(f"<dt>{html.escape(k)}</dt><dd>{html.escape(str(v))}</dd>" for k, v in rows)
        + "</dl><p>" + html.escape(
            f"Al aprobar se asigna {who} al incidente (si está ocupado, el siguiente libre del ranking), se "
            "reservan los repuestos sugeridos y se avisa al técnico, al operador y a dirección. Sin respuesta en "
            "30 minutos se asigna automáticamente.")
        + '</p><form method="post">\n<button type="submit" name="confirm" value="1">Aprobar</button></form>')
    return _html(200, content)


def handler(event, context=None, sfn=None, registry=None):
    token = token_from_link(unquote((event.get("pathParameters") or {}).get("token") or ""))
    query = event.get("queryStringParameters") or {}
    tech, inc = query.get("tech") or None, query.get("inc") or ""
    match = INC_RE.match(inc)
    ctx = {"incident_id": inc if match else None, "machine_id": match.group(1) if match else None}
    if not token:
        return _page(400, "Enlace de aprobacion incompleto.")
    if tech is not None and not TECH_RE.match(tech):
        log.event("aprobacion_tecnico_invalido", **ctx)
        return _page(400, "Tecnico invalido en el enlace de aprobacion.")
    method = (event.get("requestContext") or {}).get("http", {}).get("method")
    if method != "POST":
        meta = _meta(registry, ctx["incident_id"], ctx)
        if meta and (meta.get("tech_id") or meta.get("status") == "closed"):
            log.event("aprobacion_ya_decidida", status=meta.get("status"), tech_id=meta.get("tech_id"), **ctx)
            return _page(200, DONE)
        log.event("aprobacion_confirmacion_mostrada", tech_id=tech, **ctx)
        return _confirm(tech, inc if match else "", ctx, meta)
    sfn = sfn or _client()
    output = json.dumps({"approval": "approved", "tech_id": tech})
    try:
        sfn.send_task_success(taskToken=token, output=output)
    except (sfn.exceptions.TaskTimedOut, sfn.exceptions.TaskDoesNotExist, sfn.exceptions.InvalidToken) as exc:
        log.event("aprobacion_ya_procesada", error=type(exc).__name__, **ctx)
        return _page(200, DONE)
    log.event("aprobacion_recibida", tech_id=tech, method=method, **ctx)
    return _page(200, f"Aprobado: {tech or 'el mejor tecnico disponible'} asignado a "
                      f"{inc if match else 'el incidente'}.")
