"""Lambda registry: consume registry-q (SNS fp-domain-events filtrado a part.reserve.requested, incident.closed y
kpi.daily) y aplica en fp-registry. Fallo parcial por mensaje (ReportBatchItemFailures); tras 5 intentos va a la DLQ.
"""
from __future__ import annotations

import json
import logging
import time

from botocore.exceptions import BotoCoreError, ClientError

from reg import store
from reg.apply import apply

log = logging.getLogger()
log.setLevel(logging.INFO)

_ddb = None


def ddb():
    global _ddb
    if _ddb is None:
        _ddb = store.client()
    return _ddb


def parse_body(raw: str) -> dict:
    """Con raw message delivery el cuerpo es el sobre; sin ella viene envuelto por SNS."""
    body = json.loads(raw)
    if isinstance(body, dict) and body.get("Type") == "Notification" and "Message" in body:
        body = json.loads(body["Message"])
    return body


def process(client, record: dict) -> dict:
    event = parse_body(record["body"])
    started = time.monotonic()
    res = apply(client, event)
    log.info(json.dumps({"msg": "registry_applied", "event_id": event.get("event_id"),
                         "event_type": event.get("event_type"), "incident_id": event.get("incident_id"),
                         "status": res["status"], "follow_up": res["follow_up"],
                         "ms": int((time.monotonic() - started) * 1000)}))
    return res


def handler(event, context=None, client=None):
    client = client or ddb()
    failures = []
    for record in event.get("Records", []):
        try:
            process(client, record)
        except (ClientError, BotoCoreError, ValueError, KeyError) as exc:
            log.error(json.dumps({"msg": "registry_failed", "message_id": record.get("messageId"),
                                  "error": type(exc).__name__, "detail": str(exc)[:300]}))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
