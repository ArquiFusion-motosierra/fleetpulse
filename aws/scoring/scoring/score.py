"""Puntaje de anomalia por maquina y lote: z-scores contra la linea base propia y la de pares, mas tendencia.

Funciones puras, sin I/O. Idempotencia: mismos insumos, mismo resultado, asi que reprocesar un lote
reescribe el mismo item `S#`.

score = suma(peso_var * max(|z_self|, |z_peer|)) + TREND_STEP * variables en alza sostenida (3 lotes).
"""
from __future__ import annotations

VARS = ("coolant_temp_c", "hydraulic_oil_temp_c", "hydraulic_pressure_bar", "vibration_mm_s", "fuel_rate_lph",
        "engine_rpm")
WEIGHTS = {"hydraulic_pressure_bar": 1.0, "vibration_mm_s": 1.0, "coolant_temp_c": 0.9,
           "hydraulic_oil_temp_c": 0.9, "fuel_rate_lph": 0.8, "engine_rpm": 0.5}
THRESHOLDS = {"observe": 1.2, "schedule": 2.0, "immediate": 3.5}
SIGMA_FLOOR = 1e-6
DEVIATED_Z = 1.0
TREND_STEP = 0.5


def batch_means(readings: list[dict]) -> dict[str, float]:
    on = [r for r in readings if r.get("engine_status") != "off"]
    out: dict[str, float] = {}
    for v in VARS:
        vals = [float(r[v]) for r in on if r.get(v) is not None]
        if vals:
            out[v] = sum(vals) / len(vals)
    return out


def _z(value: float, base: dict | None) -> float | None:
    if not base:
        return None
    return (value - float(base["mu"])) / max(float(base["sigma"]), SIGMA_FLOOR)


def zscores(means: dict, self_baseline: dict | None, peer_baseline: dict | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for v in VARS:
        if v not in means:
            continue
        zs = _z(means[v], (self_baseline or {}).get(v))
        zp = _z(means[v], (peer_baseline or {}).get(v))
        if zs is None and zp is None:
            continue
        out[v] = {"self": zs, "peer": zp}
    return out


def trend_bonus(means: dict, previous_means: list[dict]) -> int:
    """`previous_means` en orden cronologico (el mas antiguo primero); necesita 2 lotes previos."""
    if len(previous_means) < 2:
        return 0
    a, b = previous_means[-2], previous_means[-1]
    return sum(1 for v in VARS if v in means and v in a and v in b and a[v] < b[v] < means[v])


def severity(value: float) -> str:
    for name in ("immediate", "schedule", "observe"):
        if value >= THRESHOLDS[name]:
            return name
    return "ok"


def _dominant(zz: dict) -> float:
    vals = [x for x in (zz["self"], zz["peer"]) if x is not None]
    return max(vals, key=abs)


def compute(means: dict, self_baseline: dict | None, peer_baseline: dict | None, previous_means: list[dict]) -> dict:
    z = zscores(means, self_baseline, peer_baseline)
    dominant = {v: _dominant(zz) for v, zz in z.items()}
    base = sum(WEIGHTS[v] * abs(d) for v, d in dominant.items())
    total = base + TREND_STEP * trend_bonus(means, previous_means)
    deviated = sorted(
        ({"var": v, "value": means[v], "z": d} for v, d in dominant.items() if abs(d) >= DEVIATED_Z),
        key=lambda x: abs(x["z"]), reverse=True)
    return {"score": total, "severity": severity(total), "z": z, "deviated_vars": deviated}
