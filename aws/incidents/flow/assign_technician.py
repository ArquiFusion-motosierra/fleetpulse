"""Lambda `assign_technician` (AssignTechnician): asigna el tecnico, emite `incident.assigned` y lo reserva.

Preferencia: `$.approval.tech_id` (aprobacion por correo, T14) y despues `$.ranking.top` en orden. approval:
`$.approval.approval` o `auto`. Explicacion: `$.explanation` (Cloud Run, T14) o la plantilla sobre
`$.incident.deviated_vars`.
Cada intento es una sola transaccion: META (tech_id, assigned_at, approval, explanation, tech_busy) condicionado a
que el incidente aun no tenga tecnico + EV# + fp-outbox + reserva de `TECH#<id>` (available=false,
current_incident) condicionada a que siga libre. Si otra ejecucion tomo al tecnico despues del ranking, se cancela
todo y se prueba el siguiente. Un tecnico sin item en fp-state (respaldo de ids.yaml) se asigna sin reserva.
Si nadie esta libre se asigna el primero de la lista sin tocar su `TECH#` (no se pisa su incidente actual), con
`tech_busy=true` en el META y el log `tecnico_ocupado`; los mensajes ya lo avisan (ranking con score 0).
Idempotencia: un reintento choca con la condicion del META y devuelve la asignacion guardada sin emitir otro evento.
"""
from __future__ import annotations

from fplib import clock, envelope, log
from lib import registry as fp_registry
from lib import technicians, templates

RESERVE_INDEX = 3  # posicion de la reserva del tecnico en la transaccion: META, EV#, outbox, TECH#


def _order(event: dict) -> list[str]:
    decided = (event.get("approval") or {}).get("tech_id")
    first = [decided] if decided else []
    return first + [t["tech_id"] for t in event["ranking"]["top"] if t["tech_id"] not in first]


def _commit(registry, outbox_tbl_name: str, event: dict, tech_id: str, *, busy: bool, reserve: dict | None,
            approval: str, explanation: dict) -> dict:
    """Un intento de asignacion. `ConditionFailed(RESERVE_INDEX)` si el tecnico ya no esta libre."""
    incident_id, machine_id = event["incident_id"], event["machine_id"]
    now = clock.now()
    assigned_at = clock.iso(now)
    payload = {"tech_id": tech_id, "assigned_at": assigned_at, "approval": approval, "explanation": explanation}
    ranked = next((t for t in event["ranking"]["top"] if t["tech_id"] == tech_id), None)
    if ranked:
        payload["distance_km"] = ranked["distance_km"]
    env = envelope.build("incident.assigned", "aws.incident-flow", "incident", incident_id, payload,
                         incident_id=incident_id, machine_id=machine_id, occurred_at=now)
    try:
        fp_registry.update_incident(registry, incident_id, envelope=env, outbox_tbl_name=outbox_tbl_name,
                                    expect={"tech_id": None}, extra_items=[reserve] if reserve else None,
                                    status="open", tech_id=tech_id, assigned_at=assigned_at, approval=approval,
                                    explanation=explanation, tech_busy=busy)
    except fp_registry.ConditionFailed as exc:
        if exc.index != 0:
            raise
        meta = fp_registry.get_incident(registry, incident_id) or {}
        if not meta.get("tech_id"):
            raise RuntimeError(f"{incident_id}: el incidente no existe en fp-registry, no se puede asignar") from None
        log.event("tecnico_ya_asignado", incident_id=incident_id, machine_id=machine_id, tech_id=meta["tech_id"])
        return {"tech_id": meta["tech_id"], "assigned_at": meta["assigned_at"], "approval": meta["approval"],
                "tech_busy": bool(meta.get("tech_busy", False))}
    log.event("tecnico_asignado", incident_id=incident_id, machine_id=machine_id, tech_id=tech_id,
              approval=approval, tech_busy=busy, source=explanation["source"], event_id=env["event_id"])
    return {"tech_id": tech_id, "assigned_at": assigned_at, "approval": approval, "tech_busy": busy}


def handler(event, context=None, state=None, registry=None, outbox=None):
    state = state or fp_registry.table_from_env("STATE_TABLE")
    registry = registry or fp_registry.table_from_env("REGISTRY_TABLE")
    outbox = outbox or fp_registry.table_from_env("OUTBOX_TABLE")
    incident_id, machine_id = event["incident_id"], event["machine_id"]
    ctx = {"incident_id": incident_id, "machine_id": machine_id}
    order = _order(event)
    if not order:
        raise ValueError(f"sin tecnico para asignar a {incident_id}")
    deviated = event["incident"].get("deviated_vars") or event["deviated_vars"]
    common = {"approval": (event.get("approval") or {}).get("approval") or "auto",
              "explanation": event.get("explanation") or templates.explanation(deviated, event["failure_mode"],
                                                                               machine_id)}
    for tech_id in order:
        item = technicians.get(state, tech_id)
        if item is not None and not technicians.is_free(item, incident_id):
            continue
        reserve = technicians.claim(state.name, tech_id, incident_id) if item is not None else None
        try:
            return _commit(registry, outbox.name, event, tech_id, busy=False, reserve=reserve, **common)
        except fp_registry.ConditionFailed as exc:
            if exc.index != RESERVE_INDEX:
                raise
            log.event("tecnico_tomado", tech_id=tech_id, **ctx)
    log.event("tecnico_ocupado", tech_id=order[0], **ctx)
    return _commit(registry, outbox.name, event, order[0], busy=True, reserve=None, **common)
