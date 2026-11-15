"""seed-baselines (T47, backlog #51): agro e industria tambien tienen lecturas operando y linea base."""
import importlib.util
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "simulator"))
sys.path.insert(0, os.path.join(ROOT, "aws", "scoring"))
spec = importlib.util.spec_from_file_location("seed_baselines", os.path.join(ROOT, "scripts", "seed-baselines.py"))
seed_baselines = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_baselines)

END = datetime(2026, 11, 15, 20, 0, tzinfo=timezone.utc)  # 15:00 Lima, turno pleno


def test_every_park_gets_a_baseline():
    ids = {"EX-017", "TA-001", "CS-001", "MC-001", "CP-001", "GE-001"}  # mineria, agro e industria
    out = seed_baselines.machine_baselines(END, hours=6, step_min=5, machine_ids=ids)
    assert set(out) == ids
    for machine_id, (baseline, n) in out.items():
        assert n > 0 and baseline is not None, machine_id
