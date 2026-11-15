"""Lambda `check_existing` (CheckExistingIncident): busca un incidente abierto de la maquina y lo escala.

Si hay uno abierto y la nueva severidad es mayor, escribe `incident.escalated` (fp-outbox + EV#) y sube la
severidad del META en una sola transaccion condicionada a la severidad leida.
Idempotencia: al reintentar, la severidad ya subio (o cambio en paralelo) y no se emite un segundo evento.
Si escalo, devuelve tambien `from_severity`, `to_severity` y `score`: con ellos la maquina de estados arma el aviso
a supervisor y direccion (`Escalated?` -> `NotifyEscalation`), y `operator_message`, la instruccion de la nueva
severidad para el operador (en immediate, detener la maquina; RenderMessages ya pudo haberle dicho que siga).
"""
from __future__ import annotations

from fplib import envelope, log
from lib import registry as fp_registry
from lib import templates

RANK = {"observe": 1, "schedule": 2, "immediate": 3}


def handler(event, context=None, registry=None, outbox=None):
    registry = registry or fp_registry.table_from_env("REGISTRY_TABLE")
    outbox = outbox or fp_registry.table_from_env("OUTBOX_TABLE")
    machine_id = event["machine_id"]
    ctx = {"incident_id": event["incident_id"], "machine_id": machine_id}
    found = fp_registry.open_incident_for(registry, machine_id)
    if not found:
        log.event("sin_incidente_abierto", **ctx)
        return {"found": False, "incident_id": None, "escalated": False}
    open_id, old, new = found["incident_id"], found.get("severity"), event["classified"]["severity"]
    ctx["open_incident_id"] = open_id
    if RANK[new] <= RANK.get(old, 0):
        log.event("incidente_abierto_sin_cambio", severity=old, new_severity=new, **ctx)
        return {"found": True, "incident_id": open_id, "escalated": False}
    score = float(event["score"])
    env = envelope.build("incident.escalated", "aws.incident-flow", "incident", open_id,
                         {"from_severity": old, "to_severity": new, "score": score},
                         incident_id=open_id, machine_id=machine_id)
    try:
        fp_registry.update_incident(registry, open_id, envelope=env, outbox_tbl_name=outbox.name,
                                    expect={"severity": old}, severity=new, score=score)
    except fp_registry.ConditionFailed:
        log.event("escalamiento_ya_aplicado", severity=old, new_severity=new, **ctx)
        return {"found": True, "incident_id": open_id, "escalated": False}
    log.event("incidente_escalado", from_severity=old, to_severity=new, event_id=env["event_id"], **ctx)
    return {"found": True, "incident_id": open_id, "escalated": True, "from_severity": old, "to_severity": new,
            "score": round(score, 2), "operator_message": templates.operator_message(machine_id, new)}
