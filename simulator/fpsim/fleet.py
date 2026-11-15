"""Flota fija de los tres parques (contracts/tenants.yaml: ids.yaml de mineria + tenants/*.yaml, unidos y vendorizados
como ids.json por sync_ids.py) y parametros fisicos por modelo.

FP_TENANTS=mineria,agro (por defecto todos) apaga parques sin tocar codigo."""
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
    # Parque agricola (agro): tractor y cosechadora
    "TA": dict(coolant=84.0, hyd_temp=55.0, pressure=190.0, vibration=5.0, rpm=2000.0, fuel=14.0,
               speed=9.0, load=0.0, cycle_t=0.0, cycles_h=0, km_h=7.0, tank_l=300.0, idle=0.15),
    "CS": dict(coolant=86.0, hyd_temp=60.0, pressure=210.0, vibration=7.0, rpm=2100.0, fuel=30.0,
               speed=6.0, load=8.0, cycle_t=8.0, cycles_h=4, km_h=5.0, tank_l=750.0, idle=0.12),
    # Parque industrial (industria): montacargas, compresor y grupo electrogeno (estos dos, estacionarios)
    "MC": dict(coolant=82.0, hyd_temp=48.0, pressure=170.0, vibration=3.5, rpm=2200.0, fuel=4.0,
               speed=8.0, load=2.5, cycle_t=2.5, cycles_h=25, km_h=5.0, tank_l=60.0, idle=0.25),
    "CP": dict(coolant=80.0, hyd_temp=85.0, pressure=120.0, vibration=3.0, rpm=1500.0, fuel=12.0,
               speed=0.0, load=0.0, cycle_t=0.0, cycles_h=0, km_h=0.0, tank_l=400.0, idle=0.20),
    "GE": dict(coolant=82.0, hyd_temp=60.0, pressure=100.0, vibration=3.0, rpm=1800.0, fuel=28.0,
               speed=0.0, load=0.0, cycle_t=0.0, cycles_h=0, km_h=0.0, tank_l=900.0, idle=0.10),
}

# Temperatura ambiente base por parque sobre la del tajo a 4 000 m (Ica y Lurin son calidos).
AMBIENT_OFFSET = {"mineria": 0.0, "agro": 16.0, "industria": 12.0}

# Sigma del ruido gaussiano por variable: calibrado para que el baseline no dispare falsas alarmas.
NOISE_SIGMA = dict(coolant=1.2, hyd_temp=1.0, pressure=6.0, vibration=0.35, rpm=35.0, fuel=1.2, speed=1.5)

DEFAULT_TENANT = "mineria"

# Modelos que recorren las zonas de su parque mientras operan (los demas quedan en su zona).
ROAMING = {"VQ": ("NORTE", "CENTRO", "SUR"), "TA": ("FUNDO-A", "FUNDO-B"), "CS": ("FUNDO-A", "FUNDO-B"),
           "MC": ("PLANTA-1", "PLANTA-2", "ALMACEN")}


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
    tenant_id: str = DEFAULT_TENANT
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


def enabled_tenants() -> tuple[str, ...]:
    """Parques simulados: FP_TENANTS (lista separada por comas) o todos los de ids.json, en orden de registro."""
    known = [t["tenant_id"] for t in load_ids().get("tenants") or [{"tenant_id": DEFAULT_TENANT}]]
    wanted = [t for t in os.environ.get("FP_TENANTS", "").split(",") if t]
    return tuple(t for t in known if not wanted or t in wanted)


def stations() -> dict[str, dict]:
    """Estacion de combustible por zona: `{zone_id: {station_id, lot}}`."""
    return {s["zone_id"]: s for s in load_ids()["fuel_stations"]}


@lru_cache(maxsize=1)
def fleet() -> tuple[Machine, ...]:
    ids = load_ids()
    zones = {z["zone_id"]: z for z in ids["zones"]}
    enabled = enabled_tenants()
    out = []
    for m in ids["machines"]:
        if m.get("tenant_id", DEFAULT_TENANT) not in enabled:
            continue
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
            zone_lat=z["lat"], zone_lon=z["lon"], tenant_id=m.get("tenant_id", DEFAULT_TENANT),
            availability=0.78 + 0.17 * unit_hash(mid, "avail"),
            idle_ratio=idle, hours_per_day=hours_per_day, bias=bias,
        ))
    return tuple(out)


def shift_factor(m: Machine, h: float) -> float:
    """Fraccion de la disponibilidad que opera a la hora `h` de Lima, por parque y modelo.
    Mineria: turno pleno 6-18 y medio turno 18-22. Agro: tractores 6-18 y media cuadrilla 18-22 (riego y fumigacion
    con luces); la cosechadora solo de dia, 7-18, cuando seca el rocio. Industria: dos turnos 6-22 y una cuadrilla de
    noche; el compresor sigue a la planta y el grupo electrogeno es respaldo (turno de dia 8-16 y 20 % en cortes)."""
    if m.tenant_id == "agro":
        if m.model == "CS":
            return 1.0 if 7 <= h < 18 else 0.0
        return 1.0 if 6 <= h < 18 else 0.5 if 18 <= h < 22 else 0.0
    if m.tenant_id == "industria":
        if m.model == "GE":
            return 1.0 if 8.0 <= h < 16.0 else 0.2  # turno de dia de respaldo (minimo para la linea base); 20 % en cortes
        return 1.0 if 6.0 <= h < 22.0 else (0.6 if m.model == "CP" else 0.25)
    return 1.0 if 6 <= h < 18 else 0.5 if 18 <= h < 22 else 0.0


def nominal_on_h(m: Machine) -> float:
    """Horas encendidas por dia con disponibilidad 1 (suma del turno hora a hora; mineria = 14)."""
    return sum(shift_factor(m, h + 0.5) for h in range(24))


def by_id() -> dict[str, Machine]:
    return {m.machine_id: m for m in fleet()}


def zones(tenant: str | None = None) -> dict[str, dict]:
    """Zonas de los parques simulados (o de uno)."""
    enabled = enabled_tenants()
    return {z["zone_id"]: z for z in load_ids()["zones"]
            if z.get("tenant_id", DEFAULT_TENANT) in enabled and (tenant is None or z.get("tenant_id", DEFAULT_TENANT) == tenant)}
