"""Lambda `gcp-events`: eventos de dominio de SQS gcp-events-q (SNS fp-domain-events, todos los event_type) a
BigQuery fleetpulse.domain_events. Un lote de SQS = un NDJSON gzip en GCS fp-landing + un load job WRITE_APPEND
(nunca streaming: regla de costo del Free Trial de GCP).

Idempotencia: fp-inbox con `message_id = gcp-events:<event_id>`. Antes de cargar se descartan los event_id ya
cargados (BatchGetItem) y los repetidos dentro del lote; despues de un load job exitoso se marcan (BatchWriteItem).
Si la Lambda muere entre el load job y la marca, un reintento de SQS puede cargar el mismo evento dos veces: es
la unica ventana de duplicados y se tolera (las consultas que necesiten exactitud deduplican por event_id). El
nombre del objeto GCS es determinista (hash de los event_id), asi que un reintento del mismo lote lo sobrescribe.

Un sobre invalido se reporta como fallo de su mensaje (ReportBatchItemFailures) y termina en gcp-events-dlq; un
error de GCP o de fp-inbox falla todo el lote. Lo reutiliza scripts/backfill_domain_events.py (`load_events`).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import time

from fplib import clock, log, schema

CONSUMER = "gcp-events"
TABLE = "domain_events"
INBOX_TTL_DAYS = 30  # > retencion de gcp-events-dlq (14 dias): un redrive tardio sigue deduplicado

_ddb = None


def _ddbc():
    global _ddb
    if _ddb is None:
        import boto3

        _ddb = boto3.client("dynamodb")
    return _ddb


def parse_body(raw: str) -> dict:
    """Con raw message delivery el cuerpo es el sobre; sin ella viene envuelto por SNS."""
    body = json.loads(raw)
    if isinstance(body, dict) and body.get("Type") == "Notification" and "Message" in body:
        body = json.loads(body["Message"])
    return body


def to_row(env: dict, ingested_at: str) -> dict:
    """Fila de domain_events. `payload` va como objeto: en NDJSON la columna JSON lo guarda como objeto (un string
    quedaria como escalar JSON y JSON_VALUE(payload, '$.x') daria NULL). tenant_id/incident_id/machine_id se
    toman del sobre y, si faltan, del payload (supply.* los lleva ahi); tenant_id NULL = mineria."""
    payload = env.get("payload") or {}
    return {
        "event_id": env["event_id"],
        "event_type": env["event_type"],
        "schema_version": env.get("schema_version"),
        "source": env.get("source"),
        "aggregate_type": env.get("aggregate_type"),
        "aggregate_id": env.get("aggregate_id"),
        "occurred_at": env["occurred_at"],
        "incident_id": env.get("incident_id") or payload.get("incident_id"),
        "machine_id": env.get("machine_id") or payload.get("machine_id"),
        "trace_id": env.get("trace_id"),
        "payload": payload,
        "ingested_at": ingested_at,
        "tenant_id": env.get("tenant_id") or payload.get("tenant_id"),
    }


def ndjson_gz(rows: list[dict]) -> bytes:
    data = "".join(json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n" for r in rows)
    return gzip.compress(data.encode("utf-8"), mtime=0)  # mtime=0: mismo lote, mismos bytes


def object_name(rows: list[dict]) -> str:
    """events/dt=<dia del primer evento>/<hash de los event_id>.ndjson.gz (determinista)."""
    ids = sorted(r["event_id"] for r in rows)
    digest = hashlib.sha256("\n".join(ids).encode()).hexdigest()[:24]
    day = min(r["occurred_at"] for r in rows)[:10]
    return f"events/dt={day}/{digest}.ndjson.gz"


def _key(event_id: str) -> dict:
    return {"message_id": {"S": f"{CONSUMER}:{event_id}"}}


def already_loaded(ddb, inbox: str, event_ids: list[str]) -> set[str]:
    """event_id ya marcados en fp-inbox (lecturas de 100 en 100, reintentando UnprocessedKeys)."""
    found: set[str] = set()
    for i in range(0, len(event_ids), 100):
        request = {inbox: {"Keys": [_key(e) for e in event_ids[i:i + 100]], "ProjectionExpression": "message_id"}}
        for attempt in range(5):
            resp = ddb.batch_get_item(RequestItems=request)
            for item in resp.get("Responses", {}).get(inbox, []):
                found.add(item["message_id"]["S"].split(":", 1)[1])
            request = resp.get("UnprocessedKeys") or {}
            if not request:
                break
            time.sleep(0.2 * (attempt + 1))
        if request:
            raise RuntimeError("fp-inbox: UnprocessedKeys tras 5 intentos")
    return found


def mark_loaded(ddb, inbox: str, event_ids: list[str], now) -> None:
    ttl = str(int(now.timestamp()) + INBOX_TTL_DAYS * 86400)
    processed_at = clock.iso(now)
    for i in range(0, len(event_ids), 25):
        puts = [{"PutRequest": {"Item": _key(e) | {"consumer": {"S": CONSUMER}, "processed_at": {"S": processed_at},
                                                   "ttl": {"N": ttl}}}} for e in event_ids[i:i + 25]]
        request = {inbox: puts}
        for attempt in range(5):
            request = ddb.batch_write_item(RequestItems=request).get("UnprocessedItems") or {}
            if not request:
                break
            time.sleep(0.2 * (attempt + 1))
        if request:
            raise RuntimeError("fp-inbox: UnprocessedItems tras 5 intentos")


def load_events(envelopes: list[dict], *, gcp, ddb, bucket: str, project: str, dataset: str, inbox: str,
                now=None) -> dict:
    """Carga los sobres nuevos con UN objeto en GCS y UN load job. Devuelve que se cargo y que se salto."""
    now = now or clock.now()
    unique: dict[str, dict] = {}
    for env in envelopes:
        unique.setdefault(env["event_id"], env)
    seen = already_loaded(ddb, inbox, list(unique))
    fresh = [env for eid, env in unique.items() if eid not in seen]
    out = {"loaded": [e["event_id"] for e in fresh], "skipped": len(envelopes) - len(fresh), "object": None}
    if not fresh:
        return out

    rows = [to_row(env, clock.iso(now)) for env in fresh]
    name = object_name(rows)
    gcp.upload(bucket, name, ndjson_gz(rows))
    gcp.load(project, dataset, TABLE, [f"gs://{bucket}/{name}"])
    out["object"] = name
    try:
        mark_loaded(ddb, inbox, out["loaded"], now)
    except Exception as exc:  # ya esta en BigQuery: fallar el lote solo lo duplicaria
        log.error("gcp-events: no se pudo marcar fp-inbox", error=type(exc).__name__, detail=str(exc)[:300])
    return out


def handler(event, context=None, gcp=None, ddb=None, now=None):
    records = event.get("Records", [])
    failures, good = [], []
    for record in records:
        try:
            env = parse_body(record["body"])
            schema.validate("envelope", env)
            good.append((record["messageId"], env))
        except (ValueError, KeyError, TypeError) as exc:  # ContractError y JSONDecodeError son ValueError
            log.error("gcp-events: sobre invalido", message_id=record.get("messageId"),
                      error=type(exc).__name__, detail=str(exc)[:300])
            failures.append({"itemIdentifier": record["messageId"]})

    if good:
        if gcp is None:
            from fplib import secrets

            from exp.gcp import GcpClient

            gcp = GcpClient(secrets.get_json(os.environ["SA_SECRET"]))
        try:
            out = load_events([env for _, env in good], gcp=gcp, ddb=ddb or _ddbc(),
                              bucket=os.environ["DST_BUCKET"], project=os.environ["GCP_PROJECT"],
                              dataset=os.environ.get("DATASET", "fleetpulse"),
                              inbox=os.environ.get("INBOX_TABLE", "fp-inbox"), now=now)
            log.event("gcp-events: lote cargado", records=len(records), loaded=len(out["loaded"]),
                      skipped=out["skipped"], object=out["object"])
        except Exception as exc:
            log.error("gcp-events: fallo el lote", records=len(good), error=type(exc).__name__,
                      detail=str(exc)[:300])
            failures += [{"itemIdentifier": mid} for mid, _ in good]

    return {"batchItemFailures": failures}
