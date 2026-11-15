"""Ficha tecnica del fabricante (contracts/oem_specs.yaml, vendorizada como oem.json). Valores ilustrativos."""
from __future__ import annotations

import json
import os
from functools import lru_cache

OEM_JSON = os.path.join(os.path.dirname(__file__), "oem.json")


@lru_cache(maxsize=1)
def specs() -> dict:
    with open(OEM_JSON, encoding="utf-8") as f:
        return json.load(f)


def model(model_id: str) -> dict:
    return specs()["models"][model_id]


def harmonics_hz(rpm: float) -> dict[str, float]:
    """1x, 2x y 3x de la velocidad del motor (rpm/60), en Hz."""
    f1 = rpm / 60.0
    return {"1x": f1, "2x": 2 * f1, "3x": 3 * f1}


def resonance(model_id: str, rpm: float) -> dict | None:
    """Primer armonico (1x..3x) que cae dentro de la tolerancia de una frecuencia natural del modelo."""
    tol = specs()["resonance_tolerance_pct"] / 100.0
    for name, hz in harmonics_hz(rpm).items():
        for nf in model(model_id)["vibration"]["natural_frequencies"]:
            if abs(hz - nf["hz"]) <= tol * nf["hz"]:
                return {"harmonic": name, "frequency_hz": round(hz, 1), "natural_frequency_hz": nf["hz"],
                        "component": nf["component"]}
    return None
