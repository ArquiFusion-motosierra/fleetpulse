"""Lambda `publisher`: consume el stream de fp-outbox y publica cada evento en SNS `fp-domain-events`.

El mensaje SNS es el campo `payload` (envelope JSON) tal cual; los atributos permiten filtrar por
`event_type`, `aggregate_type`, `incident_id` y `tenant_id` (parque; un sobre anterior al multi-tenancy sale con el
parque de su maquina segun el catalogo, o el por defecto). Idempotencia: SNS publica al menos una vez, un reintento
de lote repite eventos y los consumidores deduplican por `event_id`. Un registro malo o un fallo de SNS no
tumba el lote: se devuelve en `batchItemFailures` y solo ese registro se reintenta.
"""
from __future__ import annotations

import json
import os

from boto3.dynamodb.types import TypeDeserializer

from fplib import ids, log

_DESER = TypeDeserializer()
_sns = None


def _client():
    global _sns
    if _sns is None:
        import boto3
        _sns = boto3.client("sns")
    return _sns


def _attr(value: str) -> dict:
    return {"DataType": "String", "StringValue": value}


def _publish(sns, topic_arn: str, item: dict) -> None:
    payload = item["payload"]
    envelope = json.loads(payload)
    attrs = {
        "event_type": _attr(str(item["type"])),
        "aggregate_type": _attr(str(envelope["aggregate_type"])),
        "tenant_id": _attr(str(envelope.get("tenant_id") or ids.tenant_of_machine(envelope.get("machine_id")))),
    }
    if item.get("incident_id"):
        attrs["incident_id"] = _attr(str(item["incident_id"]))
    sns.publish(TopicArn=topic_arn, Message=payload, MessageAttributes=attrs)


def handler(event, context=None, sns=None):
    topic_arn = os.environ["DOMAIN_TOPIC_ARN"]
    sns = sns or _client()
    failures = []
    for rec in event.get("Records", []):
        if rec.get("eventName") != "INSERT":
            continue
        fields = {}
        try:
            image = (rec.get("dynamodb") or {}).get("NewImage") or {}
            item = {k: _DESER.deserialize(v) for k, v in image.items()}
            fields = {"event_id": item.get("event_id"), "incident_id": item.get("incident_id")}
            _publish(sns, topic_arn, item)
            log.event("evento publicado", **fields)
        except Exception as exc:  # un registro malo no debe fallar el lote
            log.error("fallo al publicar", error=repr(exc), **fields)
            failures.append({"itemIdentifier": rec["eventID"]})
    return {"batchItemFailures": failures}
