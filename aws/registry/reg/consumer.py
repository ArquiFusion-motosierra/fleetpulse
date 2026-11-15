"""Lambda registry: consume registry-q (todos los eventos de dominio de SNS fp-domain-events) y los aplica
en fp-registry. Fallo parcial por mensaje (ReportBatchItemFailures); tras 5 intentos va a registry-dlq.
Con el resultado de una reserva escribe part.reserved / part.shortage en fp-outbox (llegan a BigQuery).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone

from botocore.exceptions import BotoCoreError, ClientError

from reg import store
from reg.apply import apply, parse_body

log = logging.getLogger()
log.setLevel(logging.INFO)

_ddb = None


def ddb():
    global _ddb
    if _ddb is None:
        _ddb = store.client()
    return _ddb


def result_events(event: dict, reservation: dict | None, now: datetime) -> list[dict]:
    """part.reserved / part.shortage (contracts/part.schema.json) a partir del resultado de la reserva."""
    if not reservation:
        return []
    base = {"schema_version": 1, "source": "aws.registry", "aggregate_type": "part_reservation",
            "aggregate_id": event["aggregate_id"], "occurred_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "incident_id": event["incident_id"], "machine_id": event["machine_id"]}
    out = []
    if reservation.get("reserved"):
        out.append({"event_id": str(uuid.uuid4()), "event_type": "part.reserved", **base,
                    "payload": {"reservations": reservation["reserved"]}})
    if reservation.get("missing"):
        out.append({"event_id": str(uuid.uuid4()), "event_type": "part.shortage", **base,
                    "payload": {"missing": reservation["missing"],
                                "reorder_suggested": bool(reservation.get("reorder_suggested"))}})
    return out


def write_outbox(client, events: list[dict]) -> None:
    # ASUNCION: item de fp-outbox = {event_id, type, aggregate_id, payload (sobre completo en JSON), created_at, ttl}
    # segun el documento de diseno; ajustar si aws/shared/lib/outbox usa otro formato.
    now = int(time.time())
    for e in events:
        client.put_item(TableName=store.OUTBOX, ConditionExpression="attribute_not_exists(event_id)",
                        Item=store.ser({"event_id": e["event_id"], "type": e["event_type"],
                                        "aggregate_id": e["aggregate_id"], "incident_id": e.get("incident_id"),
                                        "payload": store.dumps(e), "created_at": e["occurred_at"],
                                        "ttl": now + store.TTL_7D}))


def process(client, record: dict) -> dict:
    event = parse_body(record["body"])
    started = time.monotonic()
    res = apply(client, event)
    follow = result_events(event, res.get("reservation"), datetime.now(timezone.utc)) \
        if res["status"] == "applied" else []
    if follow:
        write_outbox(client, follow)
    log.info(json.dumps({"msg": "registry_applied", "event_id": event.get("event_id"),
                         "event_type": event.get("event_type"), "incident_id": event.get("incident_id"),
                         "status": res["status"], "follow_up": [e["event_type"] for e in follow],
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
