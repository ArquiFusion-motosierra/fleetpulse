"""Logs de una linea JSON por evento, a nivel INFO (o ERROR). Nunca pasar secretos como campos."""
from __future__ import annotations

import json
import logging

_log = logging.getLogger("fplib")
_log.setLevel(logging.INFO)


def _line(msg: str, level: str, fields: dict) -> str:
    return json.dumps({"level": level, "msg": msg, **fields}, default=str, separators=(",", ":"))


def event(msg: str, **fields) -> None:
    _log.info(_line(msg, "INFO", fields))


def error(msg: str, **fields) -> None:
    _log.error(_line(msg, "ERROR", fields))
