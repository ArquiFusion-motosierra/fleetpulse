"""Lambda oci-writer: consume oci-writer-q (eventos de dominio desde SNS fp-domain-events) y los aplica en OCI
por ORDS POST v1/events. OCI descarta event_id repetidos (PROCESSED_EVENTS), asi que reintentar es seguro.

Fallo parcial por mensaje (ReportBatchItemFailures): solo se reintenta lo que fallo. Tras 5 intentos la cola
lo manda a oci-writer-dlq. Si OCI reserva repuestos, escribe part.reserved / part.shortage en fp-outbox.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

from ociw.ords import OrdsClient, OrdsError

log = logging.getLogger()
log.setLevel(logging.INFO)

OUTBOX_TABLE = os.environ.get("OUTBOX_TABLE", "fp-outbox")
OUTBOX_TTL_S = 7 * 24 * 3600

_ords: OrdsClient | None = None
_outbox = None


def ords() -> OrdsClient:
    global _ords
    if _ords is None:
        _ords = OrdsClient()
    return _ords


def outbox():
    global _outbox
    if _outbox is None:
        import boto3
        _outbox = boto3.resource("dynamodb").Table(OUTBOX_TABLE)
    return _outbox


def parse_record(record: dict) -> dict:
    """El cuerpo puede venir envuelto por SNS (sin raw delivery) o crudo."""
    body = json.loads(record["body"])
    if isinstance(body, dict) and body.get("Type") == "Notification" and "Message" in body:
        body = json.loads(body["Message"])
    return body


def result_events(event: dict, reservation: dict | None, now: datetime) -> list[dict]:
    """part.reserved / part.shortage a partir de la respuesta de OCI (contracts/part.schema.json)."""
    if not reservation:
        return []
    base = {"schema_version": 1, "source": "aws.oci-writer", "aggregate_type": "part_reservation",
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


def write_outbox(events: list[dict], table=None) -> None:
    # ASUNCION: item de fp-outbox = {event_id, type, aggregate_id, payload (sobre completo en JSON), created_at, ttl}
    # segun el documento de diseno; ajustar si la plantilla de aws/shared/lib/outbox usa otro formato.
    t = table or outbox()
    now = int(time.time())
    for e in events:
        t.put_item(
            Item={"event_id": e["event_id"], "type": e["event_type"], "aggregate_id": e["aggregate_id"],
                  "incident_id": e.get("incident_id"), "payload": json.dumps(e, separators=(",", ":")),
                  "created_at": e["occurred_at"], "ttl": now + OUTBOX_TTL_S},
            ConditionExpression="attribute_not_exists(event_id)")


def process(record: dict, client: OrdsClient, table=None) -> dict:
    event = parse_record(record)
    ctx = {"event_id": event.get("event_id"), "event_type": event.get("event_type"),
           "incident_id": event.get("incident_id")}
    started = time.monotonic()
    resp = client.post("events", event)
    follow = result_events(event, resp.get("reservation"), datetime.now(timezone.utc))
    if follow:
        write_outbox(follow, table)
    log.info(json.dumps({"msg": "oci_applied", **ctx, "status": resp.get("status"),
                         "follow_up": [e["event_type"] for e in follow],
                         "ms": int((time.monotonic() - started) * 1000)}))
    return resp


def handler(event, context=None, client: OrdsClient | None = None, table=None):
    client = client or ords()
    failures = []
    for record in event.get("Records", []):
        try:
            process(record, client, table)
        except (OrdsError, OSError, ValueError, KeyError) as exc:
            log.error(json.dumps({"msg": "oci_apply_failed", "message_id": record.get("messageId"),
                                  "error": type(exc).__name__, "detail": str(exc)[:300]}))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
