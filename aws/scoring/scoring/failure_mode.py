"""Modo de falla probable a partir de las variables desviadas. Funcion pura y determinista."""
from __future__ import annotations


def infer(deviated_vars: list[dict]) -> str:
    z = {d["var"]: d["z"] for d in deviated_vars}
    up = {v for v, x in z.items() if x > 0}
    if z.get("hydraulic_pressure_bar", 0) < 0 and ({"hydraulic_oil_temp_c", "vibration_mm_s"} & up):
        return "hydraulic_pump"
    if "fuel_rate_lph" in up and ("engine_rpm" in z or "coolant_temp_c" in up):
        return "fuel_contamination"
    if len(z) == 1:
        only = next(iter(up), None)
        return {"coolant_temp_c": "engine_overheat", "hydraulic_oil_temp_c": "hydraulic_overheat",
                "vibration_mm_s": "final_drive"}.get(only, "sensor_fault")
    return "sensor_fault"
