"""Puntaje de anomalia por maquina y lote: z-scores contra la linea base propia y la de pares, mas tendencia.

Funciones puras, sin I/O. Idempotencia: mismos insumos, mismo resultado, asi que reprocesar un lote
reescribe el mismo item `S#`.

Solo se puntuan lecturas `operating` (ralenti y apagado tienen otra fisica). Con z_v = el de mayor |z| entre
propio y pares:

    score = suma_v(peso_v * max(0, |z_v| - DEADBAND)) + TREND_STEP * #{v : |z_v| >= DEADBAND y sube 3 lotes seguidos}

La banda muerta (DEADBAND = 2.0) descuenta el ruido sano: sumar seis |z| sin banda deja un lote sano en
~2.2 (mediana), sobre `schedule`. Umbrales sin cambio: observe 1.2, schedule 2.0, immediate 3.5.
Calibracion (replay del simulador real con la linea base de `scripts/seed-baselines.py`, 100 maquinas;
`FP_CALIBRATION=1 pytest -s aws/scoring/tests/test_calibration.py`): 0 maquinas >= 2.0 en 2 h sin deriva (max
1.92; en 24 h, 3 lotes sueltos empujados por la tendencia); S1 cruza `schedule` en 5-10 min si EX-017 esta
operando (hasta 20 min si cae un bloque en ralenti) y siempre como hydraulic_pump; S2 detectado en las 4 maquinas
como fuel_contamination en 5-13 min.
"""
from __future__ import annotations

VARS = ("coolant_temp_c", "hydraulic_oil_temp_c", "hydraulic_pressure_bar", "vibration_mm_s", "fuel_rate_lph",
        "engine_rpm")
WEIGHTS = {"hydraulic_pressure_bar": 1.0, "vibration_mm_s": 1.0, "coolant_temp_c": 0.9,
           "hydraulic_oil_temp_c": 0.9, "fuel_rate_lph": 0.8, "engine_rpm": 0.5}
THRESHOLDS = {"observe": 1.2, "schedule": 2.0, "immediate": 3.5}
SCORED_STATUS = "operating"
DEADBAND = 2.0
SIGMA_FLOOR = 1e-6
SIGMA_REL_FLOOR = 0.01  # sigma minima = 1 % de |mu|: una linea base degenerada (sigma ~0) no satura el z
DEVIATED_Z = 1.0
TREND_STEP = 0.5


def batch_means(readings: list[dict]) -> dict[str, float]:
    on = [r for r in readings if r.get("engine_status") == SCORED_STATUS]
    out: dict[str, float] = {}
    for v in VARS:
        vals = [float(r[v]) for r in on if r.get(v) is not None]
        if vals:
            out[v] = sum(vals) / len(vals)
    return out


def _z(value: float, base: dict | None) -> float | None:
    if not base:
        return None
    mu, sigma = float(base["mu"]), float(base["sigma"])
    return (value - mu) / max(sigma, SIGMA_REL_FLOOR * abs(mu), SIGMA_FLOOR)


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


def trend_bonus(means: dict, previous_means: list[dict], only: set[str] | None = None) -> int:
    """`previous_means` en orden cronologico (el mas antiguo primero); necesita 2 lotes previos.
    `only` limita las variables que cuentan (None = todas)."""
    if len(previous_means) < 2:
        return 0
    a, b = previous_means[-2], previous_means[-1]
    return sum(1 for v in VARS if (only is None or v in only) and v in means and v in a and v in b
               and a[v] < b[v] < means[v])


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
    base = sum(WEIGHTS[v] * max(0.0, abs(d) - DEADBAND) for v, d in dominant.items())
    out_of_band = {v for v, d in dominant.items() if abs(d) >= DEADBAND}
    total = base + TREND_STEP * trend_bonus(means, previous_means, out_of_band)
    deviated = sorted(
        ({"var": v, "value": means[v], "z": d} for v, d in dominant.items() if abs(d) >= DEVIATED_Z),
        key=lambda x: abs(x["z"]), reverse=True)
    if not deviated and dominant and total >= THRESHOLDS["schedule"]:
        # incident.opened exige al menos una variable desviada: va la de mayor |z|
        v, d = max(dominant.items(), key=lambda kv: abs(kv[1]))
        deviated = [{"var": v, "value": means[v], "z": d}]
    return {"score": total, "severity": severity(total), "z": z, "deviated_vars": deviated}
