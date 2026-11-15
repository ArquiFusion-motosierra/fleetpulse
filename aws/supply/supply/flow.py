"""Lambdas de la maquina de estados Standard `supply-flow` (diseno de abastecimiento, 6.1). Una ejecucion por PO o
TRF, con nombre = id (un reintento de supply-sourcing no crea dos). Entrada de la ejecucion: `{"id", "kind"}`.

Load -> WaitForApproval (token en META.task_token, waiting_for approval, 24 h) -> Approve -> PlaceOrder | Ship ->
WaitForReceipt (waiting_for receipt, 2 x eta / DEMO_WARP, una espera mas si vence) -> Receive -> AllocateBackorders.
Rechazo (SendTaskFailure con error `SupplyRejected`), 24 h sin respuesta o stock insuficiente al despachar -> Cancel.

Cada paso relee el META y escribe con condicion de estado: un reintento de Step Functions que encuentra el documento
ya en el estado destino responde lo mismo sin volver a mover stock; cualquier otro estado es `InvalidState`.
Devuelven solo datos JSON (sin Decimal) y nunca el task token.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import timedelta

from fplib import clock, envelope, ids, log, outbox

from supply import docs, notices, store
from supply.docs import InvalidState, event, event_items, stock_item, transition_item

APPROVAL_TIMEOUT_S = 86400
MIN_RECEIPT_TIMEOUT_S = 300
RSV_NAMESPACE = uuid.UUID("8f0f7f5e-2b0c-4c56-9a51-5e3a1c0d9b11")
_clients: dict = {}


class InsufficientStock(Exception):
    """El almacen de origen ya no tiene `available >= qty + safety_stock` (sin override): se cancela la TRF."""


def _client(name: str):
    if name not in _clients:
        import boto3
        _clients[name] = boto3.client(name)
    return _clients[name]


def _ddb(ddb):
    return ddb or _client("dynamodb")


def _publish(sns, role: str, tenant_id: str, notice: dict) -> None:
    (sns or _client("sns")).publish(
        TopicArn=os.environ["NOTIFY_TOPIC_ARN"], Subject=notice["subject"], Message=notice["message"],
        MessageAttributes={"role": {"DataType": "String", "StringValue": role},
                           "tenant_id": {"DataType": "String", "StringValue": tenant_id}})


def _receipt_timeout_s(eta_h: float) -> int:
    warp = max(float(os.environ.get("DEMO_WARP", "1") or 1), 1.0)
    return max(MIN_RECEIPT_TIMEOUT_S, int(2 * float(eta_h) * 3600 / warp))


def _advance(ddb, doc: dict, frm: tuple[str, ...], to: str, items: list[dict], fields: dict | None = None,
             reason: str | None = None, on_fail=None) -> dict:
    """META frm -> to + `items` + EV#/outbox en una transaccion. Devuelve el META nuevo.

    Si el META ya esta en `to` (reintento) no escribe nada. `on_fail(reasons)` traduce una condicion fallida de
    `items` (indices desde 0) en un error de dominio."""
    new = {**doc, **(fields or {}), "status": to}
    env = event(new, to, reason=reason)
    try:
        store.transact(ddb, [transition_item(doc, frm, to, fields)] + items + event_items(env))
    except store.ConditionFailed as exc:
        current = docs.load(ddb, doc["id"])
        if current["status"] == to:
            log.event("supply_paso_repetido", id=doc["id"], status=to)
            return current
        if exc.reasons[0] is None and on_fail:
            on_fail(exc.reasons[1:1 + len(items)])
        raise InvalidState(f"{doc['id']}: esta en {current['status']}, se esperaba {'/'.join(frm)}") from None
    log.event("supply_transicion", id=doc["id"], kind=doc["kind"], status=to, event_id=env["event_id"])
    return new


# ---------- pasos ----------

def load(event_, context=None, ddb=None):
    return docs.summary(docs.load(_ddb(ddb), event_["id"]))


def wait_approval(event_, context=None, ddb=None, sns=None):
    """waitForTaskToken: guarda el token y avisa al aprobador. La API (T40) llama SendTaskSuccess / Failure."""
    ddb = _ddb(ddb)
    docs.set_wait(ddb, event_["id"], event_["token"], "approval", ("suggested",))
    doc = docs.load(ddb, event_["id"])
    _publish(sns, notices.notify_role(doc), doc["tenant_id"], notices.approval_request(doc))
    log.event("supply_espera_aprobacion", id=doc["id"], approver_roles=doc.get("approver_roles"))


def approve(event_, context=None, ddb=None):
    ddb = _ddb(ddb)
    doc = docs.load(ddb, event_["id"])
    a = event_.get("approval") or {}
    fields = {"approved_by": a.get("approved_by") or a.get("by"), "approved_role": a.get("role"),
              "override": bool(a.get("override")), "approved_at": clock.iso(clock.now())}
    return docs.summary(_advance(ddb, doc, ("suggested",), "approved", [], fields))


def cancel(event_, context=None, ddb=None):
    """Rechazo, 24 h sin respuesta o stock insuficiente. Libera los BACKORDER# que apuntaban a esta orden."""
    ddb = _ddb(ddb)
    doc = docs.load(ddb, event_["id"])
    c = event_.get("cancel") or {}
    by, reason = None, c.get("Cause") or ""
    try:  # la API manda Cause = {"reason", "by"} en JSON
        parsed = json.loads(reason)
        if isinstance(parsed, dict):
            by, reason = parsed.get("by"), parsed.get("reason") or ""
    except ValueError:
        pass
    if c.get("Error") == "States.Timeout":
        reason = "sin respuesta en 24 h"
    elif c.get("Error") == "InsufficientStock":
        reason = f"stock insuficiente en {doc.get('from_zone')} al despachar"
    reason = (reason or "rechazada")[:300]
    held = [b for b in store.query_all(ddb, f"BACKORDER#{doc['part_id']}#{doc['zone_id']}") if b.get("ref") == doc["id"]]
    items = [{"Delete": {"TableName": store.table("REGISTRY_TABLE"), "Key": store.key(b["PK"], b["SK"])}} for b in held]
    new = _advance(ddb, doc, ("suggested", "approved"), "cancelled", items,
                   {"cancelled_by": by, "cancel_reason": reason, "cancelled_at": clock.iso(clock.now())}, reason=reason)
    return {**docs.summary(new), "notice": notices.cancelled(new, new.get("cancel_reason", reason))}


def place_order(event_, context=None, ddb=None):
    """PO: status ordered, on_order += qty en el almacen destino, eta = ahora + lead time."""
    ddb = _ddb(ddb)
    doc = docs.load(ddb, event_["id"])
    now = clock.now()
    eta_h = float(doc.get("lead_time_d") or 0) * 24 or float(doc.get("eta_h") or 0)
    fields = {"eta": clock.iso(now + timedelta(hours=eta_h)), "eta_h": eta_h, "ordered_at": clock.iso(now)}
    items = [stock_item(doc["part_id"], doc["zone_id"], "SET on_order = if_not_exists(on_order, :z) + :q",
                        z=0, q=int(doc["qty"]))]
    new = _advance(ddb, doc, ("approved",), "ordered", items, fields)
    return {**docs.summary(new), "receipt_timeout_s": _receipt_timeout_s(new["eta_h"]), "notice": notices.ordered(new)}


def ship(event_, context=None, ddb=None):
    """TRF: origen on_hand/available -= qty con condicion available >= qty + safety_stock (sin override), destino
    in_transit += qty, MOVE# transfer_out en el origen."""
    ddb = _ddb(ddb)
    doc = docs.load(ddb, event_["id"])
    now, q = clock.now(), int(doc["qty"])
    origin = store.stock(ddb, doc["part_id"], doc["from_zone"]) or {}
    safety = max(int(origin.get("safety_stock") or 0), ids.part_policy(doc["part_id"])["safety_stock"])
    need = q if doc.get("override") else q + safety
    at = clock.iso(now)
    items = [
        stock_item(doc["part_id"], doc["from_zone"], "SET on_hand = on_hand - :q, available = available - :q",
                   "attribute_exists(PK) AND available >= :need", q=q, need=need),
        stock_item(doc["part_id"], doc["zone_id"], "SET in_transit = if_not_exists(in_transit, :z) + :q", z=0, q=q),
        docs.move_item(doc["part_id"], doc["from_zone"], "transfer_out", -q, doc["id"], doc["from_tenant"], at),
    ]
    fields = {"eta": clock.iso(now + timedelta(hours=float(doc["eta_h"]))), "shipped_at": at}

    def short(reasons):
        if reasons and reasons[0] == "ConditionalCheckFailed":
            raise InsufficientStock(f"{doc['from_zone']}: available < {need} de {doc['part_id']}")

    new = _advance(ddb, doc, ("approved",), "in_transit", items, fields, on_fail=short)
    return {**docs.summary(new), "receipt_timeout_s": _receipt_timeout_s(new["eta_h"]), "notice": notices.shipped(new)}


def wait_receipt(event_, context=None, ddb=None, sns=None):
    """waitForTaskToken de la recepcion; con `late` (vencio la primera espera) avisa al jefe."""
    ddb = _ddb(ddb)
    docs.set_wait(ddb, event_["id"], event_["token"], "receipt", ("ordered", "in_transit"))
    if event_.get("late"):
        doc = docs.load(ddb, event_["id"])
        _publish(sns, "maintenance_head", doc["tenant_id"], notices.late(doc))
        log.event("supply_atrasada", id=doc["id"], eta=doc.get("eta"))


def receive(event_, context=None, ddb=None):
    """Destino on_hand/available += qty, on_order (PO) o in_transit (TRF) -= qty, MOVE# receipt | transfer_in."""
    ddb = _ddb(ddb)
    doc = docs.load(ddb, event_["id"])
    q, at = int(doc["qty"]), clock.iso(clock.now())
    pending = "on_order" if doc["kind"] == "po" else "in_transit"
    r = event_.get("receipt") or {}
    items = [
        stock_item(doc["part_id"], doc["zone_id"],
                   f"SET on_hand = on_hand + :q, available = available + :q, {pending} = if_not_exists({pending}, :z) - :q",
                   z=0, q=q),
        docs.move_item(doc["part_id"], doc["zone_id"], "receipt" if doc["kind"] == "po" else "transfer_in", q,
                       doc["id"], doc["tenant_id"], at),
    ]
    new = _advance(ddb, doc, ("ordered", "in_transit"), "received", items,
                   {"received_by": r.get("received_by") or r.get("by"), "received_at": at})
    return docs.summary(new)


def _reserved_event(bo: dict, rid: str) -> dict:
    return envelope.build("part.reserved", "aws.supply", "part_reservation", f"RSV-{bo['incident_id']}",
                          {"reservations": [{"reservation_id": rid, "part_id": bo["part_id"], "zone_id": bo["zone_id"],
                                             "qty": int(bo["qty"])}]},
                          incident_id=bo["incident_id"], machine_id=bo["machine_id"], tenant_id=bo.get("tenant_id"))


def allocate_backorders(event_, context=None, ddb=None):
    """Por cada BACKORDER# del almacen y repuesto (el mas antiguo primero) reserva como `reserve()` de aws/registry
    (RSV#<incident>/<part>#<zone>, que el cierre del incidente consume), emite part.reserved con el incident_id y
    borra el BACKORDER#. Si no alcanza, los que quedan siguen esperando la proxima recepcion."""
    ddb = _ddb(ddb)
    doc = docs.load(ddb, event_["id"])
    reg = store.table("REGISTRY_TABLE")
    waiting = sorted(store.query_all(ddb, f"BACKORDER#{doc['part_id']}#{doc['zone_id']}"),
                     key=lambda b: (b.get("created_at", ""), b["SK"]))
    allocated, pending = [], []
    for bo in waiting:
        q = int(bo["qty"])
        rid = f"{uuid.uuid5(RSV_NAMESPACE, bo['PK'] + '#' + bo['SK'] + '#' + str(bo.get('ref')))}#0"
        env = _reserved_event(bo, rid)
        delete = {"Delete": {"TableName": reg, "Key": store.key(bo["PK"], bo["SK"])}}
        try:
            store.transact(ddb, [
                stock_item(bo["part_id"], bo["zone_id"], "SET reserved = reserved + :q, available = available - :q",
                           "attribute_exists(PK) AND available >= :q", q=q),
                {"Put": {"TableName": reg, "ConditionExpression": "attribute_not_exists(PK)", "Item": store.ser({
                    "PK": f"RSV#{bo['incident_id']}", "SK": f"{bo['part_id']}#{bo['zone_id']}", "reservation_id": rid,
                    "incident_id": bo["incident_id"], "part_id": bo["part_id"], "zone_id": bo["zone_id"], "qty": q,
                    "status": "reserved", "created_at": env["occurred_at"], "ref": bo.get("ref")})}},
                delete,
                outbox.transact_items(env, table=store.table("OUTBOX_TABLE")),
                {"Put": {"TableName": reg, "Item": store.ser({
                    "PK": f"INCIDENT#{bo['incident_id']}", "SK": f"EV#{env['occurred_at']}#{env['event_id']}",
                    "event_type": env["event_type"], "source": env["source"], "payload": store.dumps(env)})}},
            ])
        except store.ConditionFailed as exc:
            if exc.reasons[1] == "ConditionalCheckFailed":  # ya reservado (reintento): solo falta borrar el pendiente
                ddb.delete_item(TableName=reg, Key=store.key(bo["PK"], bo["SK"]))
                allocated.append({"incident_id": bo["incident_id"], "machine_id": bo.get("machine_id"), "qty": q})
                continue
            pending.append(bo["incident_id"])  # no alcanza el stock
            continue
        allocated.append({"incident_id": bo["incident_id"], "machine_id": bo.get("machine_id"), "qty": q})
        log.event("supply_backorder_reservado", id=doc["id"], incident_id=bo["incident_id"], part_id=bo["part_id"],
                  zone_id=bo["zone_id"], qty=q, event_id=env["event_id"])
    return {"allocated": allocated, "pending": pending,
            "notice_technician": notices.received(doc, allocated, "technician"),
            "notice_admin": notices.received(doc, allocated, "admin")}
