"""Lambda `ingest`: API HTTP (payload v2) que recibe lotes de telemetria (`POST /telemetry`) y contexto
(`POST /context`) del simulador y los guarda en fp-state.

Idempotencia: la escritura es determinista por clave (`R#<ts>`, META condicional por `last_ts`) y el
`batch_id` se reclama en fp-inbox (`batch:<id>`, consumidor `ingest`) DESPUES de escribir, de modo que un
fallo a medias se reintenta completo y un lote repetido responde `duplicate`.
"""
from __future__ import annotations

import base64
import json
import os
from decimal import Decimal

from fplib import auth, clock, inbox as fp_inbox, log
from fplib.schema import ContractError, validate

from lib import state as fp_state

_table = None


def _state_table():
    global _table
    if _table is None:
        import boto3
        _table = boto3.resource("dynamodb").Table(os.environ["STATE_TABLE"])
    return _table


def _resp(code: int, body: dict) -> dict:
    return {"statusCode": code, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def _parse(event: dict):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    return json.loads(raw, parse_float=Decimal)


def _telemetry(batch, state, inbox_table) -> dict:
    validate("telemetry-batch", batch)
    batch_id = batch["batch_id"]
    now = clock.now()
    n = fp_state.put_readings(state, batch, now)
    fp_state.update_meta(state, fp_state.latest_by_machine(batch), now)
    if not fp_inbox.claim(inbox_table, f"batch:{batch_id}", "ingest"):
        log.event("batch duplicado", batch_id=batch_id)
        return _resp(200, {"status": "duplicate", "batch_id": batch_id})
    log.event("batch aceptado", batch_id=batch_id, readings=n)
    return _resp(202, {"status": "accepted", "batch_id": batch_id, "readings": n})


def _context(items, state) -> dict:
    items = items if isinstance(items, list) else [items]
    for it in items:
        # fuel.refill.schema.json cubre refill y weather: ramas if/then por `type` exigen campos propios.
        validate("fuel.refill", it)
    n = fp_state.put_context(state, items)
    log.event("contexto aceptado", context=n)
    return _resp(202, {"status": "accepted", "context": n})


def handler(event, context=None, state=None, inbox=None, secrets=None):
    path = event.get("rawPath", "")
    if path not in ("/telemetry", "/context"):
        return _resp(404, {"error": "not found"})
    if not auth.check_api_key(event.get("headers") or {}, os.environ["API_KEY_SECRET"], client=secrets):
        return _resp(401, {"error": "unauthorized"})
    try:
        payload = _parse(event)
    except ValueError:
        log.event("body no es JSON", path_requested=path)
        return _resp(400, {"error": "body is not valid JSON", "path": []})
    state = state or _state_table()
    try:
        if path == "/telemetry":
            return _telemetry(payload, state, inbox or os.environ["INBOX_TABLE"])
        return _context(payload, state)
    except ContractError as e:
        batch_id = payload.get("batch_id") if isinstance(payload, dict) else None
        log.event("contrato invalido", batch_id=batch_id, error=e.message, path=e.path)
        return _resp(400, {"error": e.message, "path": e.path})
