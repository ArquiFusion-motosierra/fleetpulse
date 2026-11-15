"""Lambda `render_messages` (RenderMessages): textos por rol y asunto que publica NotifyByRole en fp-notify.

Usa `$.explanation` si existe (T14) o la plantilla, y el primero del ranking como tecnico propuesto.
Idempotencia: funcion pura del input.
"""
from __future__ import annotations

from fplib import log
from lib import templates


def handler(event, context=None):
    inc = event["incident"]
    incident = {
        "incident_id": event["incident_id"], "machine_id": event["machine_id"],
        "severity": event["classified"]["severity"], "score": float(event["score"]),
        "failure_mode": event["failure_mode"], "zone_id": inc["zone_id"], "lat": event["lat"], "lon": event["lon"],
        "deviated_vars": event["deviated_vars"], "group_id": inc.get("group_id"),
        "recurrence_flag": inc.get("recurrence_flag", False),
    }
    explanation = event.get("explanation") or templates.explanation(event["deviated_vars"], event["failure_mode"],
                                                                    event["machine_id"])
    top = event["ranking"]["top"]
    out = templates.messages(incident, explanation, top[0] if top else None, None)
    log.event("mensajes_generados", incident_id=event["incident_id"], machine_id=event["machine_id"],
              subject=out["subject"])
    return out
