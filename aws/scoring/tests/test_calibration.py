"""Calibracion de Capa 1 contra el simulador real (lento, ~4 min): `FP_CALIBRATION=1 pytest -s -m slow ...`.

Siembra la linea base como `scripts/seed-baselines.py` (24 h, un lote por minuto, sin escenarios) y reproduce
lotes de `fpsim.live.build_batch` por `score.batch_means` -> `score.compute` -> `failure_mode.infer`, en el mismo
orden que `handler._score_group` (los 2 lotes puntuados anteriores alimentan la tendencia; sin pares).
Criterio del documento de diseno: detectar las fallas inyectadas con a lo sumo 1 falsa alarma/dia/100 maquinas.
"""
import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(os.environ.get("FP_CALIBRATION") != "1", reason="FP_CALIBRATION=1 para correrlo")]

REPO = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 11, 15, 14, 0, tzinfo=timezone.utc)  # 09:00 Lima, fin de la ventana sembrada
NO_DRIFT_MIN = 120
S1_OFFSETS = range(0, 60, 5)  # inicios de la deriva (min desde T0): cubre los 4 bloques de 15 min de una hora
S1_MAX_OPERATING_BATCHES = 15
S2_START_MIN = 20
S2_MAX_MIN = 45


@pytest.fixture(scope="module")
def sim():
    import sys
    if str(REPO / "simulator") not in sys.path:
        sys.path.append(str(REPO / "simulator"))
    spec = importlib.util.spec_from_file_location("seed_baselines", REPO / "scripts" / "seed-baselines.py")
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    from fpsim.live import build_batch
    from fpsim.scenarios import S2_MACHINES
    from scoring import baselines

    computed = seed.machine_baselines(T0)
    # misma ida y vuelta que META: Decimal redondeado a 6 decimales y de vuelta a float
    base = {m: baselines._floats(baselines._decimals(b)) for m, (b, _) in computed.items() if b is not None}
    return {"build_batch": build_batch, "S2_MACHINES": S2_MACHINES, "base": base,
            "insufficient": sorted(m for m, (b, _) in computed.items() if b is None)}


def replay(sim, start, minutes, active=None, machine_ids=None):
    """{machine_id: [(minuto desde start, score, deviated_vars)]} solo para los lotes puntuados."""
    from scoring import score
    prev, out = {}, {}
    for k in range(minutes):
        by = {}
        for r in sim["build_batch"](start + timedelta(minutes=k), active or {}, 5, None, machine_ids)["readings"]:
            by.setdefault(r["machine_id"], []).append(r)
        for mid, rows in by.items():
            means = score.batch_means(rows)
            if not means or mid not in sim["base"]:
                continue
            pm = prev.setdefault(mid, [])
            res = score.compute(means, sim["base"][mid], None, pm[-2:])
            pm.append(means)
            out.setdefault(mid, []).append((k, res["score"], res["deviated_vars"]))
    return out


def first_crossing(rows, threshold=2.0):
    """(indice entre los lotes puntuados, minuto, deviated_vars) del primer lote >= threshold, o None."""
    return next(((i, k, dv) for i, (k, s, dv) in enumerate(rows) if s >= threshold), None)


def test_seed_covers_every_machine(sim):
    assert sim["insufficient"] == [] and len(sim["base"]) == 100


def test_no_false_alarms_in_two_hours_without_drift(sim):
    res = replay(sim, T0, NO_DRIFT_MIN)
    peak = {m: max(s for _, s, _ in rows) for m, rows in res.items()}
    alarms = sorted(m for m, s in peak.items() if s >= 2.0)
    print(f"\n[calibration] no-drift {NO_DRIFT_MIN} min: {len(res)} machines scored, false alarms >= 2.0: "
          f"{len(alarms)} {alarms}, >= 1.2: {sum(s >= 1.2 for s in peak.values())}, max {max(peak.values()):.2f}")
    assert len(res) == 100 and alarms == []


def test_at_most_one_false_alarm_per_day(sim):
    """Objetivo del documento de diseno: <= 1 falsa alarma/dia/100 maquinas (lotes >= 2.0 en 24 h sin deriva)."""
    res = replay(sim, T0, 24 * 60)
    alarms = [(m, k, round(s, 2)) for m, rows in res.items() for k, s, _ in rows if s >= 2.0]
    peak = max(s for rows in res.values() for _, s, _ in rows)
    print(f"\n[calibration] no-drift 24 h: false-alarm batches >= 2.0: {len(alarms)} {alarms}, max {peak:.2f}")
    assert len(alarms) <= 1


def test_s1_reaches_schedule_fast_as_hydraulic_pump(sim):
    from scoring import failure_mode
    seen = []
    for off in S1_OFFSETS:
        start = T0 + timedelta(minutes=off)
        rows = replay(sim, start, 40, {"S1": start}, {"EX-017"})["EX-017"]
        hit = first_crossing(rows)
        assert hit is not None, f"S1 desde +{off} min no cruzo schedule"
        i, minute, dv = hit
        seen.append((off, minute, i + 1, failure_mode.infer(dv)))
    print("\n[calibration] S1 (start offset, minutes to schedule, operating batches, mode): " + str(seen))
    assert all(mode == "hydraulic_pump" for *_, mode in seen)
    # EX-017 queda en ralenti bloques enteros: cuenta el tiempo operando, no el reloj
    assert all(n <= S1_MAX_OPERATING_BATCHES for _, _, n, _ in seen)
    reviewer = dict((off, minute) for off, minute, _, _ in seen)[20]  # inicio del replay del review final
    assert reviewer <= 15


def test_s2_detected_on_all_four_machines_as_fuel_contamination(sim):
    from scoring import failure_mode
    start = T0 + timedelta(minutes=S2_START_MIN)
    res = replay(sim, start, S2_MAX_MIN, {"S2": start}, set(sim["S2_MACHINES"]))
    found = {}
    for mid in sim["S2_MACHINES"]:
        hit = first_crossing(res.get(mid, []))
        found[mid] = (hit[1], failure_mode.infer(hit[2])) if hit else None
    print(f"\n[calibration] S2 (machine: minutes to schedule, mode): {found}")
    assert all(v is not None and v[1] == "fuel_contamination" for v in found.values()), found
