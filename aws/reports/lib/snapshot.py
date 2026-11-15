"""Resumen de telemetria reciente de una maquina para el reporte del operario y para `/recommend-checks`.

`build` lee `MACHINE#<id>` / `META` y las ultimas `WINDOW` lecturas `R#` de fp-state: medias de las seis variables del
score (solo lecturas con motor encendido, como aws/scoring; si todas estan apagadas, todas), codigos de falla
distintos de la ventana (el mas reciente primero), horas totales y desde el ultimo servicio, modelo, zona y severidad
actual; mas `oem`, el extracto de la ficha del fabricante (`fplib.ids.oem_specs`, T26) en la forma que espera T33:
`ranges` {var: [min, max] normal}, `service_intervals` [{every_h, task, parts[]}] y `typical_failures` [failure_mode].
Sin ficha se usa el intervalo de servicio y los modos de falla del BOM de ids.yaml.
Idempotencia: solo lee. Las funciones `means`, `fault_codes` y `oem_excerpt` son puras.
"""
from __future__ import annotations

from boto3.dynamodb.conditions import Key

from fplib import ids

VARS = ("coolant_temp_c", "hydraulic_oil_temp_c", "hydraulic_pressure_bar", "vibration_mm_s", "engine_rpm",
        "fuel_rate_lph")
WINDOW = 30
MAX_FAULTS = 10


def _num(x):
    return None if x is None else float(x)


def means(readings: list[dict]) -> dict[str, float]:
    """Media de cada variable sobre las lecturas con motor encendido (`engine_status != off`); si no hay ninguna,
    sobre todas."""
    on = [r for r in readings if r.get("engine_status") != "off"] or readings
    out = {}
    for v in VARS:
        vals = [float(r[v]) for r in on if r.get(v) is not None]
        if vals:
            out[v] = round(sum(vals) / len(vals), 2)
    return out


def fault_codes(readings: list[dict]) -> list[dict]:
    """Codigos (spn, fmi) distintos de `readings` (el mas reciente primero), con su severidad."""
    seen, out = set(), []
    for r in sorted(readings, key=lambda r: r.get("ts", ""), reverse=True):
        for fc in r.get("fault_codes") or []:
            key = (int(fc["spn"]), int(fc["fmi"]))
            if key in seen:
                continue
            seen.add(key)
            out.append({"spn": key[0], "fmi": key[1], "severity": fc.get("severity")})
    return out[:MAX_FAULTS]


def oem_excerpt(model: str) -> dict:
    """Extracto de la ficha del fabricante del modelo; claves solo cuando hay datos."""
    spec = ids.oem_specs(model)
    if spec:
        ranges = spec.get("ranges") or {var: lim["normal"] for var, lim in (spec.get("limits") or {}).items()
                                        if isinstance(lim, dict) and "normal" in lim}
        intervals = [{"every_h": s["every_h"], "task": s.get("task", ""),
                      "parts": [p["part_id"] if isinstance(p, dict) else p for p in s.get("parts") or []]}
                     for s in spec.get("service_schedule") or spec.get("service_intervals") or []]
        failures = [f["failure_mode"] if isinstance(f, dict) else f for f in spec.get("typical_failures") or []]
    else:  # sin T26: intervalo de servicio del modelo y modos de falla de su BOM (ids.yaml)
        cat = ids.load()
        every = next((m["service_interval_h"] for m in cat.get("models", []) if m["model_id"] == model), None)
        ranges = {}
        intervals = [{"every_h": every, "task": "Servicio preventivo", "parts": []}] if every else []
        failures = list(dict.fromkeys(b["failure_mode"] for b in cat["bom"] if b["model_id"] == model))
    out = {"ranges": ranges, "service_intervals": intervals, "typical_failures": failures}
    return {k: v for k, v in out.items() if v}


def _hours(meta: dict, catalog: dict) -> tuple[float | None, float | None]:
    """Horas totales (META, si no ids.yaml) y desde el servicio: ids.yaml + horas acumuladas desde el seed."""
    total = _num(meta.get("hours_total"))
    base_total, base_since = _num(catalog.get("hours_total")), _num(catalog.get("hours_since_service"))
    since = _num(meta.get("hours_since_service"))
    if since is None and base_since is not None:
        since = base_since + max(0.0, (total or base_total or 0.0) - (base_total or 0.0))
    total = total if total is not None else base_total
    return (None if total is None else round(total, 1)), (None if since is None else round(since, 1))


def build(state_table, machine_id: str) -> dict:
    meta = state_table.get_item(Key={"PK": f"MACHINE#{machine_id}", "SK": "META"}).get("Item") or {}
    rows = state_table.query(
        KeyConditionExpression=Key("PK").eq(f"MACHINE#{machine_id}") & Key("SK").begins_with("R#"),
        ScanIndexForward=False, Limit=WINDOW)["Items"]
    known = ids.machines().get(machine_id, {})
    model = meta.get("model") or known.get("model_id") or machine_id.split("-", 1)[0]
    hours_total, hours_since = _hours(meta, known)
    snap = {
        "means": means(rows),
        "fault_codes": fault_codes(rows),
        "hours_total": hours_total,
        "hours_since_service": hours_since,
        "model": model,
        "zone_id": meta.get("zone_id") or known.get("zone_id"),
        "status": meta.get("status") or "ok",
        "n_readings": len(rows),
        "last_ts": max((r["ts"] for r in rows if r.get("ts")), default=None),
        "oem": oem_excerpt(model),
    }
    return snap
