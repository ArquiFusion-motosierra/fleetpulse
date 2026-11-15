"""Flota fija (contracts/ids.yaml, vendorizado como ids.json) y parametros fisicos por modelo."""
from __future__ import annotations

import json
import os
import zlib
from dataclasses import dataclass, field
from functools import lru_cache

IDS_JSON = os.path.join(os.path.dirname(__file__), "ids.json")

# Valores base por modelo con carga nominal (load factor = 1).
# tank_l: tanque; cycle_t: toneladas por ciclo; cycles_h: ciclos por hora operando.
MODEL_PARAMS = {
    "EX": dict(coolant=86.0, hyd_temp=58.0, pressure=320.0, vibration=6.5, rpm=1800.0, fuel=38.0,
               speed=1.0, load=4.0, cycle_t=4.0, cycles_h=48, km_h=0.3, tank_l=650.0, idle=0.15),
    "CF": dict(coolant=84.0, hyd_temp=52.0, pressure=220.0, vibration=5.5, rpm=1700.0, fuel=24.0,
               speed=8.0, load=6.0, cycle_t=6.0, cycles_h=40, km_h=6.0, tank_l=400.0, idle=0.20),
    "VQ": dict(coolant=88.0, hyd_temp=45.0, pressure=160.0, vibration=5.0, rpm=1650.0, fuel=45.0,
               speed=28.0, load=90.0, cycle_t=90.0, cycles_h=3, km_h=22.0, tank_l=900.0, idle=0.12),
    "MN": dict(coolant=82.0, hyd_temp=48.0, pressure=180.0, vibration=4.5, rpm=1600.0, fuel=18.0,
               speed=10.0, load=0.0, cycle_t=0.0, cycles_h=0, km_h=8.0, tank_l=350.0, idle=0.10),
    "TO": dict(coolant=90.0, hyd_temp=60.0, pressure=280.0, vibration=7.5, rpm=1900.0, fuel=42.0,
               speed=4.0, load=0.0, cycle_t=0.0, cycles_h=0, km_h=2.5, tank_l=700.0, idle=0.12),
}

# Sigma del ruido gaussiano por variable: calibrado para que el baseline no dispare falsas alarmas.
NOISE_SIGMA = dict(coolant=1.2, hyd_temp=1.0, pressure=6.0, vibration=0.35, rpm=35.0, fuel=1.2, speed=1.5)

STATIONS = {"NORTE": "EST-NORTE", "CENTRO": "EST-CENTRO", "SUR": "EST-SUR"}


def unit_hash(*parts: object) -> float:
    """Numero en [0, 1) determinista a partir de las partes (estable entre procesos)."""
    return (zlib.crc32("|".join(map(str, parts)).encode()) & 0xFFFFFFFF) / 2**32


@dataclass(frozen=True)
class Machine:
    machine_id: str
    model: str
    serial: str
    zone_id: str
    hours_total: float
    hours_since_service: float
    zone_lat: float
    zone_lon: float
    # Rasgos propios, deterministas por machine_id
    availability: float = 0.85
    idle_ratio: float = 0.15
    hours_per_day: float | None = None  # S6: horas fijas al dia
    bias: dict = field(default_factory=dict)

    @property
    def params(self) -> dict:
        return MODEL_PARAMS[self.model]

    @property
    def wear(self) -> float:
        """0..1 segun horas acumuladas: mas horas, mas temperatura base y mas ruido."""
        return min(self.hours_total / 25000.0, 1.0)


@lru_cache(maxsize=1)
def load_ids() -> dict:
    with open(IDS_JSON, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def fleet() -> tuple[Machine, ...]:
    ids = load_ids()
    zones = {z["zone_id"]: z for z in ids["zones"]}
    out = []
    for m in ids["machines"]:
        mid, model = m["machine_id"], m["model_id"]
        p = MODEL_PARAMS[model]
        idle = p["idle"] * (0.8 + 0.4 * unit_hash(mid, "idle"))
        hours_per_day = None
        bias = {"coolant": (unit_hash(mid, "bc") - 0.5) * 2.0}
        # S3: ralenti excesivo, 45 % frente a ~15 % de sus pares
        if mid == "CF-042":
            idle = 0.45
        # S4: 8 grados sobre su cluster desde siempre
        if mid == "MN-005":
            bias["coolant"] = 8.0
        # S6: uso desbalanceado
        if mid in {"VQ-031", "VQ-032", "VQ-033", "VQ-034", "VQ-035"}:
            hours_per_day = 2.0
        if mid == "MN-003":
            hours_per_day = 20.0
        z = zones[m["zone_id"]]
        out.append(Machine(
            machine_id=mid, model=model, serial=m["serial"], zone_id=m["zone_id"],
            hours_total=float(m["hours_total"]), hours_since_service=float(m["hours_since_service"]),
            zone_lat=z["lat"], zone_lon=z["lon"],
            availability=0.78 + 0.17 * unit_hash(mid, "avail"),
            idle_ratio=idle, hours_per_day=hours_per_day, bias=bias,
        ))
    return tuple(out)


def by_id() -> dict[str, Machine]:
    return {m.machine_id: m for m in fleet()}


def zones() -> dict[str, dict]:
    return {z["zone_id"]: z for z in load_ids()["zones"]}
