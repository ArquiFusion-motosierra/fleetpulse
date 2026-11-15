"""Informe tecnico de anormalidad de /explain (T28): las variables desviadas contra la ficha del fabricante.

Deterministico (sin red): `build_report(req)` arma `explanation.report` (contracts/incident.schema.json $defs/report)
con los hallazgos, la resonancia (armonicos 1x..3x de las rpm contra las frecuencias naturales del modelo), la falla
tipica de la ficha, la accion y los repuestos del BOM. Gemini solo puede reescribir los textos (`merge_texts`); los
numeros, el modo de falla, la resonancia y los repuestos salen siempre de la ficha.
Datos: app/specs.json (copia de contracts/oem_specs.yaml y del BOM de ids.yaml; ver sync_specs.py).
"""
from __future__ import annotations

import json
import os
from functools import lru_cache

SPECS_JSON = os.path.join(os.path.dirname(__file__), "specs.json")
UNITS = {"coolant_temp_c": "C", "hydraulic_oil_temp_c": "C", "hydraulic_pressure_bar": "bar",
         "vibration_mm_s": "mm/s", "engine_rpm": "rpm", "fuel_rate_lph": "L/h", "speed_kmh": "km/h"}
LABELS = {"coolant_temp_c": "refrigerante", "hydraulic_oil_temp_c": "aceite hidraulico",
          "hydraulic_pressure_bar": "presion hidraulica", "vibration_mm_s": "vibracion", "engine_rpm": "rpm",
          "fuel_rate_lph": "consumo", "speed_kmh": "velocidad"}
MAX_FINDINGS = 8
CAP = {"summary": 600, "oem_limit": 120, "note": 300, "probable_cause": 400, "recommended_action": 400,
       "component": 80, "oem_reference": 160}


@lru_cache(maxsize=1)
def specs() -> dict:
    with open(SPECS_JSON, encoding="utf-8") as f:
        return json.load(f)


def model_spec(model: str | None) -> dict | None:
    return specs()["oem"]["models"].get(model or "")


def _fmt(x) -> str:
    return f"{x:g}" if isinstance(x, (int, float)) else str(x)


def limit_text(var: str, spec: dict) -> str:
    """Ej. 'normal 280-345 bar; advertencia bajo 270 bar; critico bajo 250 bar'."""
    unit = UNITS.get(var, "")
    if var == "engine_rpm":
        return f"nominal {_fmt(spec['rated_rpm'])} rpm; maximo {_fmt(spec['max_rpm'])} rpm"
    lim = (spec.get("limits") or {}).get(var)
    if not lim:
        return "sin limite en la ficha"
    lo, hi = lim["normal"]
    parts = [f"normal {_fmt(lo)}-{_fmt(hi)} {unit}"]
    if "low_warning" in lim:
        parts += [f"advertencia bajo {_fmt(lim['low_warning'])} {unit}", f"critico bajo {_fmt(lim['low_critical'])} {unit}"]
    else:
        parts += [f"advertencia {_fmt(lim['warning'])} {unit}", f"critico {_fmt(lim['critical'])} {unit}"]
    return "; ".join(parts)[:CAP["oem_limit"]]


def status(var: str, value: float, spec: dict) -> tuple[str, str | None]:
    """(normal|warning|critical, nota) del valor contra los umbrales del fabricante."""
    if var == "engine_rpm":
        rated, top = spec["rated_rpm"], spec["max_rpm"]
        pct = round(100 * (value - rated) / rated)
        if value >= top:
            return "critical", f"Sobre el maximo del fabricante ({pct:+d} % de la nominal)"
        if abs(pct) >= 5:
            return "warning", f"{pct:+d} % respecto de la nominal"
        return "normal", None
    lim = (spec.get("limits") or {}).get(var)
    if not lim:
        return "normal", None
    lo, hi = lim["normal"]
    if "low_warning" in lim:
        if value <= lim["low_critical"]:
            return "critical", None
        if value <= lim["low_warning"]:
            return "warning", None
    else:
        if value >= lim["critical"]:
            return "critical", None
        if value >= lim["warning"]:
            return "warning", None
    if value < lo:
        return "normal", "Bajo el rango normal, sin llegar al umbral de advertencia"
    if value > hi:
        return "normal", "Sobre el rango normal, sin llegar al umbral de advertencia"
    return "normal", None


def harmonics_hz(rpm: float) -> dict[str, float]:
    f1 = rpm / 60.0
    return {"1x": f1, "2x": 2 * f1, "3x": 3 * f1}


def resonance(model: str | None, rpm: float | None) -> dict | None:
    """Primer armonico (1x..3x) dentro de la tolerancia de una frecuencia natural del modelo (como el simulador)."""
    spec = model_spec(model)
    if spec is None or not isinstance(rpm, (int, float)) or rpm <= 0:
        return None
    tol = specs()["oem"]["resonance_tolerance_pct"] / 100.0
    for name, hz in harmonics_hz(rpm).items():
        for nf in spec["vibration"]["natural_frequencies"]:
            if abs(hz - nf["hz"]) <= tol * nf["hz"]:
                return {"harmonic": name, "frequency_hz": round(hz, 1), "natural_frequency_hz": nf["hz"],
                        "component": nf["component"][:CAP["component"]], "mode": nf["mode"]}
    return None


def _rpm(req: dict) -> float | None:
    for d in req.get("deviated_vars") or []:
        if isinstance(d, dict) and d.get("var") == "engine_rpm":
            return d.get("value")
    ctx = req.get("context") or {}
    return ctx.get("engine_rpm") if isinstance(ctx, dict) else None


def bom_parts(model: str | None, failure_mode: str | None) -> list[dict]:
    return [{"part_id": b["part_id"], "qty": b["qty"]} for b in specs()["bom"]
            if b["model_id"] == model and b["failure_mode"] == failure_mode]


def build_report(req: dict) -> dict | None:
    """Informe con la ficha del modelo; None si no hay ficha, modo de falla del contrato o variables que mostrar."""
    model, mode = req.get("model"), req.get("failure_mode")
    spec = model_spec(model)
    if spec is None or mode not in specs()["failure_modes"]:
        return None
    findings = []
    for d in req.get("deviated_vars") or []:
        if not isinstance(d, dict) or d.get("var") not in UNITS or not isinstance(d.get("value"), (int, float)):
            continue
        var, value = d["var"], float(d["value"])
        st, note = status(var, value, spec)
        f = {"parameter": var, "value": round(value, 2), "unit": UNITS[var], "oem_limit": limit_text(var, spec),
             "status": st}
        if note:
            f["note"] = note[:CAP["note"]]
        findings.append(f)
    res = resonance(model, _rpm(req))
    if res:
        findings.append({"parameter": "vibration_spectrum", "value": res["frequency_hz"], "unit": "Hz",
                         "oem_limit": f"{res['harmonic']} a menos de {_fmt(specs()['oem']['resonance_tolerance_pct'])}"
                                      f" % del modo {res['mode']} ({_fmt(res['natural_frequency_hz'])} Hz)",
                         "status": "critical", "note": f"Resonancia: {res['component']}"[:CAP["note"]]})
    if not findings:
        return None
    findings = findings[:MAX_FINDINGS]
    typical = next((t for t in spec.get("typical_failures") or [] if t.get("failure_mode") == mode), None)
    name = typical["name"] if typical else mode.replace("_", " ")
    out_txt = [f"{LABELS.get(f['parameter'], f['parameter'])} {_fmt(f['value'])} {f['unit']} ({f['status']})"
               for f in findings if f["parameter"] != "vibration_spectrum"]
    cause = f"{name}: patron de la ficha del fabricante ({', '.join(out_txt) or 'sin variables desviadas'})."
    summary = f"{req.get('machine_id', model)}: {', '.join(out_txt)}."
    if res:
        cause += (f" Las rpm llevan el {res['harmonic']} ({_fmt(res['frequency_hz'])} Hz) a la frecuencia natural de"
                  f" {res['component'].lower()} ({_fmt(res['natural_frequency_hz'])} Hz).")
        summary += f" Resonancia del {res['harmonic']} con {res['component'].lower()}."
    action = typical.get("action") if typical else None
    action = action or "Inspeccionar el componente segun el manual del fabricante y comparar con la ficha."
    if res:
        action = f"Bajar las rpm fuera de la banda de resonancia. {action}"
    used = [f["parameter"] for f in findings if f["parameter"] != "vibration_spectrum"]
    ref = f"Ficha {model} v{specs()['oem'].get('version', 1)} (ilustrativa): limites de {', '.join(used) or 'motor'}"
    if res:
        ref += f" y modo {res['mode']} ({res['component'].lower()})"
    return {
        "summary": summary[:CAP["summary"]],
        "findings": findings,
        "probable_cause": cause[:CAP["probable_cause"]],
        "failure_mode": mode,
        "resonance": {k: res[k] for k in ("harmonic", "frequency_hz", "natural_frequency_hz", "component")}
        if res else None,
        "recommended_action": action[:CAP["recommended_action"]],
        "parts": bom_parts(model, mode),
        "oem_reference": ref[:CAP["oem_reference"]],
    }


def merge_texts(report: dict, gemini_report) -> dict:
    """Toma de Gemini solo los textos (summary, probable_cause, recommended_action) si son cadenas no vacias."""
    if not isinstance(gemini_report, dict):
        return report
    out = dict(report)
    for k in ("summary", "probable_cause", "recommended_action"):
        v = gemini_report.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()[:CAP[k]]
    return out


def prompt_excerpt(req: dict) -> str:
    """Lineas para el prompt de /explain: ficha del modelo y el borrador del informe."""
    spec = model_spec(req.get("model"))
    if spec is None:
        return ""
    draft = build_report(req)
    return (
        f"ficha del fabricante ({spec.get('name')}): limites {spec.get('limits')}; rpm nominal {spec.get('rated_rpm')}"
        f"; frecuencias naturales {spec['vibration']['natural_frequencies']}; fallas tipicas "
        f"{[(t.get('failure_mode'), t.get('name'), t.get('action')) for t in spec.get('typical_failures') or []]}\n"
        f"borrador del informe tecnico (numeros de la ficha, no los cambies): {json.dumps(draft, ensure_ascii=False)}\n"
    )
