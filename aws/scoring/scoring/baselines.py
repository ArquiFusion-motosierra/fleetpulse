"""Lineas base por maquina (propia) y por modelo (pares).

`from_readings` es pura. `for_machine` hace I/O sobre un `Table` inyectado: si META no tiene `baseline`, la
calcula con las ultimas 200 lecturas y la persiste; es idempotente (misma entrada, mismo resultado).
"""
from __future__ import annotations

import statistics
from decimal import Decimal

from boto3.dynamodb.conditions import Key

from scoring.score import VARS

MIN_READINGS = 50
LOOKBACK = 200


def from_readings(readings: list[dict]) -> dict[str, dict] | None:
    on = [r for r in readings if r.get("engine_status") != "off"]
    if len(on) < MIN_READINGS:
        return None
    out = {}
    for v in VARS:
        vals = [float(r[v]) for r in on if r.get(v) is not None]
        if len(vals) >= 2:
            out[v] = {"mu": statistics.fmean(vals), "sigma": statistics.stdev(vals)}
    return out or None


def _floats(baseline: dict | None) -> dict | None:
    if not baseline:
        return None
    return {v: {"mu": float(b["mu"]), "sigma": float(b["sigma"])} for v, b in baseline.items()}


def _decimals(baseline: dict) -> dict:
    return {v: {k: Decimal(str(round(x, 6))) for k, x in b.items()} for v, b in baseline.items()}


def for_machine(table, machine_id: str, model: str) -> tuple[dict | None, dict | None]:
    pk = f"MACHINE#{machine_id}"
    meta = table.get_item(Key={"PK": pk, "SK": "META"}).get("Item") or {}
    own = _floats(meta.get("baseline"))
    if own is None:
        rows = table.query(KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with("R#"),
                           ScanIndexForward=False, Limit=LOOKBACK)["Items"]
        own = from_readings(rows)
        if own is not None and meta:
            table.update_item(Key={"PK": pk, "SK": "META"},
                              UpdateExpression="SET baseline = :b, baseline_n = :n",
                              ExpressionAttributeValues={":b": _decimals(own), ":n": sum(1 for r in rows if r.get("engine_status") != "off")})
    peer = table.get_item(Key={"PK": f"MODEL#{model}", "SK": "BASELINE"}).get("Item") or {}
    return own, _floats(peer.get("baseline"))
