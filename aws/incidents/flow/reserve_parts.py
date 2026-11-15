"""Lambda `reserve_parts` (ReserveParts): pide la reserva de repuestos del incidente con `part.reserve.requested`.

Lineas: BOM del modelo y modo de falla (`fplib.ids.bom_for`); sin BOM, los `suggested_parts` de `$.explanation`
con qty 1; si tampoco hay, `requested: []` y ningun evento. El sobre (aggregate `part_reservation`
`RSV-<incident_id>`, valida contra contracts/part.schema.json) va a fp-outbox y como EV# del incidente, en la
misma transaccion que marca el META (`parts_requested_at`, `parts_requested`). La Lambda `registry`
(aws/registry) lo recibe por SNS y reserva el stock.
Idempotencia: la transaccion exige que el META aun no tenga `parts_requested_at`; un reintento no emite otro pedido
(registry reservaria dos veces) y devuelve las lineas ya pedidas. Tambien exige `status = open`: un incidente ya
cerrado (POST /close) no pide repuestos (`skipped: closed`).
"""
from __future__ import annotations

from fplib import envelope, ids, log
from lib import registry as fp_registry
from lib import templates


def _lines(event: dict) -> tuple[list[dict], str]:
    model = event.get("model") or templates.model_of(event["machine_id"])
    bom = ids.bom_for(model, event["failure_mode"])
    if bom:
        return bom, "bom"
    suggested = (event.get("explanation") or {}).get("suggested_parts") or []
    return [{"part_id": p, "qty": 1} for p in dict.fromkeys(suggested)], "explanation"


def handler(event, context=None, registry=None, outbox=None):
    registry = registry or fp_registry.table_from_env("REGISTRY_TABLE")
    outbox = outbox or fp_registry.table_from_env("OUTBOX_TABLE")
    incident_id, machine_id = event["incident_id"], event["machine_id"]
    ctx = {"incident_id": incident_id, "machine_id": machine_id}
    lines, origin = _lines(event)
    if not lines:
        log.event("sin_repuestos_para_reservar", failure_mode=event["failure_mode"], **ctx)
        return {"requested": []}
    payload = {"zone_id": event["incident"]["zone_id"], "failure_mode": event["failure_mode"], "lines": lines}
    env = envelope.build("part.reserve.requested", "aws.incident-flow", "part_reservation", f"RSV-{incident_id}",
                         payload, incident_id=incident_id, machine_id=machine_id)
    try:
        fp_registry.update_incident(registry, incident_id, envelope=env, outbox_tbl_name=outbox.name,
                                    expect={"parts_requested_at": None, "status": "open"},
                                    parts_requested_at=env["occurred_at"], parts_requested=lines)
    except fp_registry.ConditionFailed:
        meta = fp_registry.get_incident(registry, incident_id) or {}
        if "parts_requested_at" in meta:
            log.event("reserva_ya_solicitada", **ctx)
            return {"requested": fp_registry.from_ddb(meta.get("parts_requested") or [])}
        if meta.get("status") == "closed":
            log.event("reserva_omitida_incidente_cerrado", **ctx)
            return {"requested": [], "skipped": "closed"}
        raise RuntimeError(f"{incident_id}: el incidente no existe en fp-registry, no se pide reserva") from None
    log.event("reserva_solicitada", lines=lines, origin=origin, zone_id=payload["zone_id"], event_id=env["event_id"],
              **ctx)
    return {"requested": lines}
