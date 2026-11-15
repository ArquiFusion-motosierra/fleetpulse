"""Lambda `render_messages` (RenderMessages): textos por rol y asunto que publica NotifyByRole en fp-notify.

Corre despues de AssignTechnician: nombra al tecnico de `$.assignment.tech_id` (nombre y distancia desde
`$.ranking.top`, o solo el id si no esta ahi), la aprobacion y `$.assignment.tech_busy`. La severidad es la vigente
(`$.assignment.severity`, por si el incidente escalo mientras esperaba la aprobacion) o la de `$.classified`. Usa `$.explanation` si
existe (T14) o la plantilla sobre `$.incident.deviated_vars`, y `$.approve_url` si existe.
Idempotencia: funcion pura del input.
"""
from __future__ import annotations

from fplib import log
from lib import templates


def handler(event, context=None):
    inc = event["incident"]
    deviated = inc.get("deviated_vars") or event["deviated_vars"]
    assignment = event["assignment"]
    tech_id = assignment["tech_id"]
    assigned = next((t for t in event["ranking"]["top"] if t["tech_id"] == tech_id),
                    {"tech_id": tech_id, "name": tech_id, "distance_km": None})
    incident = {
        "incident_id": event["incident_id"], "machine_id": event["machine_id"],
        "severity": assignment.get("severity") or event["classified"]["severity"], "score": float(event["score"]),
        "failure_mode": event["failure_mode"], "zone_id": inc["zone_id"], "lat": event["lat"], "lon": event["lon"],
        "deviated_vars": deviated, "group_id": inc.get("group_id"),
        "recurrence_flag": inc.get("recurrence_flag", False), "tech_busy": bool(assignment.get("tech_busy")),
        "approval": assignment.get("approval"),
    }
    explanation = event.get("explanation") or templates.explanation(deviated, event["failure_mode"],
                                                                    event["machine_id"])
    out = templates.messages(incident, explanation, assigned, event.get("approve_url"))
    log.event("mensajes_generados", incident_id=event["incident_id"], machine_id=event["machine_id"],
              subject=out["subject"])
    return out
