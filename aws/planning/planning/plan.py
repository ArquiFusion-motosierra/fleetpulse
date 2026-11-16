"""Calculo de daily-planning (T18): preventivo por uso real y reorden contra el stock. Funciones puras, sin I/O.

Por maquina, con f = factor de desgaste por severidad y r = horas de operacion por dia:

    desde    = hours_total - last_service_h
    restante = service_interval_h - desde * f
    dias     = max(restante, 0) / (r * f)        -> vence el dia de Lima hoy + floor(dias), a las 08:00 (13:00Z)

Es la formula del diseno (horas desde el servicio x factor x (1 + score de 7 dias)) con la severidad actual en
lugar del score de 7 dias: los `S#` duran 3 dias y son demasiados para promediarlos en cada corrida.
Reorden por (repuesto, zona): proyectado = disponible + en pedido - demanda a 14 dias; si queda bajo el punto de
reorden se pide la diferencia.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

from fplib import clock

LIMA = timezone(timedelta(hours=-5))
DUE_HOUR_UTC = 13  # 08:00 de Lima, como el ejemplo del contrato maintenance.order
WEAR = {"ok": 1.0, "observe": 1.1, "schedule": 1.25, "immediate": 1.5}
DEFAULT_HOURS_PER_DAY = 12.0
MIN_HOURS_PER_DAY, MAX_HOURS_PER_DAY = 1.0, 24.0
MIN_SPAN = timedelta(hours=6)  # menos historia que esto no alcanza para estimar el uso diario
SOON_DAYS = 7


def lima_day(now: datetime) -> date:
    return now.astimezone(LIMA).date()


def due_at(day: date) -> str:
    return f"{day.isoformat()}T{DUE_HOUR_UTC:02d}:00:00Z"


def wear(status: str | None) -> float:
    return WEAR.get(status or "ok", 1.0)


def hours_per_day(meta: dict, oldest: dict | None) -> float:
    """Horas de operacion por dia entre la lectura mas antigua de la ventana y el META; 12 h si no hay historia."""
    if not oldest or not meta.get("last_ts"):
        return DEFAULT_HOURS_PER_DAY
    span = clock.parse(meta["last_ts"]) - clock.parse(oldest["ts"])
    if span < MIN_SPAN:
        return DEFAULT_HOURS_PER_DAY
    rate = (float(meta["hours_total"]) - float(oldest["cumulative_operating_hours"])) / (span / timedelta(days=1))
    return min(max(rate, MIN_HOURS_PER_DAY), MAX_HOURS_PER_DAY)


def last_service_h(meta: dict, catalog: dict) -> float | None:
    """Horometro del ultimo servicio: `META.last_service_h` si existe; si no, el del catalogo (ids.yaml)."""
    if meta.get("last_service_h") is not None:
        return float(meta["last_service_h"])
    if catalog.get("hours_total") is not None and catalog.get("hours_since_service") is not None:
        return float(catalog["hours_total"]) - float(catalog["hours_since_service"])
    return None


def service_kit(model: str, from_h: float, to_h: float, bom: list[dict]) -> list[dict]:
    """Repuestos cuya vida (`replace_every_h`) cumple un multiplo entre el ultimo servicio y el siguiente."""
    kit: dict[str, int] = {}
    for line in bom:
        every = float(line["replace_every_h"])
        if line["model_id"] == model and math.floor(to_h / every) > math.floor(from_h / every):
            kit[line["part_id"]] = max(kit.get(line["part_id"], 0), int(line["qty"]))
    return [{"part_id": p, "qty": q} for p, q in sorted(kit.items())]


def _reason(since: float, interval: float, days: float, rate: float, status: str, f: float, horizon: int) -> str:
    when = "vencido" if days == 0 else f"vence en ~{days:.1f} dias a {rate:.0f} h/dia"
    text = (f"Preventivo a {SOON_DAYS if days <= SOON_DAYS else horizon} dias: {since:.0f} h desde el ultimo servicio "
            f"(intervalo {interval:.0f} h), {when}")
    if f > 1:
        text += f"; estado {status} acelera el desgaste x{f:g}"
    return text[:300]


def assess(meta: dict, catalog: dict, interval: float, bom: list[dict], rate: float, today: date,
           horizon_days: int) -> dict | None:
    """Plan de una maquina si su servicio vence dentro del horizonte; None si no vence o no hay historia."""
    last = last_service_h(meta, catalog)
    if last is None or not interval:
        return None
    since = max(float(meta["hours_total"]) - last, 0.0)
    f = wear(meta.get("status"))
    remaining = float(interval) - since * f
    days = max(remaining, 0.0) / (rate * f)
    if days > horizon_days:
        return None
    day = today + timedelta(days=math.floor(days))
    return {
        "machine_id": meta["machine_id"], "zone_id": meta["zone_id"], "model": meta["model"],
        "order_id": f"ORD-{meta['machine_id']}-{day:%Y%m%d}", "due_at": due_at(day),
        "days_to_due": round(days, 2), "hours_since_service": round(since, 1), "service_interval_h": interval,
        "hours_per_day": round(rate, 1), "wear": f,
        "parts": service_kit(meta["model"], last, last + float(interval), bom),
        "reason": _reason(since, float(interval), days, rate, meta.get("status") or "ok", f, horizon_days),
    }


def demand(entries: list[dict]) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = {}
    for e in entries:
        for p in e["parts"]:
            k = (p["part_id"], e["zone_id"])
            out[k] = out.get(k, 0) + int(p["qty"])
    return out


def reorders(stock: dict[tuple[str, str], dict], demand_14d: dict[tuple[str, str], int],
             on_order: dict[tuple[str, str], int]) -> list[dict]:
    """Pedidos sugeridos por (repuesto, zona), ordenados; una fila de stock que falta cuenta como 0 disponible."""
    out = []
    for part, zone in sorted(set(stock) | set(demand_14d)):
        row = stock.get((part, zone)) or {}
        available, point = int(row.get("available", 0)), int(row.get("reorder_point", 0))
        need, pending = demand_14d.get((part, zone), 0), on_order.get((part, zone), 0)
        projected = available + pending - need
        if projected < point:
            out.append({"part_id": part, "zone_id": zone, "qty": point - projected, "available": available,
                        "reorder_point": point, "demand_14d": need, "on_order": pending})
    return out
