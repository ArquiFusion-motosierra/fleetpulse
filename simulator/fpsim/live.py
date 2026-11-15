"""Arma el lote en vivo de un minuto: 10 lecturas por maquina (cada 6 s reales) y el contexto por zona.

Funcion pura: el handler hace la E/S. batch_id es determinista por minuto, asi que si la Lambda se
reintenta, ingest recibe el mismo batch_id y lo puede descartar.
"""
from __future__ import annotations

import zlib
from datetime import datetime, timedelta, timezone

from fpsim.fleet import fleet, zones
from fpsim.physics import fuel_cycle, elapsed_hours, iso, reading, refill_context, weather_context
from fpsim.scenarios import S2_LOT, S2_MACHINES, batches_since, live_effect

READINGS_PER_MACHINE = 10
SPACING_S = 6


def floor_minute(t: datetime) -> datetime:
    return t.astimezone(timezone.utc).replace(second=0, microsecond=0)


def batch_id(sent_at: datetime) -> str:
    stamp = sent_at.strftime("%Y%m%dT%H%M%SZ")
    return f"B-{stamp}-{zlib.crc32(stamp.encode()) & 0xFFFFFFFF:08x}"


def warp_extra_h(t: datetime, live_anchor: datetime | None, warp: int) -> float:
    """Horas extra de operacion por TIME_WARP desde que arranco el vivo (acumulados avanzan x warp)."""
    if live_anchor is None or t <= live_anchor:
        return 0.0
    return (t - live_anchor).total_seconds() / 3600.0 * (warp - 1)


def build_batch(now: datetime, active: dict[str, datetime], warp: int = 5,
                live_anchor: datetime | None = None, machine_ids: set[str] | None = None) -> dict:
    sent_at = floor_minute(now)
    start = sent_at - timedelta(seconds=SPACING_S * READINGS_PER_MACHINE)
    readings = []
    for m in fleet():
        if machine_ids and m.machine_id not in machine_ids:
            continue
        for i in range(1, READINGS_PER_MACHINE + 1):
            t = start + timedelta(seconds=SPACING_S * i)
            readings.append(reading(m, t, live_effect(m.machine_id, active, t), warp_extra_h(t, live_anchor, warp)))
    return {
        "batch_id": batch_id(sent_at),
        "sent_at": iso(sent_at),
        "source": "simulator.live",
        "time_warp": warp,
        "readings": readings,
    }


def build_context(now: datetime, active: dict[str, datetime], warp: int = 5,
                  live_anchor: datetime | None = None) -> list[dict]:
    """Recargas de combustible ocurridas en el ultimo minuto, lote L-2311 al iniciar S2 y clima cada 15 min."""
    sent_at = floor_minute(now)
    prev = sent_at - timedelta(minutes=1)
    out = []
    s2_starting = "S2" in active and batches_since(active["S2"], sent_at) < 1.0
    for m in fleet():
        if s2_starting and m.machine_id in S2_MACHINES:
            out.append(refill_context(m, active["S2"].replace(microsecond=0), lot=S2_LOT))
            continue
        _, n_now = fuel_cycle(m, elapsed_hours(sent_at, warp_extra_h(sent_at, live_anchor, warp)))
        _, n_prev = fuel_cycle(m, elapsed_hours(prev, warp_extra_h(prev, live_anchor, warp)))
        if n_now > n_prev:
            out.append(refill_context(m, sent_at))
    if sent_at.minute % 15 == 0:
        out.extend(weather_context(z, sent_at) for z in zones())
    return out
