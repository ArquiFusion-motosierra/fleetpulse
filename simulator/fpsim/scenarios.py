"""Escenarios inyectables (S1-S6) y fallas historicas del backfill.

En vivo, los escenarios S1 y S2 se activan con el parametro SSM /fleetpulse/sim/scenarios:
    {"S1": {"since": "2026-11-15T14:30:00Z"}, "S2": false}
El ritmo va por lote: un lote = una invocacion = un minuto real (TIME_WARP=5 -> 5 minutos de operacion).
S3, S4 y S6 son rasgos constantes de la maquina (ver fleet.py) y no necesitan perilla.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

S2_MACHINES = ("EX-003", "VQ-011", "VQ-014", "TO-002")
S2_LOT = "L-2311"
S7_MACHINE = "VQ-022"  # volquete de la zona norte: la junta de culata lo lleva a resonancia del chasis

# SPN SAE J1939 y codigos propios del fabricante (>= 520192) para hidraulica
SPN_COOLANT_TEMP = 110
SPN_OIL_PRESSURE = 100
SPN_FUEL_RATE = 183
SPN_RPM = 190
SPN_HYD_PRESSURE = 520193
SPN_HYD_TEMP = 520194
SPN_COOLANT_LEVEL = 111


@dataclass
class Effect:
    mult: dict = field(default_factory=dict)
    add: dict = field(default_factory=dict)
    noise: dict = field(default_factory=dict)
    faults: list = field(default_factory=list)

    def merge(self, other: "Effect") -> "Effect":
        for k, v in other.mult.items():
            self.mult[k] = self.mult.get(k, 1.0) * v
        for k, v in other.add.items():
            self.add[k] = self.add.get(k, 0.0) + v
        for k, v in other.noise.items():
            self.noise[k] = max(self.noise.get(k, 1.0), v)
        self.faults.extend(other.faults)
        return self


@dataclass(frozen=True)
class Fault:
    """Falla historica inyectada en el backfill (etiqueta para calibrar el score)."""
    machine_id: str
    failure_mode: str
    start: datetime
    end: datetime  # fin de la degradacion: aqui se repara y vuelve a normal


def parse_active(param: dict | None) -> dict[str, datetime]:
    """{"S1": {"since": iso}, "S2": true, "S3": false} -> {"S1": datetime}. true sin since no avanza."""
    active: dict[str, datetime] = {}
    for name, value in (param or {}).items():
        if not value:
            continue
        since = value.get("since") if isinstance(value, dict) else None
        if since:
            active[name] = datetime.fromisoformat(since.replace("Z", "+00:00")).astimezone(timezone.utc)
    return active


def batches_since(since: datetime, now: datetime) -> float:
    return max((now - since).total_seconds() / 60.0, 0.0)


def live_effect(machine_id: str, active: dict[str, datetime], now: datetime) -> Effect:
    eff = Effect()
    if "S1" in active and machine_id == "EX-017":
        k = batches_since(active["S1"], now)
        # S1 bomba gastada: presion -0,8 %/lote; aceite +0,5 C/lote; vibracion +2 %/lote; combustible +1 %/lote
        eff.merge(Effect(
            mult={"pressure": (1 - 0.008) ** k, "vibration": 1.02 ** k, "fuel": 1.01 ** k},
            add={"hyd_temp": 0.5 * k},
            faults=[{"spn": SPN_HYD_PRESSURE, "fmi": 1, "severity": "medium" if k < 15 else "high"}] if k >= 8 else [],
        ))
    if "S2" in active and machine_id in S2_MACHINES:
        k = batches_since(active["S2"], now)
        # S2 combustible contaminado: escalon de +12 % y luego deriva; rpm inestable; refrigerante +3 C
        eff.merge(Effect(
            mult={"fuel": 1.12 * (1.005 ** k)},
            add={"coolant": 3.0 + 0.1 * k},
            noise={"rpm": 3.0},
            faults=[{"spn": SPN_FUEL_RATE, "fmi": 0, "severity": "medium"}],
        ))
    if "S7" in active and machine_id == S7_MACHINE:
        k = batches_since(active["S7"], now)
        # S7 junta de culata: llega al punto de falla en ~20 lotes; las rpm suben ~15 % y el 2x cruza los 62 Hz
        eff.merge(signature("head_gasket", min(k / 20.0, 1.0)))
    if "S5" in active and machine_id == "EX-009":
        k = batches_since(active["S5"], now)
        # S5 reincidencia: misma firma hidraulica que hace 10 dias, lenta
        eff.merge(signature("hydraulic_contamination", min(k / 25.0, 1.0)))
    return eff


def signature(failure_mode: str, r: float) -> Effect:
    """Firma de cada modo de falla con avance r en [0, 1] (1 = punto de falla)."""
    r = max(0.0, min(r, 1.0))
    if failure_mode == "head_gasket":
        # Junta de culata (oem_specs.yaml): refrigerante en alza, perdida de refrigerante (SPN 111), combustion
        # desigual (rpm y consumo inestables). Las rpm suben al perder compresion: el regulador compensa.
        return Effect(add={"coolant": 16.0 * r}, mult={"rpm": 1 + 0.15 * r}, noise={"rpm": 1 + 1.5 * r, "fuel": 1 + 2.0 * r},
                      faults=[{"spn": SPN_COOLANT_LEVEL, "fmi": 1, "severity": "high" if r > 0.7 else "medium"}]
                      if r > 0.3 else [])
    if failure_mode == "hydraulic_pump":
        return Effect(mult={"pressure": 1 - 0.14 * r, "vibration": 1 + 0.45 * r, "fuel": 1 + 0.15 * r},
                      add={"hyd_temp": 9.0 * r},
                      faults=[{"spn": SPN_HYD_PRESSURE, "fmi": 1, "severity": "high"}] if r > 0.6 else [])
    if failure_mode == "hydraulic_contamination":
        return Effect(mult={"pressure": 1 - 0.08 * r, "vibration": 1 + 0.30 * r}, add={"hyd_temp": 7.0 * r},
                      faults=[{"spn": SPN_HYD_TEMP, "fmi": 0, "severity": "medium"}] if r > 0.6 else [])
    if failure_mode == "hydraulic_overheat":
        return Effect(add={"hyd_temp": 14.0 * r},
                      faults=[{"spn": SPN_HYD_TEMP, "fmi": 0, "severity": "high"}] if r > 0.5 else [])
    if failure_mode == "fuel_contamination":
        return Effect(mult={"fuel": 1 + 0.12 + 0.08 * r}, add={"coolant": 3.0 + 2.0 * r}, noise={"rpm": 3.0},
                      faults=[{"spn": SPN_FUEL_RATE, "fmi": 0, "severity": "medium"}])
    if failure_mode == "engine_overheat":
        return Effect(add={"coolant": 13.0 * r}, mult={"fuel": 1 + 0.05 * r},
                      faults=[{"spn": SPN_COOLANT_TEMP, "fmi": 0, "severity": "high"}] if r > 0.5 else [])
    if failure_mode == "final_drive":
        return Effect(mult={"vibration": 1 + 0.9 * r, "speed": 1 - 0.2 * r})
    if failure_mode == "brake_wear":
        return Effect(mult={"vibration": 1 + 0.5 * r, "speed": 1 - 0.15 * r})
    if failure_mode == "electrical_fault":
        return Effect(noise={"rpm": 1 + 3.0 * r},
                      faults=[{"spn": SPN_RPM, "fmi": 2, "severity": "low"}] if r > 0.3 else [])
    if failure_mode == "sensor_fault":
        return Effect(noise={"pressure": 1 + 5.0 * r},
                      faults=[{"spn": SPN_OIL_PRESSURE, "fmi": 2, "severity": "low"}] if r > 0.3 else [])
    raise ValueError(f"failure_mode desconocido: {failure_mode}")


def historical_effect(machine_id: str, faults_by_machine: dict[str, list[Fault]], t: datetime) -> Effect:
    eff = Effect()
    for f in faults_by_machine.get(machine_id, ()):
        if f.start <= t < f.end:
            r = (t - f.start).total_seconds() / (f.end - f.start).total_seconds()
            eff.merge(signature(f.failure_mode, r))
    return eff
