"""Lambda `assign_technician` (AssignTechnician): asigna el tecnico, emite `incident.assigned` y lo marca ocupado.

Tecnico: `$.approval.tech_id` (aprobacion por correo, T14) o el primero de `$.ranking.top`; approval:
`$.approval.approval` o `auto`. Explicacion: `$.explanation` (Cloud Run, T14) o la plantilla.
META (tech_id, assigned_at, approval, explanation) + EV# + fp-outbox van en una transaccion condicionada a que el
incidente aun no tenga tecnico; despues `TECH#<id>` queda `available=false, current_incident=<incident_id>`.
Idempotencia: un reintento encuentra el tecnico ya asignado, no emite otro evento, devuelve esa asignacion y
repite el marcado (mismos valores).
"""
from __future__ import annotations

from fplib import clock, envelope, log
from lib import registry as fp_registry
from lib import technicians, templates


def handler(event, context=None, state=None, registry=None, outbox=None):
    state = state or fp_registry.table_from_env("STATE_TABLE")
    registry = registry or fp_registry.table_from_env("REGISTRY_TABLE")
    outbox = outbox or fp_registry.table_from_env("OUTBOX_TABLE")
    incident_id, machine_id = event["incident_id"], event["machine_id"]
    ctx = {"incident_id": incident_id, "machine_id": machine_id}
    decided = event.get("approval") or {}
    top = event["ranking"]["top"]
    tech_id = decided.get("tech_id") or (top[0]["tech_id"] if top else None)
    if not tech_id:
        raise ValueError(f"sin tecnico para asignar a {incident_id}")
    approval = decided.get("approval") or "auto"
    explanation = event.get("explanation") or templates.explanation(event["deviated_vars"], event["failure_mode"],
                                                                    machine_id)
    now = clock.now()
    assigned_at = clock.iso(now)
    payload = {"tech_id": tech_id, "assigned_at": assigned_at, "approval": approval, "explanation": explanation}
    ranked = next((t for t in top if t["tech_id"] == tech_id), None)
    if ranked:
        payload["distance_km"] = ranked["distance_km"]
    env = envelope.build("incident.assigned", "aws.incident-flow", "incident", incident_id, payload,
                         incident_id=incident_id, machine_id=machine_id, occurred_at=now)
    try:
        fp_registry.update_incident(registry, incident_id, envelope=env, outbox_tbl_name=outbox.name,
                                    expect={"tech_id": None}, status="open", tech_id=tech_id,
                                    assigned_at=assigned_at, approval=approval, explanation=explanation)
        log.event("tecnico_asignado", tech_id=tech_id, approval=approval, source=explanation["source"],
                  event_id=env["event_id"], **ctx)
    except fp_registry.ConditionFailed:
        meta = fp_registry.get_incident(registry, incident_id) or {}
        if not meta.get("tech_id"):
            raise RuntimeError(f"{incident_id}: el incidente no existe en fp-registry, no se puede asignar") from None
        tech_id, assigned_at, approval = meta["tech_id"], meta["assigned_at"], meta["approval"]
        log.event("tecnico_ya_asignado", tech_id=tech_id, **ctx)
    if not technicians.mark_busy(state, tech_id, incident_id):
        log.event("tecnico_sin_item", tech_id=tech_id, **ctx)
    return {"tech_id": tech_id, "assigned_at": assigned_at, "approval": approval}
