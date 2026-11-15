"""Lambda `wait_for_approval` (WaitForApproval, `arn:aws:states:::lambda:invoke.waitForTaskToken`): pide por correo
al supervisor que apruebe al tecnico propuesto (el primero de `$.ranking.top`).

Entrada: `{"input": <estado de la ejecucion>, "token": <task token>}`. Publica en fp-notify (`NOTIFY_TOPIC_ARN`,
atributo `role` = supervisor) el texto de `templates.approval_request` con el enlace
`GET <api>/approvals/<token url-encoded>?tech=<tech_id>&inc=<incident_id>`. La URL base de la API se lee de SSM
(`API_URL_PARAM` = `/fleetpulse/api/url`) la primera vez y queda en memoria. Corre antes de AssignTechnician, por eso
manda su propio correo (RenderMessages arma los textos despues de asignar).
No devuelve nada: la tarea espera a que `api/approvals` llame SendTaskSuccess (`$.approval`) o vence a los 30 min
(ASL: TimeoutSeconds 1800 -> ApprovalTimedOut, approval `timeout`).
El task token es una credencial: nunca se registra.
Idempotencia: un reintento de la tarea manda otro correo; el enlace de un token que ya no espera responde
"ya fue procesada".
"""
from __future__ import annotations

import os
from urllib.parse import quote, urlencode

from botocore.config import Config

from fplib import log
from lib import templates

AWS_CONFIG = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 3})
_url: str | None = None
_clients: dict = {}


def _client(name: str):
    if name not in _clients:
        import boto3
        _clients[name] = boto3.client(name, config=AWS_CONFIG)
    return _clients[name]


def _api_url(ssm) -> str:
    global _url
    if _url is None:
        _url = ssm.get_parameter(Name=os.environ["API_URL_PARAM"])["Parameter"]["Value"].rstrip("/")
    return _url


def handler(event, context=None, sns=None, ssm=None):
    state, token = event["input"], event["token"]
    incident_id, machine_id = state["incident_id"], state["machine_id"]
    inc = state["incident"]
    top = (state.get("ranking") or {}).get("top") or []
    proposed = top[0] if top else None
    query = {"tech": proposed["tech_id"]} if proposed else {}
    url = (f"{_api_url(ssm or _client('ssm'))}/approvals/{quote(token, safe='')}?"
           + urlencode({**query, "inc": incident_id}))
    deviated = inc.get("deviated_vars") or state.get("deviated_vars") or []
    explanation = state.get("explanation") or templates.explanation(deviated, state["failure_mode"], machine_id)
    incident = {"incident_id": incident_id, "machine_id": machine_id, "severity": state["classified"]["severity"],
                "score": float(state["score"]), "failure_mode": state["failure_mode"], "zone_id": inc["zone_id"]}
    mail = templates.approval_request(incident, explanation, proposed, url)
    resp = (sns or _client("sns")).publish(
        TopicArn=os.environ["NOTIFY_TOPIC_ARN"], Subject=mail["subject"], Message=mail["message"],
        MessageAttributes={"role": {"DataType": "String", "StringValue": "supervisor"}})
    log.event("aprobacion_solicitada", incident_id=incident_id, machine_id=machine_id,
              tech_id=proposed["tech_id"] if proposed else None, message_id=resp.get("MessageId"))
