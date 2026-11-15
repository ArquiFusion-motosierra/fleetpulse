"""Acceso a fp-registry y fp-outbox con el cliente de bajo nivel de DynamoDB (transacciones con condicion).

Claves (docs/superpowers/plans/2026-11-15-repuestos-abastecimiento.md, 4.2, y contracts/secrets.md):
- `STOCK#<part>#<zone>/META`: on_hand, reserved, available, reorder_point, on_order, in_transit, safety_stock,
  criticality, tenant_id (los que falten se leen como 0 / C).
- `PO#<id>/META`, `TRF#<id>/META`: documento de la orden o transferencia; su linea de tiempo en `EV#<ts>#<event_id>`.
- `BACKORDER#<part>#<zone>/<incident_id>`: incidente que espera el repuesto; lo borra AllocateBackorders.
- `MOVE#<part>#<zone>/<ts>#<ref>`: ledger con qty con signo, kind, ref y tenant_id.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

_ser = TypeSerializer()
_des = TypeDeserializer()


def table(name: str) -> str:
    defaults = {"REGISTRY_TABLE": "fp-registry", "OUTBOX_TABLE": "fp-outbox"}
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


def key(pk: str, sk: str) -> dict:
    return {"PK": {"S": pk}, "SK": {"S": sk}}


def get(ddb, pk: str, sk: str = "META") -> dict | None:
    item = ddb.get_item(TableName=table("REGISTRY_TABLE"), Key=key(pk, sk), ConsistentRead=True).get("Item")
    return des(item) if item else None


def query_all(ddb, pk: str, **kwargs) -> list[dict]:
    items, start = [], None
    params = {"TableName": table("REGISTRY_TABLE"), "KeyConditionExpression": "PK = :pk",
              "ExpressionAttributeValues": vals(pk=pk)}
    if "sk_from" in kwargs:
        params["KeyConditionExpression"] += " AND SK >= :from"
        params["ExpressionAttributeValues"].update(vals(**{"from": kwargs["sk_from"]}))
    while True:
        if start:
            params["ExclusiveStartKey"] = start
        resp = ddb.query(**params)
        items += [des(i) for i in resp.get("Items", [])]
        start = resp.get("LastEvaluatedKey")
        if not start:
            return items


def stock(ddb, part_id: str, zone_id: str) -> dict | None:
    """Stock del almacen con los campos nuevos de T38 en 0 si el item es anterior."""
    s = get(ddb, f"STOCK#{part_id}#{zone_id}")
    if s is None:
        return None
    for k in ("on_hand", "reserved", "available", "reorder_point", "on_order", "in_transit", "safety_stock"):
        s[k] = int(s.get(k) or 0)
    s.setdefault("criticality", "C")
    return s


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)


class ConditionFailed(Exception):
    """Alguna condicion de la transaccion fallo; `reasons` = codigos por elemento (None si paso)."""

    def __init__(self, reasons: list[str | None]):
        super().__init__(f"condicion fallida: {reasons}")
        self.reasons = reasons


def transact(ddb, items: list[dict]) -> None:
    try:
        ddb.transact_write_items(TransactItems=items)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        reasons = [r.get("Code") if r.get("Code") not in (None, "None") else None
                   for r in exc.response.get("CancellationReasons") or []]
        if any(r == "ConditionalCheckFailed" for r in reasons):
            raise ConditionFailed(reasons) from exc
        raise
