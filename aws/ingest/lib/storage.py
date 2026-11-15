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


def object_key(hour_ts: str, first_ts: str, tenant: str | None = None) -> str:
    """`telemetry/dt=YYYY-MM-DD/hr=HH/[tenant=<id>/]<first_ts_compact>-<8hex>.ndjson.gz`; `hour_ts` es ISO.

    `tenant=` es una subparticion Hive por parque: queda bajo `dt=`/`hr=`, asi el lifecycle por prefijo `telemetry/`
    y el exportador (que agrupa por hora) no cambian, y un parque se lee o se borra con un solo prefijo por hora."""
    compact = first_ts.replace("-", "").replace(":", "")
    tenant_part = f"tenant={tenant}/" if tenant else ""
    return (f"telemetry/dt={hour_ts[:10]}/hr={hour_ts[11:13]}/{tenant_part}"
            f"{compact}-{uuid.uuid4().hex[:8]}.ndjson.gz")
