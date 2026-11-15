"""Explicacion por plantilla y mensajes por rol (tecnico, supervisor, direccion, operador). Funciones puras.

La explicacion por plantilla cumple `incident.assigned.payload.explanation` (source `template`) y es el respaldo
cuando Cloud Run `/explain` no responde (T14). Mismos insumos, mismos textos.
"""
from __future__ import annotations

from fplib import ids

COMPONENT = {
    "hydraulic_pump": "Bomba hidraulica principal",
    "hydraulic_contamination": "Filtro y aceite del circuito hidraulico",
    "hydraulic_overheat": "Enfriador de aceite hidraulico",
    "fuel_contamination": "Filtro de combustible e inyectores",
    "engine_overheat": "Sistema de refrigeracion del motor",
    "final_drive": "Rodamientos del mando final",
    "brake_wear": "Pastillas de freno",
    "electrical_fault": "Alternador y sistema electrico",
    "sensor_fault": "Sensor de presion o su cableado",
}
OPERATOR = {
    "immediate": "detenga la maquina en un lugar seguro y espere al tecnico",
    "schedule": "reduzca la carga, siga operando con precaucion y avise si aparece una alarma",
    "observe": "siga operando con normalidad y reporte cualquier ruido o alarma",
}


def _model(machine_id: str) -> str:
    return machine_id.split("-", 1)[0]  # EX-017 -> EX (el prefijo del id es el modelo, ver ids.yaml)


def _part_names() -> dict[str, str]:
    return {p["part_id"]: p["name"] for p in ids.load()["parts"]}


def _var(d: dict) -> str:
    return f"{d['var']} {'alta' if d['z'] > 0 else 'baja'} (z {d['z']:+.1f})"


def explanation(deviated_vars: list[dict], failure_mode: str, machine_id: str) -> dict:
    top = sorted(deviated_vars, key=lambda d: abs(d["z"]), reverse=True)[:3]
    component = COMPONENT.get(failure_mode, failure_mode)
    signs = ", ".join(_var(d) for d in top) or "sin variables desviadas"
    return {
        "source": "template",
        "summary": f"{machine_id}: {signs}. Causa probable: {component.lower()} ({failure_mode}).",
        "probable_component": component,
        "suggested_parts": [b["part_id"] for b in ids.bom_for(_model(machine_id), failure_mode)],
        "common_cause": None,
    }


def messages(incident: dict, explanation: dict, ranking_top: dict | None, approve_url: str | None) -> dict:
    """`incident`: incident_id, machine_id, severity, score, failure_mode, zone_id, lat, lon, deviated_vars,
    group_id, recurrence_flag, tech_busy (el tecnico propuesto esta ocupado con otro incidente). Devuelve los cuatro
    textos y el asunto comun."""
    mid, sev, mode = incident["machine_id"], incident["severity"], incident["failure_mode"]
    names = _part_names()
    parts = ", ".join(f"{p} {names.get(p, '')}".strip() for p in explanation.get("suggested_parts") or []) or "ninguno"
    variables = "\n".join(f"- {d['var']} = {d['value']:g} (z {d['z']:+.1f})" for d in incident["deviated_vars"])
    where = f"zona {incident['zone_id']}, lat {incident['lat']}, lon {incident['lon']}"
    tech = (f"{ranking_top['tech_id']} {ranking_top['name']} ({ranking_top['distance_km']:.1f} km)"
            if ranking_top else "sin tecnico disponible")
    group = incident.get("group_id")
    busy = ("Aviso: el tecnico propuesto esta ocupado con otro incidente y no hay otro libre; "
            "coordinar la prioridad con el supervisor.") if incident.get("tech_busy") and ranking_top else None
    technician = (
        f"Incidente {incident['incident_id']}: {mid}, severidad {sev}, puntaje {incident['score']:.2f}.\n"
        f"Falla probable: {mode} ({explanation.get('probable_component', mode)}).\n"
        f"Variables desviadas:\n{variables}\n"
        f"Ubicacion: {where}.\n"
        f"Repuestos sugeridos: {parts}."
        + (f"\n{busy}" if busy else "")
    )
    supervisor_lines = [
        f"{mid} ({incident['zone_id']}) requiere atencion: severidad {sev}, puntaje {incident['score']:.2f}, "
        f"falla probable {mode}.",
        f"Diagnostico: {explanation['summary']}",
        f"Tecnico propuesto: {tech}" + (" (ocupado con otro incidente: no hay tecnicos libres, reasignar o "
                                         "priorizar)." if busy else "."),
    ]
    if incident.get("recurrence_flag"):
        supervisor_lines.append("Recurrente: misma falla cerrada en los ultimos 7 dias.")
    if group:
        supervisor_lines.append(f"Agrupado con otras maquinas de la zona: {group}.")
    if approve_url:
        supervisor_lines.append(f"Aprobar la asignacion: {approve_url}")
    management = (f"{mid} en riesgo de parada no programada por {mode} (severidad {sev}, zona {incident['zone_id']})"
                  + (f"; incidente agrupado {group}, posible causa comun" if group else "")
                  + f"; tecnico {ranking_top['tech_id'] if ranking_top else 'por asignar'}.")
    operator = f"{mid}: {OPERATOR.get(sev, OPERATOR['schedule'])}."
    return {"technician": technician, "supervisor": "\n".join(supervisor_lines), "management": management,
            "operator": operator, "subject": f"[FleetPulse] {sev} {mid} {incident['incident_id']}"}
