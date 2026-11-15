"""Lectura de secretos JSON de Secrets Manager con cache en memoria por nombre.

El cliente por defecto lleva timeouts explicitos (3 s conexion, 5 s lectura, 2 intentos): boto3 trae 60 s.
"""
from __future__ import annotations

import json
import time

_cache: dict[str, tuple[float, dict]] = {}


def _client():
    import boto3
    from botocore.config import Config
    return boto3.client("secretsmanager",
                        config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2}))


def get_json(name: str, ttl_s: int = 300, client=None) -> dict:
    hit = _cache.get(name)
    if hit and time.monotonic() - hit[0] < ttl_s:
        return hit[1]
    value = json.loads((client or _client()).get_secret_value(SecretId=name)["SecretString"])
    _cache[name] = (time.monotonic(), value)
    return value


def clear_cache() -> None:
    _cache.clear()
