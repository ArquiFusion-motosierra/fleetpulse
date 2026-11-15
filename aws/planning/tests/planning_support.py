"""Constantes y ayudas de las pruebas de daily-planning (corrida a las 05:00 de Lima del 2026-11-15).

Nombre unico a proposito: CI corre las pruebas de todos los servicios en una sola sesion de pytest.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal

from boto3.dynamodb.conditions import Key

NOW = datetime(2026, 11, 15, 10, 0, 5, tzinfo=timezone.utc)
NOW_ISO = "2026-11-15T10:00:05Z"
DAY = "2026-11-15"
# EX-001 en ids.yaml: hours_total 1508, hours_since_service 421 -> ultimo servicio a las 1087 h (intervalo 500 h)
EX001_LAST_SERVICE_H = 1087
EX001_KIT = [{"part_id": "PRT-01", "qty": 1}, {"part_id": "PRT-02", "qty": 1}, {"part_id": "PRT-11", "qty": 1}]


def make_table(res, name, gsis=(), hash_key=None):
    if hash_key:
        return res.create_table(
            TableName=name, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": hash_key, "AttributeType": "S"}],
            KeySchema=[{"AttributeName": hash_key, "KeyType": "HASH"}])
    attrs = [("PK", "S"), ("SK", "S")] + [(f"{g}{k}", "S") for g in gsis for k in ("PK", "SK")]
    return res.create_table(
        TableName=name, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": t} for a, t in attrs],
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        GlobalSecondaryIndexes=[{"IndexName": g, "Projection": {"ProjectionType": "ALL"},
                                 "KeySchema": [{"AttributeName": f"{g}PK", "KeyType": "HASH"},
                                               {"AttributeName": f"{g}SK", "KeyType": "RANGE"}]} for g in gsis])


def seed_machine(state, machine_id, model, zone, hours_total, status="ok", **attrs):
    """META como lo dejan seed-registry.py e ingest (GSI1 ZONE#<zona> / MACHINE#<id>)."""
    state.put_item(Item={
        "PK": f"MACHINE#{machine_id}", "SK": "META", "machine_id": machine_id, "model": model, "zone_id": zone,
        "hours_total": Decimal(str(hours_total)), "status": status,
        "GSI1PK": f"ZONE#{zone}", "GSI1SK": f"MACHINE#{machine_id}",
        **{k: Decimal(str(v)) if isinstance(v, float) else v for k, v in attrs.items()}})


def seed_reading(state, machine_id, ts, operating_hours):
    state.put_item(Item={"PK": f"MACHINE#{machine_id}", "SK": f"R#{ts}", "machine_id": machine_id, "ts": ts,
                         "cumulative_operating_hours": Decimal(str(operating_hours))})


def seed_stock(registry, part_id, zone, available, reorder_point):
    registry.put_item(Item={"PK": f"STOCK#{part_id}#{zone}", "SK": "META", "part_id": part_id, "zone_id": zone,
                            "on_hand": available, "reserved": 0, "available": available,
                            "reorder_point": reorder_point})


def seed_kit_stock(registry, zone, available=10, reorder_point=2, overrides=None):
    """Stock del kit de EX-001; `overrides` = {part_id: (available, reorder_point)}."""
    for line in EX001_KIT:
        av, rp = (overrides or {}).get(line["part_id"], (available, reorder_point))
        seed_stock(registry, line["part_id"], zone, av, rp)


def order_events(outbox):
    return [json.loads(i["payload"]) for i in outbox.scan()["Items"] if i["type"] == "maintenance.order.suggested"]


def reorders(registry, part_id=None, zone=None):
    rows = registry.query(IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq("REORDERS"))["Items"]
    return [r for r in rows if (part_id is None or r["part_id"] == part_id) and (zone is None or r["zone_id"] == zone)]
