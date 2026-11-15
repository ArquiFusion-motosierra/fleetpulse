"""Lambda `gcp-exporter`: exporta cada hora COMPLETA de telemetria de S3 (fp-raw) a
BigQuery fleetpulse.telemetry pasando por GCS fp-landing (load job, nunca streaming).

Idempotencia: el item EXPORT#gcp/WATERMARK en fp-state guarda la ultima hora exportada;
solo se procesan horas mayores y ya cerradas (menores que la hora actual), en orden.
"""
from __future__ import annotations

import os
import re

from fplib import clock, log

_HOUR_RE = re.compile(r"^telemetry/dt=(\d{4}-\d{2}-\d{2})/hr=(\d{2})/")
_s3 = None
_table = None


def _s3c():
    global _s3
    if _s3 is None:
        import boto3

        _s3 = boto3.client("s3")
    return _s3


def _state_table():
    global _table
    if _table is None:
        import boto3

        _table = boto3.resource("dynamodb").Table(os.environ["STATE_TABLE"])
    return _table


def hour_of(key: str) -> str | None:
    """telemetry/dt=2026-11-15/hr=14/... -> '2026-11-15T14'."""
    m = _HOUR_RE.match(key)
    return f"{m.group(1)}T{m.group(2)}" if m else None


def due_hours(keys, last_hour: str | None, current_hour: str) -> list[str]:
    """Horas presentes, ya cerradas (< actual) y posteriores al watermark, ordenadas."""
    hours = {h for h in (hour_of(k) for k in keys) if h}
    low = last_hour or ""
    return sorted(h for h in hours if h > low and h < current_hour)


def _list_keys(s3, bucket: str, prefix: str = "telemetry/") -> list[str]:
    keys, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in resp.get("Contents", [])]
        if not resp.get("IsTruncated"):
            return keys
        token = resp.get("NextContinuationToken")


def _read_watermark(table) -> str | None:
    item = table.get_item(Key={"PK": "EXPORT#gcp", "SK": "WATERMARK"}).get("Item") or {}
    return item.get("last_hour")


def _write_watermark(table, hour: str) -> None:
    table.put_item(Item={"PK": "EXPORT#gcp", "SK": "WATERMARK", "last_hour": hour})


def handler(event=None, context=None, s3=None, gcp=None, table=None, now=None):
    src = os.environ["SRC_BUCKET"]
    dst = os.environ["DST_BUCKET"]
    project = os.environ["GCP_PROJECT"]
    dataset = os.environ.get("DATASET", "fleetpulse")

    s3 = s3 or _s3c()
    table = table or _state_table()
    if gcp is None:
        from fplib import secrets

        from exp.gcp import GcpClient

        gcp = GcpClient(secrets.get_json(os.environ["SA_SECRET"]))

    current_hour = (now or clock.now()).strftime("%Y-%m-%dT%H")
    keys = _list_keys(s3, src)
    hours = due_hours(keys, _read_watermark(table), current_hour)

    for hour in hours:
        hour_keys = [k for k in keys if hour_of(k) == hour]
        uris = []
        for key in hour_keys:
            body = s3.get_object(Bucket=src, Key=key)["Body"].read()
            gcp.upload(dst, key, body)
            uris.append(f"gs://{dst}/{key}")
        if uris:
            gcp.load(project, dataset, "telemetry", uris)
        _write_watermark(table, hour)
        log.event("hora exportada", hour=hour, objects=len(hour_keys))

    return {"hours": len(hours), "exported": hours}
