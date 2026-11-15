"""Lambda `render_messages` (RenderMessages): textos por rol y asunto que publica NotifyByRole en fp-notify.

Usa `$.explanation` si existe (T14) o la plantilla sobre `$.incident.deviated_vars`, el primero del ranking como
tecnico propuesto (score 0 = ocupado: nadie libre al rankear) y `$.approve_url` si existe (T14).
Idempotencia: funcion pura del input.
"""
from __future__ import annotations

from fplib import log
from lib import templates


def handler(event, context=None):
    inc = event["incident"]
    deviated = inc.get("deviated_vars") or event["deviated_vars"]
    top = event["ranking"]["top"]
    incident = {
        "incident_id": event["incident_id"], "machine_id": event["machine_id"],
        "severity": event["classified"]["severity"], "score": float(event["score"]),
        "failure_mode": event["failure_mode"], "zone_id": inc["zone_id"], "lat": event["lat"], "lon": event["lon"],
        "deviated_vars": deviated, "group_id": inc.get("group_id"),
        "recurrence_flag": inc.get("recurrence_flag", False), "tech_busy": bool(top) and top[0]["score"] == 0,
    }
    explanation = event.get("explanation") or templates.explanation(deviated, event["failure_mode"],
                                                                    event["machine_id"])
    out = templates.messages(incident, explanation, top[0] if top else None, event.get("approve_url"))
    log.event("mensajes_generados", incident_id=event["incident_id"], machine_id=event["machine_id"],
              subject=out["subject"])
    return out
