"""Lambda `simulator`: cada minuto envia el lote en vivo a POST /telemetry y el contexto a POST /context.

Lee escenarios de SSM (/fleetpulse/sim/scenarios, cache 60 s), la URL del API de SSM (/fleetpulse/api/url)
y la API key de Secrets Manager (fleetpulse/api/ingest-key, cache 5 min). Nunca loguea la key.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from fpsim.live import build_batch, build_context
from fpsim.scenarios import parse_active

log = logging.getLogger()
log.setLevel(logging.INFO)

SCENARIOS_PARAM = os.environ.get("SCENARIOS_PARAM", "/fleetpulse/sim/scenarios")
API_URL_PARAM = os.environ.get("API_URL_PARAM", "/fleetpulse/api/url")
API_KEY_SECRET = os.environ.get("API_KEY_SECRET", "fleetpulse/api/ingest-key")
TIME_WARP = int(os.environ.get("TIME_WARP", "5"))
HTTP_TIMEOUT_S = float(os.environ.get("HTTP_TIMEOUT_S", "10"))

_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl_s: float, loader):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl_s:
        return hit[1]
    value = loader()
    _cache[key] = (time.monotonic(), value)
    return value


def _ssm():
    import boto3
    return boto3.client("ssm")


def _secrets():
    import boto3
    return boto3.client("secretsmanager")


def load_scenarios(ssm=None) -> dict:
    def _load():
        try:
            raw = (ssm or _ssm()).get_parameter(Name=SCENARIOS_PARAM)["Parameter"]["Value"]
            return json.loads(raw)
        except Exception as exc:  # parametro ausente o JSON roto: sin escenarios, no se cae el simulador
            log.warning(json.dumps({"msg": "scenarios_unavailable", "error": type(exc).__name__}))
            return {}
    return _cached("scenarios", 60, _load)


def load_api(ssm=None, secrets=None) -> tuple[str, str]:
    url = _cached("api_url", 300, lambda: (ssm or _ssm()).get_parameter(Name=API_URL_PARAM)["Parameter"]["Value"])
    key = _cached("api_key", 300, lambda: json.loads(
        (secrets or _secrets()).get_secret_value(SecretId=API_KEY_SECRET)["SecretString"])["api_key"])
    return url.rstrip("/"), key


def post_json(url: str, api_key: str, body: object, opener=urllib.request.urlopen) -> int:
    req = urllib.request.Request(
        url, data=json.dumps(body, separators=(",", ":")).encode(), method="POST",
        headers={"content-type": "application/json", "x-api-key": api_key})
    try:
        with opener(req, timeout=HTTP_TIMEOUT_S) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def live_anchor() -> datetime | None:
    raw = os.environ.get("LIVE_ANCHOR")
    return datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None


def handler(event=None, context=None, now: datetime | None = None, ssm=None, secrets=None,
            opener=urllib.request.urlopen):
    now = now or datetime.now(timezone.utc)
    active = parse_active(load_scenarios(ssm))
    batch = build_batch(now, active, TIME_WARP, live_anchor())
    ctx = build_context(now, active, TIME_WARP, live_anchor())
    url, key = load_api(ssm, secrets)

    status = post_json(f"{url}/telemetry", key, batch, opener)
    ctx_status = post_json(f"{url}/context", key, ctx, opener) if ctx else None
    log.info(json.dumps({"msg": "batch_sent", "batch_id": batch["batch_id"], "readings": len(batch["readings"]),
                         "status": status, "context": len(ctx), "context_status": ctx_status,
                         "scenarios": sorted(active)}))
    if status >= 500 or status == 429:
        # El reintento asincrono de Lambda reenvia el mismo batch_id: ingest lo descarta si ya entro
        raise RuntimeError(f"ingest respondio {status} para {batch['batch_id']}")
    return {"batch_id": batch["batch_id"], "status": status, "readings": len(batch["readings"]), "context": len(ctx)}
