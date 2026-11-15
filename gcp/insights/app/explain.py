"""Logica pura de /explain: prompt para Gemini, validacion del JSON y plantilla de respaldo.

Sin red ni credenciales aqui: todo es deterministico para poder testearlo con falsos.
"""
from __future__ import annotations

ROLES = ("technician", "supervisor", "management", "operator")

# ASUNCION: componente probable y repuestos por modo de falla, derivados del BOM de contracts/ids.yaml.
# Es el respaldo cuando Gemini no responde; no pretende reemplazar el diagnostico del tecnico.
FAILURE_CATALOG: dict[str, dict] = {
    "hydraulic_pump": {"component": "Bomba hidraulica", "parts": ["PRT-04", "PRT-03"], "specialty": "hydraulics"},
    "hydraulic_contamination": {"component": "Filtro hidraulico", "parts": ["PRT-02"], "specialty": "hydraulics"},
    "hydraulic_overheat": {"component": "Circuito hidraulico (aceite)", "parts": ["PRT-10", "PRT-02"], "specialty": "hydraulics"},
    "fuel_contamination": {"component": "Filtro de combustible / inyector", "parts": ["PRT-01", "PRT-05"], "specialty": "engine"},
    "engine_overheat": {"component": "Sistema de enfriamiento / filtro de aire", "parts": ["PRT-11", "PRT-06"], "specialty": "engine"},
    "final_drive": {"component": "Rodamiento de mando final", "parts": ["PRT-07"], "specialty": "transmission"},
    "brake_wear": {"component": "Pastillas de freno", "parts": ["PRT-12"], "specialty": "transmission"},
    "electrical_fault": {"component": "Sistema electrico / correa del alternador", "parts": ["PRT-08"], "specialty": "electrical"},
    "sensor_fault": {"component": "Sensor de presion", "parts": ["PRT-09"], "specialty": "electrical"},
}

SEVERITY_LABEL = {"observe": "observar", "schedule": "programar", "immediate": "inmediata"}


def _deviated_text(deviated_vars: list) -> str:
    partes = []
    for d in deviated_vars or []:
        if not isinstance(d, dict):
            continue
        nombre = d.get("var", "?")
        valor = d.get("value")
        z = d.get("z")
        partes.append(f"{nombre}={valor} (z={z})")
    return ", ".join(partes) if partes else "sin variables desviadas"


def build_prompt(req: dict) -> str:
    """Arma el prompt para Gemini. Pide JSON con la forma exacta que espera la consola."""
    cat = FAILURE_CATALOG.get(req.get("failure_mode", ""), {})
    campos = (
        '{"hypotheses": [string], "probable_component": string, "suggested_parts": [string PRT-##], '
        '"confidence": number 0..1, "common_cause": string|null, '
        '"role_messages": {"technician": string, "supervisor": string, "management": string, "operator": string}}'
    )
    return (
        "Eres un ingeniero de mantenimiento de maquinaria pesada minera. "
        "Analiza el incidente y responde SOLO con un objeto JSON valido con esta forma:\n"
        f"{campos}\n\n"
        "Reglas: hypotheses son 2 a 4 causas probables en espanol; suggested_parts usa IDs PRT-## reales; "
        "common_cause solo si el incidente esta agrupado (group_id no nulo), si no, null; "
        "role_messages adapta el mensaje al rol (technician: tecnico; supervisor: jefe de zona; "
        "management: gerencia; operator: operador de la maquina), breve y accionable.\n\n"
        f"incident_id: {req.get('incident_id')}\n"
        f"machine_id: {req.get('machine_id')} modelo: {req.get('model')}\n"
        f"severidad: {req.get('severity')}\n"
        f"modo de falla detectado: {req.get('failure_mode')}\n"
        f"variables desviadas: {_deviated_text(req.get('deviated_vars'))}\n"
        f"group_id: {req.get('group_id')}\n"
        f"contexto: {req.get('context')}\n"
        f"incidentes recientes de esta maquina: {req.get('recent_incidents')}\n"
        f"pista de componente/repuestos tipicos para este modo: {cat.get('component')} {cat.get('parts')}\n"
    )


def fallback_response(req: dict) -> dict:
    """Plantilla deterministica si Gemini falla o devuelve JSON invalido. No usa red."""
    fm = req.get("failure_mode", "")
    cat = FAILURE_CATALOG.get(fm, {"component": "Componente por determinar", "parts": [], "specialty": "general"})
    machine = req.get("machine_id", "la maquina")
    sev = SEVERITY_LABEL.get(req.get("severity", ""), req.get("severity", "desconocida"))
    dev = _deviated_text(req.get("deviated_vars"))
    grouped = bool(req.get("group_id"))
    common = (
        f"Incidente agrupado ({req.get('group_id')}): varias maquinas comparten el patron, "
        "revisar causa comun (lote de combustible, condiciones de zona)."
        if grouped else None
    )
    return {
        "hypotheses": [
            f"Patron compatible con {fm or 'anomalia'} en {cat['component'].lower()}.",
            f"Variables desviadas: {dev}.",
        ],
        "probable_component": cat["component"],
        "suggested_parts": cat["parts"],
        "confidence": 0.4,
        "common_cause": common,
        "role_messages": {
            "technician": f"{machine}: revisar {cat['component'].lower()} ({cat['specialty']}). Desviaciones: {dev}.",
            "supervisor": f"{machine} con severidad {sev} por {fm or 'anomalia'}. Coordinar atencion y repuestos {cat['parts']}.",
            "management": f"{machine} requiere atencion {sev}. Impacto operativo potencial; seguimiento en consola.",
            "operator": f"Reduce la exigencia de {machine} y reporta. Severidad {sev}.",
        },
        "source": "template",
    }


def validate_explain(obj: object) -> dict | None:
    """Valida que el JSON de Gemini tenga la forma esperada. Devuelve el dict normalizado o None."""
    if not isinstance(obj, dict):
        return None
    hyps = obj.get("hypotheses")
    roles = obj.get("role_messages")
    if not isinstance(hyps, list) or not hyps:
        return None
    if not isinstance(roles, dict) or any(not isinstance(roles.get(r), str) or not roles.get(r) for r in ROLES):
        return None
    parts = obj.get("suggested_parts")
    if parts is not None and not isinstance(parts, list):
        return None
    try:
        confidence = float(obj.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    common = obj.get("common_cause")
    return {
        "hypotheses": [str(h) for h in hyps],
        "probable_component": str(obj.get("probable_component", "")),
        "suggested_parts": [str(p) for p in (parts or [])],
        "confidence": confidence,
        "common_cause": common if (common is None or isinstance(common, str)) else str(common),
        "role_messages": {r: str(roles[r]) for r in ROLES},
        "source": "gemini",
    }
