"""Lecturas y escrituras de daily-planning en fp-state, fp-registry, fp-outbox y fp-inbox (cliente de bajo nivel).

Claves del modelo fijado en docs/superpowers/plans/2026-11-14-aws-core-pipeline.md y contracts/secrets.md:
- fp-state `MACHINE#<id>/META` (GSI1 `ZONE#<zona>` / `MACHINE#<id>`) y sus lecturas `R#<ts>`.
- fp-registry `STOCK#<part>#<zone>/META`, `ORDER#<maquina>/<due_at>` (GSI1 `ORDERS`) y
  `REORDER#<part>#<zone>/<created_at>` (GSI1 `REORDERS` / `<created_at>#<part>#<zone>`).
- fp-inbox `planning:<dia>:<clave>`: el reclamo va en la misma transaccion que lo que se escribe.
"""
from __future__ import annotations

import os
import time
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from fplib import clock, ids, outbox

TTL_7D = 7 * 24 * 3600
_ser = TypeSerializer()
_des = TypeDeserializer()


def table(name: str) -> str:
    defaults = {"STATE_TABLE": "fp-state", "REGISTRY_TABLE": "fp-registry", "OUTBOX_TABLE": "fp-outbox",
                "INBOX_TABLE": "fp-inbox"}
    return os.environ.get(name, defaults[name])


def client():
    import boto3
    return boto3.client("dynamodb")


def to_ddb(value):
    """float -> Decimal recursivo (DynamoDB no acepta float); los None se omiten."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_ddb(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [to_ddb(v) for v in value]
    return value


def from_ddb(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: from_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_ddb(v) for v in value]
    return value


def ser(item: dict) -> dict:
    return {k: _ser.serialize(v) for k, v in to_ddb(item).items()}


def des(item: dict) -> dict:
    return from_ddb({k: _des.deserialize(v) for k, v in item.items()})


def vals(**kwargs) -> dict:
    return {f":{k}": _ser.serialize(to_ddb(v)) for k, v in kwargs.items()}


def query_all(ddb, **kwargs) -> list[dict]:
    items, start = [], None
    while True:
        if start:
            kwargs["ExclusiveStartKey"] = start
        resp = ddb.query(**kwargs)
        items += [des(i) for i in resp.get("Items", [])]
        start = resp.get("LastEvaluatedKey")
        if not start:
            return items


# ---------- lecturas ----------

def machines(ddb) -> list[dict]:
    """META de todas las maquinas de fp-state, zona por zona (GSI1)."""
    out = []
    for zone in sorted(ids.zones()):
        out += query_all(ddb, TableName=table("STATE_TABLE"), IndexName="GSI1",
                         KeyConditionExpression="GSI1PK = :z AND begins_with(GSI1SK, :m)",
                         ExpressionAttributeValues=vals(z=f"ZONE#{zone}", m="MACHINE#"))
    return sorted(out, key=lambda m: m["machine_id"])


def oldest_reading(ddb, machine_id: str, since: str) -> dict | None:
    """La lectura `R#` mas antigua desde `since` (las lecturas duran 3 dias por TTL)."""
    resp = ddb.query(TableName=table("STATE_TABLE"), Limit=1, ScanIndexForward=True,
                     KeyConditionExpression="PK = :pk AND SK BETWEEN :a AND :b",
                     ExpressionAttributeValues=vals(pk=f"MACHINE#{machine_id}", a=f"R#{since}", b="R#~"),
                     ProjectionExpression="#t, cumulative_operating_hours", ExpressionAttributeNames={"#t": "ts"})
    items = resp.get("Items", [])
    return des(items[0]) if items else None


def stock(ddb) -> dict[tuple[str, str], dict]:
    """`STOCK#<part>#<zone>/META` de cada repuesto y zona del catalogo, por BatchGetItem."""
    keys = [{"PK": {"S": f"STOCK#{p['part_id']}#{z}"}, "SK": {"S": "META"}}
            for p in ids.load()["parts"] for z in sorted(ids.zones())]
    out: dict[tuple[str, str], dict] = {}
    for i in range(0, len(keys), 100):
        pending = {table("REGISTRY_TABLE"): {"Keys": keys[i:i + 100]}}
        while pending:
            resp = ddb.batch_get_item(RequestItems=pending)
            for item in resp.get("Responses", {}).get(table("REGISTRY_TABLE"), []):
                row = des(item)
                out[(row["part_id"], row["zone_id"])] = row
            pending = resp.get("UnprocessedKeys") or None
    return out


def open_order_machines(ddb) -> set[str]:
    rows = query_all(ddb, TableName=table("REGISTRY_TABLE"), IndexName="GSI1",
                     KeyConditionExpression="GSI1PK = :p", ExpressionAttributeValues=vals(p="ORDERS"))
    return {r["machine_id"] for r in rows if r.get("status") == "suggested"}


def on_order(ddb, since: str) -> dict[tuple[str, str], int]:
    """Cantidad en pedidos sugeridos desde `since`, de cualquier origen (falta de stock o planning)."""
    rows = query_all(ddb, TableName=table("REGISTRY_TABLE"), IndexName="GSI1",
                     KeyConditionExpression="GSI1PK = :p AND GSI1SK >= :s",
                     ExpressionAttributeValues=vals(p="REORDERS", s=since))
    out: dict[tuple[str, str], int] = {}
    for r in rows:
        k = (r["part_id"], r["zone_id"])
        out[k] = out.get(k, 0) + int(r.get("qty", 0))
    return out


# ---------- escrituras ----------

class AlreadyPlanned(Exception):
    """El reclamo de fp-inbox ya existia: esta clave se planifico hoy y no se escribio nada."""


def _claim(message_id: str) -> dict:
    return {"Put": {"TableName": table("INBOX_TABLE"), "ConditionExpression": "attribute_not_exists(message_id)",
                    "Item": ser({"message_id": message_id, "consumer": "planning",
                                 "processed_at": clock.iso(clock.now()), "ttl": int(time.time()) + TTL_7D})}}


def claim_id(day: str, key: str) -> str:
    return f"planning:{day}:{key}"


def _transact(ddb, items: list[dict]) -> None:
    """items[0] es siempre el reclamo: si su condicion falla, otra corrida ya lo escribio."""
    try:
        ddb.transact_write_items(TransactItems=items)
    except ClientError as exc:
        reasons = exc.response.get("CancellationReasons") or []
        if exc.response["Error"]["Code"] == "TransactionCanceledException" and reasons \
                and reasons[0].get("Code") == "ConditionalCheckFailed":
            raise AlreadyPlanned() from exc
        raise


def put_order_event(ddb, day: str, envelope: dict) -> None:
    """`maintenance.order.suggested` en fp-outbox; registry lo guarda en `ORDER#<maquina>/<due_at>`."""
    _transact(ddb, [_claim(claim_id(day, f"ORDER#{envelope['machine_id']}")),
                    outbox.transact_items(envelope, table=table("OUTBOX_TABLE"))])


def put_reorder(ddb, day: str, item: dict) -> None:
    """`REORDER#<part>#<zone>/<created_at>` en fp-registry, con la misma forma que los de registry."""
    _transact(ddb, [_claim(claim_id(day, f"REORDER#{item['part_id']}#{item['zone_id']}")),
                    {"Put": {"TableName": table("REGISTRY_TABLE"), "ConditionExpression": "attribute_not_exists(PK)",
                             "Item": ser(item)}}])
