"""Genera una lectura AEMP / ISO 15143-3 + sensores para una maquina en un instante.

valor = base del modelo x factor de carga + ciclo de turno + ambiente + sesgo de la maquina + ruido + deriva

Es una funcion pura del instante (sin estado): los acumulados son lineales en el tiempo desde ANCHOR,
asi que el simulador en vivo (Lambda) y el backfill producen series continuas y monotonas sin guardar nada.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from fpsim import oem
from fpsim.fleet import MODEL_PARAMS, NOISE_SIGMA, STATIONS, Machine, unit_hash, zones
from fpsim.scenarios import Effect

ANCHOR = datetime(2026, 10, 1, tzinfo=timezone.utc)
LIMA = timedelta(hours=-5)
NOMINAL_ON_H = 14.0  # 12 h de turno pleno + 4 h a media carga
ZONE_ORDER = ("NORTE", "CENTRO", "SUR")
PIN_JITTER = 0.0004  # grados (~45 m de lado): muy por debajo de una celda geohash6 (~0,0055 x 0,011 grados)
RESONANCE_GAIN = 1.6  # el RMS de vibracion sube cuando un armonico de las rpm cruza una frecuencia natural


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_hour(t: datetime) -> float:
    lt = t + LIMA
    return lt.hour + lt.minute / 60.0


def slot_of(t: datetime) -> int:
    """Bloque de 15 minutos: el estado del motor y la carga cambian por bloque, no por lectura."""
    return int((t - ANCHOR).total_seconds() // 900)


def rates(m: Machine) -> tuple[float, float]:
    """Horas operando y en ralenti por hora de reloj (valor esperado)."""
    on_h = m.hours_per_day if m.hours_per_day is not None else NOMINAL_ON_H * m.availability
    return on_h * (1 - m.idle_ratio) / 24.0, on_h * m.idle_ratio / 24.0


def engine_status(m: Machine, t: datetime) -> str:
    h = local_hour(t)
    slot = slot_of(t)
    if m.hours_per_day is not None:
        start = 2.0 if m.hours_per_day >= 12 else 8.0
        on = start <= h < start + m.hours_per_day
    else:
        shift = 1.0 if 6 <= h < 18 else 0.5 if 18 <= h < 22 else 0.0
        on = unit_hash(m.machine_id, slot, "on") < m.availability * shift
    if not on:
        return "off"
    return "idle" if unit_hash(m.machine_id, slot, "idle") < m.idle_ratio else "operating"


def ambient_c(m: Machine, t: datetime, rng: random.Random) -> float:
    h = local_hour(t)
    zone_off = {"NORTE": -1.5, "CENTRO": 0.0, "SUR": 0.8}[m.zone_id]
    return 6.0 + 7.0 * math.sin(2 * math.pi * (h - 9) / 24) + zone_off + rng.gauss(0, 0.4)


def elapsed_hours(t: datetime, warp_extra_h: float = 0.0) -> float:
    return max((t - ANCHOR).total_seconds() / 3600.0, 0.0) + warp_extra_h


def fuel_cycle(m: Machine, e_h: float) -> tuple[float, int]:
    """(fuel_remaining_ratio, numero de recargas desde ANCHOR). Diente de sierra: recarga al 15 %."""
    p = m.params
    op_rate, idle_rate = rates(m)
    burn = (op_rate * p["fuel"] * 0.8 + idle_rate * p["fuel"] * 0.18) * e_h
    usable = 0.8 * p["tank_l"]
    phase = unit_hash(m.machine_id, "tank") * usable
    total = burn + phase
    return round(0.95 - (total % usable) / p["tank_l"], 3), int(total // usable)


def position(m: Machine, t: datetime, status: str, pin: str | None = None) -> tuple[float, float, str]:
    """`pin` = zona: la maquina queda junto a la estacion de esa zona (S2), en la misma celda geohash6 que las otras
    fijadas, con un temblor de unos 20 m."""
    slot = slot_of(t)
    if pin is not None:
        z = zones()[pin]
        lat = z["lat"] + (unit_hash(m.machine_id, slot, "jy") - 0.5) * PIN_JITTER
        lon = z["lon"] + (unit_hash(m.machine_id, slot, "jx") - 0.5) * PIN_JITTER
        return round(lat, 6), round(lon, 6), pin
    zone = m.zone_id
    lat, lon = m.zone_lat, m.zone_lon
    if m.model == "VQ" and status == "operating":
        # Los volquetes se mueven entre las 3 zonas por bloque
        zone = ZONE_ORDER[(slot + int(unit_hash(m.machine_id, "route") * 3)) % 3]
        z = zones()[zone]
        lat, lon = z["lat"], z["lon"]
    lat += (unit_hash(m.machine_id, "dlat") - 0.5) * 0.012 + (unit_hash(m.machine_id, slot, "jy") - 0.5) * 0.001
    lon += (unit_hash(m.machine_id, "dlon") - 0.5) * 0.012 + (unit_hash(m.machine_id, slot, "jx") - 0.5) * 0.001
    return round(lat, 6), round(lon, 6), zone


def reading(m: Machine, t: datetime, effect: Effect | None = None, warp_extra_h: float = 0.0,
            pin: str | None = None) -> dict:
    eff = effect or Effect()
    p = MODEL_PARAMS[m.model]
    rng = random.Random(f"{m.machine_id}|{iso(t)}")
    status = engine_status(m, t)
    slot = slot_of(t)
    lf = 0.7 + 0.4 * unit_hash(m.machine_id, slot, "lf") if status == "operating" else 0.0
    amb = ambient_c(m, t, rng)
    wear = m.wear

    if status == "operating":
        base = {
            "rpm": p["rpm"] * (0.8 + 0.2 * lf),
            "coolant": 70 + (p["coolant"] - 70) * (0.7 + 0.3 * lf) + 0.2 * (amb - 8) + 2.0 * wear,
            "hyd_temp": 35 + (p["hyd_temp"] - 35) * (0.7 + 0.3 * lf) + 0.15 * (amb - 8) + 1.5 * wear,
            "pressure": p["pressure"] * (0.75 + 0.25 * lf),
            "vibration": p["vibration"] * (0.8 + 0.2 * lf) * (1 + 0.3 * wear),
            "fuel": p["fuel"] * (0.55 + 0.45 * lf),
            "speed": p["speed"] * (0.5 + 0.5 * unit_hash(m.machine_id, slot, "sp")),
        }
        loaded = m.model != "VQ" or slot % 2 == 0
        load_t = p["load"] * lf if loaded else 0.0
    elif status == "idle":
        base = {"rpm": 750.0, "coolant": 74 + 0.2 * (amb - 8) + 1.5 * wear, "hyd_temp": 40 + 0.15 * (amb - 8),
                "pressure": p["pressure"] * 0.35, "vibration": p["vibration"] * 0.35, "fuel": p["fuel"] * 0.18,
                "speed": 0.0}
        load_t = 0.0
    else:
        base = {"rpm": 0.0, "coolant": amb + 8, "hyd_temp": amb + 5, "pressure": 0.0, "vibration": 0.0,
                "fuel": 0.0, "speed": 0.0}
        load_t = 0.0
    if m.model == "CF" and m.idle_ratio > 0.3:
        base["fuel"] *= 1.15  # S3: mas litros por tonelada

    base["coolant"] += m.bias.get("coolant", 0.0)
    values = {}
    for var, v in base.items():
        if status != "off" or var in ("coolant", "hyd_temp"):
            v = v * eff.mult.get(var, 1.0) + eff.add.get(var, 0.0)
        if status != "off":
            v += rng.gauss(0, NOISE_SIGMA[var] * eff.noise.get(var, 1.0) * (1 + 0.5 * wear))
        values[var] = max(v, 0.0)
    # Resonancia: un armonico de la velocidad de operacion cruza una frecuencia natural del modelo (oem_specs)
    if status == "operating" and oem.resonance(m.model, values["rpm"]):
        values["vibration"] *= RESONANCE_GAIN

    e_h = elapsed_hours(t, warp_extra_h)
    op_rate, idle_rate = rates(m)
    op_h = m.hours_total + op_rate * e_h
    idle_h = m.hours_total * m.idle_ratio + idle_rate * e_h
    fuel_ratio, _ = fuel_cycle(m, e_h)
    loads = int(op_h * p["cycles_h"])
    lat, lon, zone = position(m, t, status, pin)

    return {
        "machine_id": m.machine_id,
        "serial": m.serial,
        "model": m.model,
        "ts": iso(t),
        "lat": lat,
        "lon": lon,
        "zone_id": zone,
        "cumulative_operating_hours": round(op_h, 3),
        "cumulative_idle_hours": round(idle_h, 3),
        "cumulative_fuel_used_l": round(op_h * p["fuel"] * 0.8 + idle_h * p["fuel"] * 0.18, 1),
        "fuel_remaining_ratio": fuel_ratio,
        "engine_status": status,
        "cumulative_load_count": loads,
        "cumulative_payload_t": round(loads * p["cycle_t"], 1),
        "distance_km": round(op_h * p["km_h"], 2),
        "fault_codes": list(eff.faults) if status != "off" else [],
        "coolant_temp_c": round(min(values["coolant"], 150.0), 2),
        "hydraulic_oil_temp_c": round(min(values["hyd_temp"], 150.0), 2),
        "hydraulic_pressure_bar": round(min(values["pressure"], 500.0), 1),
        "vibration_mm_s": round(min(values["vibration"], 100.0), 3),
        "engine_rpm": round(min(values["rpm"], 3500.0), 0),
        "fuel_rate_lph": round(min(values["fuel"], 200.0), 2),
        "speed_kmh": round(min(values["speed"], 80.0), 1),
        "load_t": round(load_t, 2),
        "ambient_temp_c": round(amb, 2),
    }


def refill_context(m: Machine, t: datetime, lot: str | None = None) -> dict:
    """Evento fuel.refill de contexto (contracts/fuel.refill.schema.json)."""
    zone = m.zone_id
    return {
        "context_id": f"CTX-{t.strftime('%Y%m%dT%H%M%SZ')}-{int(unit_hash(m.machine_id, iso(t)) * 0xFFFFFF):06x}",
        "type": "fuel.refill",
        "ts": iso(t),
        "zone_id": zone,
        "machine_id": m.machine_id,
        "station_id": STATIONS[zone],
        "fuel_lot": lot or {"NORTE": "L-2311", "CENTRO": "L-2309", "SUR": "L-2307"}[zone],
        "liters": round(0.8 * m.params["tank_l"], 1),
    }


def weather_context(zone_id: str, t: datetime) -> dict:
    slot = slot_of(t)
    u = unit_hash(zone_id, slot // 4, "wx")  # cambia cada hora
    risk = {"NORTE": "dust", "CENTRO": "dust", "SUR": "rain"}[zone_id]
    condition = risk if u > 0.6 else "clear"
    return {
        "context_id": f"CTX-{t.strftime('%Y%m%dT%H%M%SZ')}-{int(unit_hash(zone_id, iso(t)) * 0xFFFFFF):06x}",
        "type": "weather",
        "ts": iso(t),
        "zone_id": zone_id,
        "condition": condition,
        "intensity": round((u - 0.6) / 0.4, 2) if condition != "clear" else 0.0,
    }
