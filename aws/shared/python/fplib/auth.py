"""Validacion de la cabecera `x-api-key` contra un secreto, en tiempo constante.

Cada rechazo tiene un motivo sin valores (`missing_header`, `secret_unavailable:<TipoDeExcepcion>`, `mismatch`):
`check_api_key` lo loguea como `api_key_rechazada` con el nombre del secreto; `rejection` solo lo devuelve, para
quien prueba varias llaves y loguea una sola linea (ver incidents/api/close.py). Nunca se loguea una llave.
"""
from __future__ import annotations

import hmac

from fplib import log, secrets


def rejection(headers: dict, secret_name: str, client=None) -> str | None:
    """None si la llave es valida; si no, el motivo del rechazo."""
    given = next((v for k, v in (headers or {}).items() if k.lower() == "x-api-key"), None)
    if not given:
        return "missing_header"
    try:
        expected = secrets.get_json(secret_name, client=client).get("api_key")
    except Exception as exc:  # secreto ausente, ilegible o sin JSON: se rechaza; solo el tipo sale en el log
        return f"secret_unavailable:{type(exc).__name__}"
    if not expected:
        return "secret_unavailable:MissingApiKey"
    return None if hmac.compare_digest(str(given).encode(), str(expected).encode()) else "mismatch"


def check_api_key(headers: dict, secret_name: str, client=None) -> bool:
    reason = rejection(headers, secret_name, client=client)
    if reason:
        log.error("api_key_rechazada", reason=reason, secret=secret_name)
    return reason is None
