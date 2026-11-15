"""Formato de objetos en S3: NDJSON comprimido con gzip y claves particionadas por hora."""
from __future__ import annotations

import gzip
import json
import uuid
from decimal import Decimal


def _default(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(f"no serializable: {type(o).__name__}")


def ndjson_gz(records: list[dict]) -> bytes:
    body = "".join(json.dumps(r, default=_default, separators=(",", ":")) + "\n" for r in records)
    return gzip.compress(body.encode("utf-8"))


def object_key(hour_ts: str, first_ts: str) -> str:
    """`telemetry/dt=YYYY-MM-DD/hr=HH/<first_ts_compact>-<8hex>.ndjson.gz`; `hour_ts` es ISO (>= hasta la hora)."""
    compact = first_ts.replace("-", "").replace(":", "")
    return f"telemetry/dt={hour_ts[:10]}/hr={hour_ts[11:13]}/{compact}-{uuid.uuid4().hex[:8]}.ndjson.gz"
