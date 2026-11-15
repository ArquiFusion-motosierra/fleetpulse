"""Validacion de la cabecera `x-api-key` contra un secreto, en tiempo constante."""
from __future__ import annotations

import hmac

from fplib import secrets


def check_api_key(headers: dict, secret_name: str, client=None) -> bool:
    given = next((v for k, v in (headers or {}).items() if k.lower() == "x-api-key"), None)
    if not given:
        return False
    try:
        expected = secrets.get_json(secret_name, client=client).get("api_key")
    except Exception:  # secreto ausente o ilegible: se rechaza, sin filtrar el motivo
        return False
    if not expected:
        return False
    return hmac.compare_digest(str(given).encode(), str(expected).encode())
