"""Lambda `score`: consume INSERTs `R#` del stream de fp-state, puntua cada (maquina, lote) e inicia `incident-flow`.

Un grupo que falla no impide puntuar los demas; al final se relanza el primer error para que el event source
mapping reintente/bisecte.
Idempotencia: reprocesar un lote reescribe el mismo item `S#<ts>` (y `A#<ts>#<maquina>`), y el nombre de la
ejecucion es el `incident_id` (maquina + ventana de 15 min), asi que un segundo inicio en la misma ventana
lanza `ExecutionAlreadyExists`, que se ignora. Solo cuentan las lecturas `operating`: un lote sin ellas no se
puntua. Sin linea base propia tampoco (arranque en frio; la siembra `scripts/seed-baselines.py`).
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer

from fplib import clock, geohash, ids, log
from scoring import baselines, failure_mode, score

_DESER = TypeDeserializer()
SCORE_TTL = timedelta(days=3)
CELL_TTL = timedelta(hours=2)
_table = None
_sfn = None


def _get_table():
    global _table
    if _table is None:
        import boto3
        _table = boto3.resource("dynamodb").Table(os.environ["STATE_TABLE"])
    return _table


def _get_sfn():
    global _sfn
    if _sfn is None:
        import boto3
        _sfn = boto3.client("stepfunctions")
    return _sfn


def _groups(event: dict) -> dict[tuple[str, str], list[dict]]:
    out: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for rec in event.get("Records", []):
        if rec.get("eventName") != "INSERT":
            continue
        image = (rec.get("dynamodb") or {}).get("NewImage")
        if not image:
            continue
        item = {k: _DESER.deserialize(v) for k, v in image.items()}
        if str(item.get("SK", "")).startswith("R#") and item.get("machine_id"):
            out[(item["machine_id"], str(item.get("batch_id", "")))].append(item)
    return out


def _dec(o):
    """Convierte floats anidados a Decimal para escribir en DynamoDB (None se conserva)."""
    if isinstance(o, float):
        return Decimal(str(round(o, 6)))
    if isinstance(o, dict):
        return {k: _dec(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_dec(v) for v in o]
    return o


def _previous_means(table, machine_id: str, last_ts: str) -> list[dict]:
    """`means` de los 2 lotes puntuados antes de `last_ts`, en orden cronologico."""
    rows = table.query(
        KeyConditionExpression=Key("PK").eq(f"MACHINE#{machine_id}") & Key("SK").between("S#", f"S#{last_ts}"),
        ScanIndexForward=False, Limit=3)["Items"]
    rows = [r for r in rows if r["SK"] != f"S#{last_ts}"][:2]
    return [{k: float(v) for k, v in r.get("means", {}).items()} for r in reversed(rows)]


def _ttl(ts: str, delta: timedelta) -> int:
    return int((clock.parse(ts) + delta).timestamp())


def _num(x):
    return float(x) if isinstance(x, Decimal) else x


def _score_group(table, sfn, machine_id: str, batch_id: str, readings: list[dict]) -> tuple[bool, str | None]:
    """Puntua un lote. Devuelve (puntuado, incident_id si inicio una ejecucion nueva)."""
    ctx = {"machine_id": machine_id, "batch_id": batch_id}
    meta = table.get_item(Key={"PK": f"MACHINE#{machine_id}", "SK": "META"}).get("Item")
    if not meta:
        log.event("sin_meta", **ctx)
        return False, None
    means = score.batch_means(readings)
    if not means:  # solo ralenti/apagado: se puntua el siguiente lote operando
        log.event("sin_lecturas_operando", **ctx)
        return False, None
    own, peer = baselines.for_machine(table, machine_id, meta["model"])
    if own is None:
        log.event("sin_linea_base", **ctx)
        return False, None
    last_ts = max(r["ts"] for r in readings)
    res = score.compute(means, own, peer, _previous_means(table, machine_id, last_ts))
    table.put_item(Item=_dec({
        "PK": f"MACHINE#{machine_id}", "SK": f"S#{last_ts}", "machine_id": machine_id, "ts": last_ts,
        "score": res["score"], "severity": res["severity"], "z": res["z"], "means": means,
        "batch_id": batch_id, "ttl": _ttl(last_ts, SCORE_TTL)}))
    table.update_item(Key={"PK": f"MACHINE#{machine_id}", "SK": "META"},
                      UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
                      ExpressionAttributeValues={":s": res["severity"]},
                      ConditionExpression="attribute_exists(PK)")
    lat, lon = _num(meta["lat"]), _num(meta["lon"])
    gh = meta.get("geohash6") or geohash.encode(lat, lon, 6)
    incident_id = ids.incident_id(machine_id, clock.parse(last_ts))
    ctx["incident_id"] = incident_id
    if res["severity"] != "ok":
        table.put_item(Item=_dec({
            "PK": f"CELL#{gh}", "SK": f"A#{last_ts}#{machine_id}", "machine_id": machine_id,
            "score": res["score"], "severity": res["severity"],
            "deviated_vars": [d["var"] for d in res["deviated_vars"]], "ttl": _ttl(last_ts, CELL_TTL)}))
    log.event("puntuado", score=res["score"], severity=res["severity"], **ctx)
    if res["score"] < score.THRESHOLDS["schedule"]:
        return True, None
    payload = {
        "incident_id": incident_id, "machine_id": machine_id, "model": meta["model"], "serial": meta.get("serial", ""),
        "zone_id": meta.get("zone_id", ""), "lat": lat, "lon": lon, "geohash6": gh, "ts": last_ts, "batch_id": batch_id,
        "score": res["score"], "severity": res["severity"], "z": res["z"], "deviated_vars": res["deviated_vars"],
        "failure_mode": failure_mode.infer(res["deviated_vars"]), "hours_total": _num(meta.get("hours_total", 0)),
        "tenant_id": meta.get("tenant_id") or ids.tenant_of_machine(machine_id),
    }
    try:
        sfn.start_execution(stateMachineArn=os.environ["STATE_MACHINE_ARN"], name=incident_id,
                            input=json.dumps(payload, default=_num))
    except sfn.exceptions.ExecutionAlreadyExists:
        log.event("ejecucion_existente", **ctx)
        return True, None
    log.event("ejecucion_iniciada", failure_mode=payload["failure_mode"], **ctx)
    return True, incident_id


def handler(event, context=None, table=None, sfn=None):
    table = table or _get_table()
    sfn = sfn or _get_sfn()
    scored, started, failed = 0, [], []
    for (machine_id, batch_id), rows in sorted(_groups(event).items(), key=lambda kv: max(r["ts"] for r in kv[1])):
        try:
            done, incident_id = _score_group(table, sfn, machine_id, batch_id, rows)
        except Exception as e:  # noqa: BLE001 - aislar el grupo; se relanza al final para que el stream reintente
            log.error("fallo_grupo", machine_id=machine_id, batch_id=batch_id, error=repr(e))
            failed.append(e)
            continue
        scored += done
        if incident_id and incident_id not in started:
            started.append(incident_id)
    if failed:
        raise failed[0]
    return {"scored": scored, "started": started}
