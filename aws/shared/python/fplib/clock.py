"""Reloj UTC y formatos de tiempo ISO 8601 con `Z`; `now` se parchea en las pruebas."""
from __future__ import annotations

from datetime import datetime, timezone

FMT = "%Y-%m-%dT%H:%M:%SZ"


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(FMT)


def parse(s: str) -> datetime:
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


def window15(dt: datetime) -> str:
    """Inicio de la ventana de 15 minutos UTC como `YYYYMMDDTHHMM`."""
    dt = dt.astimezone(timezone.utc)
    return dt.replace(minute=dt.minute - dt.minute % 15).strftime("%Y%m%dT%H%M")
