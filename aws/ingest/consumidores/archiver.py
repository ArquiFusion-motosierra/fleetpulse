"""Lambda `archiver`: consume el stream de fp-state y archiva las lecturas `R#` en S3 como NDJSON gzip.

Idempotencia: cada invocacion escribe objetos nuevos (sufijo aleatorio); un reintento de lote deja
duplicados en S3 que el consumidor aguas abajo (Athena/BigQuery) deduplica por (machine_id, ts). Un fallo
de S3 lanza la excepcion para que todo el lote se reintente (con bisect en el event source mapping).
"""
from __future__ import annotations

import os
from collections import defaultdict

from boto3.dynamodb.types import TypeDeserializer

from fplib import log
from lib import storage

_DESER = TypeDeserializer()
_s3 = None
_INTERNAL = ("PK", "SK", "ttl")


def _client():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3")
    return _s3


def _readings(event: dict) -> list[dict]:
    out = []
    for rec in event.get("Records", []):
        if rec.get("eventName") not in ("INSERT", "MODIFY"):
            continue
        image = (rec.get("dynamodb") or {}).get("NewImage")
        if not image:
            continue
        item = {k: _DESER.deserialize(v) for k, v in image.items()}
        if str(item.get("SK", "")).startswith("R#"):
            out.append({k: v for k, v in item.items() if k not in _INTERNAL})
    return out


def handler(event, context=None, s3=None):
    records = _readings(event)
    by_hour: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_hour[r["ts"][:13]].append(r)
    s3 = s3 or _client()
    bucket = os.environ["BUCKET"]
    for hour, group in by_hour.items():
        group.sort(key=lambda r: r["ts"])
        key = storage.object_key(hour, group[0]["ts"])
        s3.put_object(Bucket=bucket, Key=key, Body=storage.ndjson_gz(group), ContentEncoding="gzip",
                      ContentType="application/x-ndjson")
    log.event("archivado", objects=len(by_hour), records=len(records))
    return {"objects": len(by_hour), "records": len(records)}
