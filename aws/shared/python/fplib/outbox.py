"""Escritura en fp-outbox: item `{event_id, type, aggregate_id, incident_id, payload (JSON), created_at, ttl}`."""
from __future__ import annotations

import json
import time

from fplib import clock


def _client():
    import boto3
    return boto3.client("dynamodb")


def _item(envelope: dict, ttl_days: int) -> dict:
    item = {
        "event_id": {"S": envelope["event_id"]},
        "type": {"S": envelope["event_type"]},
        "aggregate_id": {"S": envelope["aggregate_id"]},
        "payload": {"S": json.dumps(envelope, separators=(",", ":"))},
        "created_at": {"S": clock.iso(clock.now())},
        "ttl": {"N": str(int(time.time()) + ttl_days * 86400)},
    }
    if envelope.get("incident_id"):
        item["incident_id"] = {"S": envelope["incident_id"]}
    return item


def put_event(table: str, envelope: dict, ttl_days: int = 7) -> None:
    """Put condicional: falla con ConditionalCheckFailedException si el event_id ya existe."""
    _client().put_item(
        TableName=table,
        Item=_item(envelope, ttl_days),
        ConditionExpression="attribute_not_exists(event_id)",
    )


def transact_items(envelope: dict, table: str = "fp-outbox", ttl_days: int = 7) -> dict:
    """Elemento `Put` para TransactWriteItems, para escribir el evento junto al cambio de estado."""
    return {
        "Put": {
            "TableName": table,
            "Item": _item(envelope, ttl_days),
            "ConditionExpression": "attribute_not_exists(event_id)",
        }
    }
