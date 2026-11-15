"""Acceso a DynamoDB fp-registry / fp-inbox / fp-state con el cliente de bajo nivel (transacciones).

Modelo de claves en contracts/secrets.md.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

REGISTRY = os.environ.get("REGISTRY_TABLE", "fp-registry")
INBOX = os.environ.get("INBOX_TABLE", "fp-inbox")
STATE = os.environ.get("STATE_TABLE", "fp-state")
OUTBOX = os.environ.get("OUTBOX_TABLE", "fp-outbox")
TTL_7D = 7 * 24 * 3600

_ser = TypeSerializer()
_des = TypeDeserializer()


def to_ddb(value):
    """float -> Decimal recursivo (DynamoDB no acepta float)."""
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


def key(pk: str, sk: str) -> dict:
    return {"PK": {"S": pk}, "SK": {"S": sk}}


def client():
    import boto3
    return boto3.client("dynamodb")


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


def get(ddb, pk: str, sk: str, table: str = REGISTRY) -> dict | None:
    item = ddb.get_item(TableName=table, Key=key(pk, sk)).get("Item")
    return des(item) if item else None


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)
