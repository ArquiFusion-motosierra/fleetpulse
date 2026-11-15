"""Logica pura de /recommend-checks (T33): prompt para Gemini, validacion del JSON y plantilla de respaldo.

Entrada (la arma aws/reports/lib/recommend.py): report_id, machine_id, model, abnormality_type, description,
severity_perceived, telemetry_snapshot{means, fault_codes, hours_total, hours_since_service},
oem_specs{ranges, service_intervals, typical_failures} y open_incident{incident_id, failure_mode, severity} opcional.
Salida: probable_causes[{cause, component, confidence}], checks[{order, step, tool, expected, if_fails}],
urgency (observe|schedule|immediate), suggested_parts[PRT-##] (solo del BOM del modelo), safety_notes[], source.

Guardas deterministicas que aplican a Gemini y a la plantilla: repuestos filtrados al BOM, y una urgencia minima
(`urgency_floor`) por severidad percibida alta, medias fuera del rango del fabricante, codigos de falla o un
incidente abierto. Sin red ni credenciales aqui.
"""
from __future__ import annotations

URGENCIES = ("observe", "schedule", "immediate")
_RANK = {u: i for i, u in enumerate(URGENCIES)}
MAX_CAUSES, MAX_CHECKS, MAX_NOTES = 5, 10, 5
CAP = {"cause": 200, "component": 60, "step": 300, "tool": 100, "expected": 200, "if_fails": 200, "note": 200}

# Repuestos por modelo y modo de falla: copia de `bom` en contracts/ids.yaml (la imagen solo lleva app/).
# tests/test_recommend.py verifica que coincide con el contrato.
BOM: dict[str, dict[str, list[str]]] = {
    "EX": {"hydraulic_pump": ["PRT-04", "PRT-03"], "hydraulic_contamination": ["PRT-02"],
           "hydraulic_overheat": ["PRT-10"], "fuel_contamination": ["PRT-01", "PRT-05"],
           "engine_overheat": ["PRT-11"], "sensor_fault": ["PRT-09"], "head_gasket": ["PRT-13"]},
    "CF": {"hydraulic_contamination": ["PRT-02"], "hydraulic_pump": ["PRT-03"], "fuel_contamination": ["PRT-01"],
           "engine_overheat": ["PRT-11"], "brake_wear": ["PRT-12"], "electrical_fault": ["PRT-08"],
           "head_gasket": ["PRT-13"]},
    "VQ": {"fuel_contamination": ["PRT-01", "PRT-05"], "engine_overheat": ["PRT-06", "PRT-11"],
           "brake_wear": ["PRT-12"], "final_drive": ["PRT-07"], "electrical_fault": ["PRT-08"],
           "head_gasket": ["PRT-13"]},
    "MN": {"fuel_contamination": ["PRT-01"], "engine_overheat": ["PRT-11"], "hydraulic_contamination": ["PRT-02"],
           "electrical_fault": ["PRT-08"], "head_gasket": ["PRT-13"]},
    "TO": {"fuel_contamination": ["PRT-01", "PRT-05"], "final_drive": ["PRT-07"],
           "hydraulic_contamination": ["PRT-02"], "engine_overheat": ["PRT-06"], "head_gasket": ["PRT-13"]},
}

# Variable de telemetria que mira cada tipo de anormalidad (para citar el rango del fabricante en el chequeo).
TYPE_VAR = {"high_engine_temp": "coolant_temp_c", "high_hydraulic_temp": "hydraulic_oil_temp_c",
            "abnormal_vibration": "vibration_mm_s", "low_hydraulic_pressure": "hydraulic_pressure_bar",
            "power_loss": "fuel_rate_lph"}


def _c(step, tool, expected, if_fails):
    return {"step": step, "tool": tool, "expected": expected, "if_fails": if_fails}


# Plantilla por tipo de anormalidad (semilla de contracts/report.schema.json abnormality_type_seed). `modes` son los
# modos de falla de ids.yaml de donde salen los repuestos sugeridos; componentes en ingles, como en aws/reports.
CATALOG: dict[str, dict] = {
    "high_engine_temp": {
        "urgency": "immediate", "modes": ["head_gasket", "engine_overheat"],
        "causes": [("Junta de culata con fuga", "head_gasket", 0.4), ("Termostato pegado", "thermostat", 0.3),
                   ("Bomba de agua con falla", "water_pump", 0.2)],
        "checks": [
            _c("Verificar nivel y color del refrigerante con el motor frio", "visual",
               "nivel en la marca, sin aceite ni espuma", "sospechar junta de culata; no seguir operando"),
            _c("Prueba de presion del sistema de refrigeracion", "bomba manual 1,2 bar", "mantiene la presion 10 min",
               "fuga interna o externa: revisar junta de culata y mangueras"),
            _c("Verificar apertura del termostato y caudal de la bomba de agua", "termometro infrarrojo",
               "manguera superior dentro del rango del fabricante", "cambiar termostato o bomba de agua"),
        ],
        "safety": ["No abrir la tapa del radiador con el motor caliente"],
    },
    "high_hydraulic_temp": {
        "urgency": "schedule", "modes": ["hydraulic_overheat", "hydraulic_contamination"],
        "causes": [("Enfriador de aceite obstruido", "oil_cooler", 0.5),
                   ("Aceite hidraulico bajo o degradado", "hydraulic_oil", 0.3),
                   ("Valvula de alivio abierta", "relief_valve", 0.2)],
        "checks": [
            _c("Limpiar e inspeccionar el enfriador de aceite hidraulico", "visual / aire comprimido",
               "panal limpio y ventilador girando", "limpiar o reemplazar el enfriador"),
            _c("Verificar nivel y estado del aceite hidraulico", "varilla / visual", "nivel en la marca, aceite claro",
               "completar o cambiar el aceite hidraulico"),
            _c("Medir temperatura de entrada y salida del enfriador", "termometro infrarrojo", "caida de 8 a 12 C",
               "enfriador obstruido por dentro"),
        ],
        "safety": ["Despresurizar el circuito antes de abrir conexiones"],
    },
    "abnormal_vibration": {
        "urgency": "schedule", "modes": ["final_drive"],
        "causes": [("Rodamiento del mando final gastado", "final_drive_bearing", 0.45),
                   ("Desbalance o pernos flojos", "unbalance", 0.3), ("Soporte de motor danado", "engine_mount", 0.25)],
        "checks": [
            _c("Inspeccionar pernos y soportes de motor y bastidor", "llave de torque", "torque segun manual, sin fisuras",
               "reapretar o cambiar soportes"),
            _c("Medir vibracion en el mando final y comparar con el limite del fabricante", "vibrometro",
               "por debajo del limite de advertencia", "sospechar rodamiento del mando final"),
            _c("Revisar el aceite del mando final por particulas metalicas", "tapon magnetico", "sin limalla",
               "desmontar y cambiar el rodamiento"),
        ],
        "safety": ["Bloquear la maquina antes de trabajar debajo del bastidor"],
    },
    "low_hydraulic_pressure": {
        "urgency": "schedule", "modes": ["hydraulic_pump", "hydraulic_contamination"],
        "causes": [("Bomba hidraulica gastada", "hydraulic_pump", 0.5),
                   ("Sellos de cilindro con fuga interna", "cylinder_seals", 0.3),
                   ("Filtro hidraulico obstruido", "hydraulic_filter", 0.2)],
        "checks": [
            _c("Medir la presion de la bomba en la toma de prueba", "manometro 0-400 bar",
               "dentro del rango del fabricante", "medir caudal; sospechar bomba hidraulica"),
            _c("Inspeccionar cilindros y sellos por fugas o deriva", "visual", "sin fugas ni deriva de cilindros",
               "cambiar kit de sellos"),
            _c("Revisar el indicador de restriccion del filtro hidraulico", "visual", "indicador en verde",
               "cambiar filtro hidraulico"),
        ],
        "safety": ["Bajar implementos al suelo y despresurizar antes de intervenir"],
    },
    "smoke": {
        "urgency": "immediate", "modes": ["fuel_contamination", "engine_overheat"],
        "causes": [("Inyectores con falla", "injector", 0.45), ("Turbocompresor con fuga de aceite", "turbo", 0.3),
                   ("Filtro de aire obstruido", "air_filter", 0.25)],
        "checks": [
            _c("Identificar el color del humo", "visual", "sin humo visible con carga normal",
               "negro: aire o inyeccion; azul: aceite o turbo; blanco: refrigerante"),
            _c("Revisar filtro de aire e indicador de restriccion", "visual", "filtro limpio", "cambiar filtro de aire"),
            _c("Revisar juego del eje del turbo y fugas de aceite", "visual / manual", "sin juego radial ni aceite",
               "cambiar turbocompresor"),
        ],
        "safety": ["Si hay olor a quemado o llama, detener el motor y usar el extintor"],
    },
    "leak": {
        "urgency": "schedule", "modes": ["hydraulic_contamination", "hydraulic_pump"],
        "causes": [("Sellos de cilindro danados", "seals", 0.5), ("Manguera fisurada", "hose", 0.4),
                   ("Conexion floja", "fittings", 0.1)],
        "checks": [
            _c("Limpiar la zona e identificar el fluido (aceite, refrigerante, combustible)", "visual",
               "sin goteo tras 10 min en ralenti", "ubicar la fuente de la fuga"),
            _c("Inspeccionar mangueras y conexiones hidraulicas", "visual / carton bajo la maquina",
               "sin fisuras ni conexiones flojas", "cambiar manguera o reapretar"),
            _c("Inspeccionar sellos de cilindros", "visual", "vastagos secos", "cambiar kit de sellos"),
        ],
        "safety": ["No buscar fugas hidraulicas con la mano: usar un carton"],
    },
    "noise": {
        "urgency": "observe", "modes": ["electrical_fault", "final_drive"],
        "causes": [("Correa del alternador floja o gastada", "belt", 0.4), ("Rodamiento con desgaste", "bearing", 0.4),
                   ("Escape suelto", "exhaust", 0.2)],
        "checks": [
            _c("Ubicar el origen del ruido con el motor en ralenti", "estetoscopio mecanico",
               "sin golpeteo ni chirrido", "registrar la zona del ruido"),
            _c("Revisar tension y estado de la correa del alternador", "visual / tensiometro",
               "tension segun manual, sin grietas", "cambiar la correa"),
            _c("Revisar rodamientos accesibles por juego y temperatura", "termometro infrarrojo",
               "sin juego ni sobretemperatura", "programar el cambio del rodamiento"),
        ],
        "safety": ["Mantener manos y ropa lejos de correas y poleas en movimiento"],
    },
    "warning_light": {
        "urgency": "schedule", "modes": ["sensor_fault", "electrical_fault"],
        "causes": [("Sensor defectuoso o desconectado", "sensor", 0.5), ("Falla electrica", "electrical", 0.3),
                   ("Condicion real fuera de rango", "operating_condition", 0.2)],
        "checks": [
            _c("Leer los codigos activos (SPN/FMI) en el tablero o con el escaner", "escaner J1939",
               "sin codigos activos", "seguir el procedimiento del SPN en el manual"),
            _c("Verificar conectores y cableado del sensor indicado", "multimetro", "continuidad y voltaje correctos",
               "reparar cableado o cambiar el sensor"),
            _c("Confirmar la lectura con un instrumento externo", "manometro / termometro", "coincide con el tablero",
               "sensor defectuoso"),
        ],
        "safety": ["Desconectar la bateria antes de manipular conectores"],
    },
    "power_loss": {
        "urgency": "schedule", "modes": ["fuel_contamination", "engine_overheat"],
        "causes": [("Filtro de combustible obstruido o con agua", "fuel_filter", 0.45),
                   ("Turbocompresor con baja presion", "turbo", 0.3), ("Filtro de aire obstruido", "air_filter", 0.25)],
        "checks": [
            _c("Revisar filtro de combustible y separador de agua", "visual", "sin agua ni sedimento",
               "cambiar filtro de combustible"),
            _c("Revisar filtro de aire e indicador de restriccion", "visual", "indicador en verde",
               "cambiar filtro de aire"),
            _c("Verificar la presion de sobrealimentacion del turbo", "manometro", "segun manual",
               "revisar el turbocompresor"),
        ],
        "safety": ["Trabajar con el motor detenido y frio"],
    },
    "other": {
        "urgency": "observe", "modes": [],
        "causes": [("Causa no determinada: requiere inspeccion", "general_inspection", 0.3)],
        "checks": [
            _c("Inspeccion visual general (fugas, pernos, mangueras, cableado)", "visual", "sin hallazgos",
               "documentar y fotografiar"),
            _c("Leer los codigos activos en el tablero", "escaner J1939", "sin codigos activos",
               "seguir el procedimiento del SPN en el manual"),
            _c("Comparar la telemetria reciente con los rangos del fabricante", "consola FleetPulse", "dentro de rango",
               "escalar al supervisor"),
        ],
        "safety": [],
    },
}


def model_parts(model: str) -> set[str]:
    return {p for parts in BOM.get(model or "", {}).values() for p in parts}


def out_of_range(req: dict) -> list[dict]:
    """Medias de la telemetria fuera del rango normal del fabricante: [{var, value, normal}]."""
    means = (req.get("telemetry_snapshot") or {}).get("means") or {}
    ranges = (req.get("oem_specs") or {}).get("ranges") or {}
    out = []
    for var, rng in ranges.items():
        value = means.get(var)
        if not isinstance(value, (int, float)) or not isinstance(rng, (list, tuple)) or len(rng) != 2:
            continue
        lo, hi = rng
        if (isinstance(lo, (int, float)) and value < lo) or (isinstance(hi, (int, float)) and value > hi):
            out.append({"var": var, "value": value, "normal": [lo, hi]})
    return out


def urgency_floor(req: dict) -> str:
    """Urgencia minima por reglas: un incidente abierto inmediato -> immediate; severidad percibida alta, medias
    fuera de rango, codigos de falla o un incidente abierto -> schedule; si no, observe."""
    inc = req.get("open_incident") or {}
    if inc.get("severity") == "immediate":
        return "immediate"
    snap = req.get("telemetry_snapshot") or {}
    if req.get("severity_perceived") == "high" or out_of_range(req) or snap.get("fault_codes") or inc:
        return "schedule"
    return "observe"


def _max_urgency(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


def build_prompt(req: dict) -> str:
    """Prompt para Gemini: observacion del operario + telemetria + ficha del fabricante. Pide el JSON de salida."""
    forma = (
        '{"probable_causes": [{"cause": string, "component": string, "confidence": number 0..1}], '
        '"checks": [{"order": int, "step": string, "tool": string, "expected": string, "if_fails": string}], '
        '"urgency": "observe"|"schedule"|"immediate", "suggested_parts": [string PRT-##], "safety_notes": [string]}'
    )
    snap = req.get("telemetry_snapshot") or {}
    oem = req.get("oem_specs") or {}
    tpl = CATALOG.get(req.get("abnormality_type", ""), CATALOG["other"])
    return (
        "Eres un ingeniero de mantenimiento preventivo de maquinaria pesada minera. Un operario reporto una "
        "anormalidad. Cruza su observacion con la telemetria y la ficha del fabricante y recomienda al tecnico "
        "asignado los chequeos a realizar, en orden. Responde SOLO con un objeto JSON valido con esta forma:\n"
        f"{forma}\n\n"
        "Reglas: en espanol; 2 a 4 causas probables (component en ingles snake_case, ej. head_gasket); 3 a 6 "
        "chequeos concretos con herramienta, valor esperado (cita el rango del fabricante si aplica) y que hacer si "
        "falla; urgency immediate si hay riesgo de dano mayor o de seguridad; suggested_parts solo de esta lista: "
        f"{sorted(model_parts(req.get('model')))}; safety_notes breves.\n\n"
        f"maquina: {req.get('machine_id')} modelo: {req.get('model')}\n"
        f"tipo de anormalidad: {req.get('abnormality_type')}\n"
        f"descripcion del operario: {req.get('description')}\n"
        f"severidad percibida: {req.get('severity_perceived')}\n"
        f"medias recientes: {snap.get('means')}\n"
        f"fuera del rango del fabricante: {out_of_range(req) or 'ninguna'}\n"
        f"codigos de falla: {snap.get('fault_codes')}\n"
        f"horas totales: {snap.get('hours_total')} desde el ultimo servicio: {snap.get('hours_since_service')}\n"
        f"rangos normales del fabricante: {oem.get('ranges')}\n"
        f"intervalos de servicio: {oem.get('service_intervals')}\n"
        f"fallas tipicas del modelo: {oem.get('typical_failures')}\n"
        f"incidente abierto: {req.get('open_incident')}\n"
        f"pista (plantilla local): causas {[c[0] for c in tpl['causes']]}\n"
    )


def _text(v, cap: int) -> str:
    return v.strip()[:cap] if isinstance(v, str) else ""


def validate_recommendation(obj: object, req: dict) -> dict | None:
    """Normaliza el JSON de Gemini (topes, repuestos del BOM, urgencia minima) o None si no sirve."""
    if not isinstance(obj, dict):
        return None
    causes_in, checks_in = obj.get("probable_causes"), obj.get("checks")
    if not isinstance(causes_in, list) or not isinstance(checks_in, list) or obj.get("urgency") not in URGENCIES:
        return None
    causes = []
    for c in causes_in[:MAX_CAUSES]:
        if isinstance(c, dict) and _text(c.get("cause"), CAP["cause"]):
            conf = c.get("confidence")
            ok = isinstance(conf, (int, float)) and not isinstance(conf, bool)
            causes.append({"cause": _text(c["cause"], CAP["cause"]),
                           "component": _text(c.get("component"), CAP["component"]),
                           "confidence": min(max(float(conf), 0.0), 1.0) if ok else None})
    checks = []
    for i, c in enumerate(checks_in, 1):
        if isinstance(c, dict) and _text(c.get("step"), CAP["step"]):
            order = c.get("order")
            checks.append({"order": order if isinstance(order, int) and not isinstance(order, bool) else i,
                           **{k: _text(c.get(k), CAP[k]) for k in ("step", "tool", "expected", "if_fails")}})
    if not causes or not checks:
        return None
    checks = sorted(checks, key=lambda c: c["order"])[:MAX_CHECKS]
    bom = model_parts(req.get("model"))
    parts = obj.get("suggested_parts") if isinstance(obj.get("suggested_parts"), list) else []
    notes = obj.get("safety_notes") if isinstance(obj.get("safety_notes"), list) else []
    return {
        "probable_causes": causes,
        "checks": checks,
        "urgency": _max_urgency(obj["urgency"], urgency_floor(req)),
        "suggested_parts": [p for p in dict.fromkeys(p for p in parts if isinstance(p, str)) if p in bom],
        "safety_notes": [t for t in (_text(n, CAP["note"]) for n in notes[:MAX_NOTES]) if t],
        "source": "gemini",
    }


def fallback_recommendation(req: dict) -> dict:
    """Plantilla deterministica por abnormality_type (tipo desconocido -> `other`). Cita el rango del fabricante en
    el chequeo de la variable del tipo y agrega un chequeo por cada media fuera de rango."""
    kind = req.get("abnormality_type", "")
    tpl = CATALOG.get(kind, CATALOG["other"])
    model = req.get("model")
    checks = [dict(c) for c in tpl["checks"]]
    ranges = (req.get("oem_specs") or {}).get("ranges") or {}
    var = TYPE_VAR.get(kind)
    rng = ranges.get(var)
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        lo, hi = rng
        checks[0]["expected"] = f"{checks[0]['expected']} (fabricante: {var} normal {lo}-{hi})"[:CAP["expected"]]
    for dev in out_of_range(req):
        if dev["var"] == var:
            continue
        lo, hi = dev["normal"]
        checks.append(_c(f"Revisar {dev['var']}: media reciente {dev['value']} fuera del rango del fabricante",
                         "consola FleetPulse / instrumento", f"entre {lo} y {hi}", "escalar al supervisor"))
    parts = [p for mode in tpl["modes"] for p in BOM.get(model or "", {}).get(mode, [])]
    return {
        "probable_causes": [{"cause": c, "component": comp, "confidence": conf} for c, comp, conf in tpl["causes"]],
        "checks": [{"order": i, **c} for i, c in enumerate(checks[:MAX_CHECKS], 1)],
        "urgency": _max_urgency(tpl["urgency"], urgency_floor(req)),
        "suggested_parts": list(dict.fromkeys(parts)),
        "safety_notes": list(tpl["safety"]),
        "source": "template",
    }
