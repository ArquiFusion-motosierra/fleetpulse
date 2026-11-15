"""Documento de una orden de compra (`PO#<id>/META`) o transferencia (`TRF#<id>/META`), sus eventos `supply.*` y
las transiciones de estado con condicion (diseno de abastecimiento, 4.2 a 4.4).

Estados: suggested -> approved -> ordered (PO) | in_transit (TRF) -> received; suggested | approved -> cancelled.
Cada transicion es una TransactWriteItems: META con `#st IN (...)`, lo que toque de STOCK / MOVE# / BACKORDER#, el EV#
de la linea de tiempo y el evento en fp-outbox. `task_token` / `waiting_for` (approval | receipt) los escriben las
esperas de supply-flow y se borran en la transicion siguiente. El token nunca se registra ni se devuelve.
"""
from __future__ import annotations

from fplib import clock, envelope, outbox, schema

from supply import store
from supply.store import key, ser, vals

EVENT_TYPE = {"suggested": "supply.suggested", "approved": "supply.approved", "cancelled": "supply.cancelled",
              "ordered": "po.ordered", "in_transit": "transfer.shipped", "received": "supply.received"}
SUMMARY = ("id", "kind", "status", "tenant_id", "zone_id", "from_zone", "from_tenant", "part_id", "qty", "supplier_id",
           "incident_id", "machine_id", "eta", "eta_h", "cost_usd", "approver_roles", "override")


class InvalidState(Exception):
    """El documento no esta en un estado desde el que se pueda hacer la transicion (o no existe)."""


def pk_of(doc_id: str) -> str:
    if doc_id.startswith("PO-"):
        return f"PO#{doc_id}"
    if doc_id.startswith("TRF-"):
        return f"TRF#{doc_id}"
    raise InvalidState(f"id desconocido: {doc_id}")


def load(ddb, doc_id: str) -> dict:
    doc = store.get(ddb, pk_of(doc_id))
    if doc is None:
        raise InvalidState(f"{doc_id}: no existe")
    return doc


def summary(doc: dict) -> dict:
    """Lo que viaja en el estado de la ejecucion: sin token, sin opciones (limite de 256 KB y nada secreto)."""
    return {k: doc[k] for k in SUMMARY if doc.get(k) is not None}


def event(doc: dict, status: str, *, reason: str | None = None) -> dict:
    """Sobre `supply.*` validado contra envelope.schema.json y supply.schema.json."""
    p = {"kind": doc["kind"], "tenant_id": doc["tenant_id"], "zone_id": doc["zone_id"], "part_id": doc["part_id"],
         "qty": int(doc["qty"]), "status": status, "cost_usd": doc.get("cost_usd"), "eta": doc.get("eta"),
         "incident_id": doc.get("incident_id"), "from_zone": doc.get("from_zone"),
         "supplier_id": doc.get("supplier_id"), "reason": reason}
    if doc.get("downtime_avoided_h"):
        p |= {"downtime_avoided_h": doc["downtime_avoided_h"], "downtime_avoided_usd": doc.get("downtime_avoided_usd", 0)}
    if status == "suggested":
        p["options"] = doc.get("options") or []
    p = {k: v for k, v in p.items() if v is not None}
    env = envelope.build(EVENT_TYPE[status], "aws.supply", doc["aggregate_type"], doc["id"], p,
                         incident_id=doc.get("incident_id"), tenant_id=doc["tenant_id"])
    schema.validate("supply", env)
    return env


def event_items(env: dict, pk: str | None = None) -> list[dict]:
    """EV# de la linea de tiempo (sobre completo, como INCIDENT#.../EV#) + fp-outbox."""
    pk = pk or pk_of(env["aggregate_id"])
    return [{"Put": {"TableName": store.table("REGISTRY_TABLE"), "Item": ser({
                "PK": pk, "SK": f"EV#{env['occurred_at']}#{env['event_id']}", "event_type": env["event_type"],
                "source": env["source"], "payload": store.dumps(env)})}},
            outbox.transact_items(env, table=store.table("OUTBOX_TABLE"))]


def put_items(doc: dict) -> list[dict]:
    """Alta de la sugerencia (condicion: el id no existe) + su evento supply.suggested."""
    return [{"Put": {"TableName": store.table("REGISTRY_TABLE"), "Item": ser(doc),
                     "ConditionExpression": "attribute_not_exists(PK)"}}] + event_items(event(doc, "suggested"))


def transition_item(doc: dict, frm: tuple[str, ...], to: str, fields: dict | None = None,
                    clear_wait: bool = True) -> dict:
    fields = {"updated_at": clock.iso(clock.now()), **(fields or {})}
    names, values = {"#st": "status"}, {"to": to, "g2": f"SUPPLY#{to}"}
    sets = ["#st = :to", "GSI2PK = :g2"]
    for i, (k, v) in enumerate(fields.items()):
        if v is None:
            continue
        names[f"#f{i}"], values[f"f{i}"] = k, v
        sets.append(f"#f{i} = :f{i}")
    allowed = {f"from{i}": s for i, s in enumerate(frm)}
    expr = "SET " + ", ".join(sets) + (" REMOVE task_token, waiting_for" if clear_wait else "")
    return {"Update": {"TableName": store.table("REGISTRY_TABLE"), "Key": key(pk_of(doc["id"]), "META"),
                       "UpdateExpression": expr, "ExpressionAttributeNames": names,
                       "ConditionExpression": f"#st IN ({', '.join(':' + k for k in allowed)})",
                       "ExpressionAttributeValues": vals(**values, **allowed)}}


def stock_item(part_id: str, zone_id: str, expr: str, cond: str = "attribute_exists(PK)", **values) -> dict:
    return {"Update": {"TableName": store.table("REGISTRY_TABLE"), "Key": key(f"STOCK#{part_id}#{zone_id}", "META"),
                       "UpdateExpression": expr, "ConditionExpression": cond, "ExpressionAttributeValues": vals(**values)}}


def move_item(part_id: str, zone_id: str, kind: str, qty: int, ref: str, tenant_id: str, at: str) -> dict:
    return {"Put": {"TableName": store.table("REGISTRY_TABLE"), "Item": ser({
        "PK": f"MOVE#{part_id}#{zone_id}", "SK": f"{at}#{ref}", "kind": kind, "qty": qty, "ref": ref,
        "tenant_id": tenant_id, "part_id": part_id, "zone_id": zone_id, "created_at": at})}}


def backorder_item(doc: dict, created_at: str) -> dict:
    """El incidente espera el repuesto en el almacen destino; AllocateBackorders lo reserva al recibir."""
    return {"Put": {"TableName": store.table("REGISTRY_TABLE"), "Item": ser({
        "PK": f"BACKORDER#{doc['part_id']}#{doc['zone_id']}", "SK": doc["incident_id"], "part_id": doc["part_id"],
        "zone_id": doc["zone_id"], "incident_id": doc["incident_id"], "machine_id": doc.get("machine_id"),
        "qty": int(doc.get("needed_qty") or doc["qty"]),
        "ref": doc["id"], "tenant_id": doc["tenant_id"], "created_at": created_at})}}


def set_wait(ddb, doc_id: str, token: str, waiting_for: str, frm: tuple[str, ...]) -> None:
    """Guarda el task token de la espera actual (condicion: el documento sigue en `frm`)."""
    allowed = {f"from{i}": s for i, s in enumerate(frm)}
    try:
        ddb.update_item(TableName=store.table("REGISTRY_TABLE"), Key=key(pk_of(doc_id), "META"),
                        UpdateExpression="SET task_token = :t, waiting_for = :w, updated_at = :u",
                        ConditionExpression=f"#st IN ({', '.join(':' + k for k in allowed)})",
                        ExpressionAttributeNames={"#st": "status"},
                        ExpressionAttributeValues=vals(t=token, w=waiting_for, u=clock.iso(clock.now()), **allowed))
    except ddb.exceptions.ConditionalCheckFailedException:
        raise InvalidState(f"{doc_id}: no esta en {'/'.join(frm)} para esperar {waiting_for}") from None
