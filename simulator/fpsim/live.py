"""Arma el lote en vivo de un minuto: 10 lecturas por maquina (cada 6 s reales) y el contexto por zona.

Funcion pura: el handler hace la E/S. batch_id es determinista por minuto, asi que si la Lambda se
reintenta, ingest recibe el mismo batch_id y lo puede descartar.

Multi-tenancy: un lote por parque y por minuto (`tenant_id` en el lote; ingest rechaza maquinas de otro parque). El
lote de mineria conserva el batch_id de antes (crc del minuto); el de otro parque mezcla el parque en el crc.
"""
from __future__ import annotations

import zlib
from datetime import datetime, timedelta, timezone

from fpsim.fleet import DEFAULT_TENANT, enabled_tenants, fleet, zones
from fpsim.physics import fuel_cycle, elapsed_hours, iso, reading, refill_context, weather_context
from fpsim.scenarios import S2_LOT, S2_MACHINES, batches_since, live_effect, live_pin

READINGS_PER_MACHINE = 10
SPACING_S = 6


def floor_minute(t: datetime) -> datetime:
    return t.astimezone(timezone.utc).replace(second=0, microsecond=0)


def batch_id(sent_at: datetime, tenant: str = DEFAULT_TENANT) -> str:
    stamp = sent_at.strftime("%Y%m%dT%H%M%SZ")
    seed = stamp if tenant == DEFAULT_TENANT else f"{stamp}|{tenant}"
    return f"B-{stamp}-{zlib.crc32(seed.encode()) & 0xFFFFFFFF:08x}"


def warp_extra_h(t: datetime, live_anchor: datetime | None, warp: int) -> float:
    """Horas extra de operacion por TIME_WARP desde que arranco el vivo (acumulados avanzan x warp)."""
    if live_anchor is None or t <= live_anchor:
        return 0.0
    return (t - live_anchor).total_seconds() / 3600.0 * (warp - 1)


def build_batch(now: datetime, active: dict[str, datetime], warp: int = 5,
                live_anchor: datetime | None = None, machine_ids: set[str] | None = None,
                tenant: str = DEFAULT_TENANT) -> dict:
    sent_at = floor_minute(now)
    start = sent_at - timedelta(seconds=SPACING_S * READINGS_PER_MACHINE)
    readings = []
    for m in fleet():
        if m.tenant_id != tenant or (machine_ids and m.machine_id not in machine_ids):
            continue
        for i in range(1, READINGS_PER_MACHINE + 1):
            t = start + timedelta(seconds=SPACING_S * i)
            readings.append(reading(m, t, live_effect(m.machine_id, active, t), warp_extra_h(t, live_anchor, warp),
                                    pin=live_pin(m.machine_id, active, t)))
    return {
        "batch_id": batch_id(sent_at, tenant),
        "sent_at": iso(sent_at),
        "source": "simulator.live",
        "tenant_id": tenant,
        "time_warp": warp,
        "readings": readings,
    }


def build_batches(now: datetime, active: dict[str, datetime], warp: int = 5,
                  live_anchor: datetime | None = None) -> list[dict]:
    """Un lote por parque simulado (FP_TENANTS); cada lote cabe en el contrato (<= 100 maquinas x 10 lecturas)."""
    batches = [build_batch(now, active, warp, live_anchor, tenant=t) for t in enabled_tenants()]
    return [b for b in batches if b["readings"]]


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
