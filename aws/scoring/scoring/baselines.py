"""Lineas base por maquina (propia) y por modelo (pares).

`from_readings` es pura: mu y sigma por variable con las lecturas `operating` (minimo MIN_READINGS).
La linea base propia la siembra `scripts/seed-baselines.py` con 24 h de historia simulada. `for_machine` hace
I/O sobre un `Table` inyectado y solo como respaldo, si META no tiene `baseline`, la calcula con las ultimas
LOOKBACK lecturas y un piso de sigma relativo (BOOTSTRAP_REL_FLOOR); la persiste sin pisar una ya escrita.
Idempotente: misma entrada, mismo resultado.
"""
from __future__ import annotations

import statistics
from decimal import Decimal

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from scoring.score import SCORED_STATUS, VARS

MIN_READINGS = 200
LOOKBACK = 2000
BOOTSTRAP_REL_FLOOR = 0.05  # unos minutos de datos no cubren los bloques de carga: sigma >= 5 % de |mu|


def operating(readings: list[dict]) -> list[dict]:
    return [r for r in readings if r.get("engine_status") == SCORED_STATUS]


def from_readings(readings: list[dict], rel_floor: float = 0.0) -> dict[str, dict] | None:
    on = operating(readings)
    if len(on) < MIN_READINGS:
        return None
    out = {}
    for v in VARS:
        vals = [float(r[v]) for r in on if r.get(v) is not None]
        if len(vals) >= 2:
            mu = statistics.fmean(vals)
            out[v] = {"mu": mu, "sigma": max(statistics.stdev(vals), rel_floor * abs(mu))}
    return out or None


def _floats(baseline: dict | None) -> dict | None:
    if not baseline:
        return None
    return {v: {"mu": float(b["mu"]), "sigma": float(b["sigma"])} for v, b in baseline.items()}


def _decimals(baseline: dict) -> dict:
    return {v: {k: Decimal(str(round(x, 6))) for k, x in b.items()} for v, b in baseline.items()}


def persist(table, machine_id: str, baseline: dict, n: int, only_if_absent: bool = False) -> None:
    """Escribe `baseline` y `baseline_n` en META, que debe existir. `only_if_absent`: no pisa una ya escrita.
    Lanza ClientError(ConditionalCheckFailedException) si la condicion no se cumple."""
    cond = "attribute_exists(PK)" + (" AND attribute_not_exists(baseline)" if only_if_absent else "")
    table.update_item(Key={"PK": f"MACHINE#{machine_id}", "SK": "META"},
                      UpdateExpression="SET baseline = :b, baseline_n = :n", ConditionExpression=cond,
                      ExpressionAttributeValues={":b": _decimals(baseline), ":n": n})


def recent_readings(table, pk: str) -> list[dict]:
    """Ultimas LOOKBACK lecturas `R#` (la mas nueva primero), paginando: una pagina de DynamoDB trae <= 1 MB."""
    kw = {"KeyConditionExpression": Key("PK").eq(pk) & Key("SK").begins_with("R#"), "ScanIndexForward": False}
    rows: list[dict] = []
    while len(rows) < LOOKBACK:
        page = table.query(Limit=LOOKBACK - len(rows), **kw)
        rows.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return rows


def _conditional_failed(e: ClientError) -> bool:
    return e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def for_machine(table, machine_id: str, model: str) -> tuple[dict | None, dict | None]:
    pk = f"MACHINE#{machine_id}"
    meta = table.get_item(Key={"PK": pk, "SK": "META"}).get("Item") or {}
    own = _floats(meta.get("baseline"))
    if own is None:
        rows = recent_readings(table, pk)
        own = from_readings(rows, rel_floor=BOOTSTRAP_REL_FLOOR)
        if own is not None and meta:
            try:
                persist(table, machine_id, own, len(operating(rows)), only_if_absent=True)
            except ClientError as e:
                if not _conditional_failed(e):
                    raise
                # otro escritor (seed-baselines.py o un lote en paralelo) ya la guardo: se usa la persistida
                fresh = table.get_item(Key={"PK": pk, "SK": "META"}).get("Item") or {}
                own = _floats(fresh.get("baseline")) or own
    peer = table.get_item(Key={"PK": f"MODEL#{model}", "SK": "BASELINE"}).get("Item") or {}
    return own, _floats(peer.get("baseline"))
