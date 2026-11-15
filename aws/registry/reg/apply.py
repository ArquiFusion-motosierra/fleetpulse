"""Aplica eventos de dominio en fp-registry (reemplaza FLEET_EVENTS.apply de OCI en el plan B).

Cada evento entra en una TransactWriteItems junto con fp-inbox (message_id = event_id, attribute_not_exists):
si ya se aplico, la transaccion se cancela y se responde "duplicate" sin tocar nada.
La reserva de cada linea de repuestos es su propia transaccion: stock (available >= qty) + item
RSV#<event_id>#<linea> con attribute_not_exists, asi que un reintento nunca reserva dos veces.
"""
from __future__ import annotations

import json
import time

from botocore.exceptions import ClientError

from reg import store
from reg.store import INBOX, REGISTRY, dumps, key, ser, vals

ZONE_COORDS = {"NORTE": (-13.9000, -71.5600), "CENTRO": (-13.9200, -71.5450), "SUR": (-13.9400, -71.5550)}


class Duplicate(Exception):
    pass


def _inbox_put(event: dict) -> dict:
    return {"Put": {"TableName": INBOX,
                    "Item": ser({"message_id": event["event_id"], "consumer": "registry",
                                 "event_type": event["event_type"], "ttl": int(time.time()) + store.TTL_7D}),
                    "ConditionExpression": "attribute_not_exists(message_id)"}}


def _timeline_put(event: dict) -> dict:
    return {"Put": {"TableName": REGISTRY, "Item": ser({
        "PK": f"INCIDENT#{event['incident_id']}", "SK": f"EVT#{event['occurred_at']}#{event['event_id']}",
        "type": event["event_type"], "source": event.get("source"), "payload": dumps(event["payload"])})}}


def _update(pk: str, sk: str, expr: str, names: dict | None = None, **values) -> dict:
    u = {"TableName": REGISTRY, "Key": key(pk, sk), "UpdateExpression": expr}
    if values:
        u["ExpressionAttributeValues"] = vals(**values)
    if names:
        u["ExpressionAttributeNames"] = names
    return {"Update": u}


def _transact(ddb, items: list[dict]) -> None:
    """Primer item siempre es fp-inbox: si su condicion falla, el evento ya se aplico."""
    try:
        ddb.transact_write_items(TransactItems=items)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "TransactionCanceledException":
            reasons = exc.response.get("CancellationReasons") or []
            if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
                raise Duplicate() from exc
        raise


def _already(ddb, event_id: str) -> bool:
    return "Item" in ddb.get_item(TableName=INBOX, Key={"message_id": {"S": event_id}})


def _incident(ddb, inc: str) -> dict:
    return store.get(ddb, f"INCIDENT#{inc}", "META") or {}


# ---------- incidentes ----------

def on_opened(ddb, e: dict) -> list[dict]:
    p, inc, mid = e["payload"], e["incident_id"], e["machine_id"]
    return [
        _update(f"INCIDENT#{inc}", "META",
                "SET machine_id = :mid, zone_id = :zone, group_id = :grp, severity = :sev, score = :score, "
                "failure_mode = :mode, opened_at = :opened, recurrence_flag = :rec, lat = :lat, lon = :lon, "
                "#st = if_not_exists(#st, :open), GSI1PK = :g1, GSI1SK = :opened, GSI2PK = :g2, GSI2SK = :g2sk",
                names={"#st": "status"},
                mid=mid, zone=p["zone_id"], grp=p.get("group_id") or "-", sev=p["severity"], score=p["score"],
                mode=p["failure_mode"], opened=p["opened_at"], rec=bool(p["recurrence_flag"]),
                lat=p["lat"], lon=p["lon"], open="open", g1="INCIDENTS", g2=f"MACHINE#{mid}",
                g2sk=f"INC#{p['opened_at']}"),
        _update(f"MACHINE#{mid}", "META", "SET severity = :sev, score = :score, open_incident_id = :inc",
                sev=p["severity"], score=p["score"], inc=inc),
    ]


# Si un evento posterior llega antes que incident.opened (SQS estandar no ordena)
ENSURE = ("machine_id = if_not_exists(machine_id, :mid), opened_at = if_not_exists(opened_at, :ts), "
          "GSI1PK = if_not_exists(GSI1PK, :g1), GSI1SK = if_not_exists(GSI1SK, :ts), "
          "GSI2PK = if_not_exists(GSI2PK, :g2), GSI2SK = if_not_exists(GSI2SK, :g2sk)")


def _ensure_vals(mid: str, ts: str) -> dict:
    return {"mid": mid, "ts": ts, "g1": "INCIDENTS", "g2": f"MACHINE#{mid}", "g2sk": f"INC#{ts}"}


def on_escalated(ddb, e: dict) -> list[dict]:
    p, inc, mid = e["payload"], e["incident_id"], e["machine_id"]
    machine = store.get(ddb, f"MACHINE#{mid}", "META") or {}
    items = [_update(f"INCIDENT#{inc}", "META", "SET severity = :sev, score = :score, " + ENSURE,
                     sev=p["to_severity"], score=p["score"], **_ensure_vals(mid, e["occurred_at"]))]
    if machine.get("open_incident_id") == inc:
        items.append(_update(f"MACHINE#{mid}", "META", "SET severity = :sev, score = :score",
                             sev=p["to_severity"], score=p["score"]))
    return items


def on_assigned(ddb, e: dict) -> list[dict]:
    p, inc, mid = e["payload"], e["incident_id"], e["machine_id"]
    closed = _incident(ddb, inc).get("status") == "closed"
    expl = p["explanation"]
    items = [_update(
        f"INCIDENT#{inc}", "META",
        "SET tech_id = :tech, assigned_at = :at, approval = :appr, explanation = :ex, explanation_src = :src, "
        "#st = :st, " + ENSURE, names={"#st": "status"},
        tech=p["tech_id"], at=p["assigned_at"], appr=p["approval"], ex=expl["summary"][:1000],
        src=expl["source"], st="closed" if closed else "assigned", **_ensure_vals(mid, e["occurred_at"]))]
    if not closed:
        items.append(_update(f"TECH#{p['tech_id']}", "META", "SET available = :n, current_incident = :inc",
                             n=False, inc=inc))
    return items


def on_closed(ddb, e: dict) -> list[dict]:
    p, inc, mid = e["payload"], e["incident_id"], e["machine_id"]
    current = _incident(ddb, inc)
    machine = store.get(ddb, f"MACHINE#{mid}", "META") or {}
    rsv = store.query_all(ddb, TableName=REGISTRY, KeyConditionExpression="PK = :pk AND begins_with(SK, :r)",
                          ExpressionAttributeValues=vals(pk=f"INCIDENT#{inc}", r="RSV#"))
    reserved = [r for r in rsv if r.get("status") == "reserved"]
    items = [_update(
        f"INCIDENT#{inc}", "META",
        "SET #st = :closed, closed_at = :at, downtime_h = :dt, cause_summary = :cause, parts_used = :parts, "
        + ENSURE, names={"#st": "status"},
        closed="closed", at=p["closed_at"], dt=p["downtime_h"], cause=(p.get("cause_summary") or "-")[:1000],
        parts=p["parts_used"], **_ensure_vals(mid, e["occurred_at"]))]
    if current.get("tech_id"):
        items.append(_update(f"TECH#{current['tech_id']}", "META", "SET available = :y REMOVE current_incident",
                             y=True))
    if machine.get("open_incident_id") in (inc, None):
        items.append(_update(f"MACHINE#{mid}", "META", "SET severity = :ok, score = :z REMOVE open_incident_id",
                             ok="ok", z=0))
    # Una transaccion no puede tocar dos veces el mismo item: se agrupa por (repuesto, zona)
    used: dict[tuple[str, str], int] = {}
    for line in p["parts_used"]:
        k = (line["part_id"], line["zone_id"])
        used[k] = used.get(k, 0) + int(line["qty"])
    held: dict[tuple[str, str], int] = {}
    for r in reserved:
        k = (r["part_id"], r["zone_id"])
        held[k] = held.get(k, 0) + int(r["qty"])
        status = "consumed" if k in used else "released"  # lo reservado que no se uso vuelve al stock libre
        items.append(_update(r["PK"], r["SK"], "SET #st = :s", names={"#st": "status"}, s=status))
    for k in sorted(set(used) | set(held)):
        part, zone = k
        q, h = used.get(k, 0), held.get(k, 0)
        items.append(_update("STOCK", f"{part}#{zone}",
                             "SET on_hand = on_hand - :q, reserved = reserved - :h, available = available + :d",
                             q=q, h=h, d=h - q))
        if q:
            items.append({"Put": {"TableName": REGISTRY, "Item": ser({
                "PK": f"MOVE#{part}#{zone}", "SK": f"{p['closed_at']}#{inc}", "qty": q, "kind": "consumption",
                "incident_id": inc})}})
    return items


# ---------- repuestos ----------

def _zones_by_distance(origin: str) -> list[str]:
    o = ZONE_COORDS.get(origin, ZONE_COORDS["CENTRO"])
    return sorted(ZONE_COORDS, key=lambda z: (z != origin, (ZONE_COORDS[z][0] - o[0]) ** 2
                                              + (ZONE_COORDS[z][1] - o[1]) ** 2))


def reserve(ddb, e: dict) -> dict:
    p, inc = e["payload"], e["incident_id"]
    reserved, missing = [], []
    for idx, line in enumerate(p["lines"]):
        part, qty = line["part_id"], int(line["qty"])
        rsv_sk = f"RSV#{e['event_id']}#{idx}"
        prior = store.get(ddb, f"INCIDENT#{inc}", rsv_sk)
        if prior:  # reintento: esta linea ya se reservo
            reserved.append({"reservation_id": f"{e['event_id']}#{idx}", "part_id": part,
                             "zone_id": prior["zone_id"], "qty": qty})
            continue
        done = False
        for zone in _zones_by_distance(p["zone_id"]):
            try:
                ddb.transact_write_items(TransactItems=[
                    {"Update": {"TableName": REGISTRY, "Key": key("STOCK", f"{part}#{zone}"),
                                "UpdateExpression": "SET reserved = reserved + :q, available = available - :q",
                                # DynamoDB no admite aritmetica en condiciones: available = on_hand - reserved
                                "ConditionExpression": "attribute_exists(PK) AND available >= :q",
                                "ExpressionAttributeValues": vals(q=qty)}},
                    {"Put": {"TableName": REGISTRY, "ConditionExpression": "attribute_not_exists(PK)",
                             "Item": ser({"PK": f"INCIDENT#{inc}", "SK": rsv_sk, "part_id": part, "zone_id": zone,
                                          "qty": qty, "status": "reserved", "created_at": e["occurred_at"]})}},
                ])
                reserved.append({"reservation_id": f"{e['event_id']}#{idx}", "part_id": part, "zone_id": zone,
                                 "qty": qty})
                done = True
                break
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "TransactionCanceledException":
                    raise
        if not done:
            missing.append({"part_id": part, "qty": qty})
            stock = store.get(ddb, "STOCK", f"{part}#{p['zone_id']}") or {}
            ddb.put_item(TableName=REGISTRY, Item=ser({
                "PK": "REORDER", "SK": f"{e['occurred_at']}#{part}#{p['zone_id']}", "part_id": part,
                "zone_id": p["zone_id"], "qty": qty + int(stock.get("reorder_point", 0)), "source": "shortage",
                "incident_id": inc, "created_at": e["occurred_at"]}))
    return {"reserved": reserved, "missing": missing, "reorder_suggested": bool(missing)}


def kpis(ddb, e: dict) -> None:
    day = e["payload"]["day"]
    for row in e["payload"]["machines"]:
        ddb.put_item(TableName=REGISTRY, Item=ser({"PK": f"KPI#{day}", "SK": row["machine_id"], **row}))


# ---------- entrada ----------

HANDLERS = {"incident.opened": on_opened, "incident.escalated": on_escalated,
            "incident.assigned": on_assigned, "incident.closed": on_closed}


def apply(ddb, event: dict) -> dict:
    etype = event["event_type"]
    if _already(ddb, event["event_id"]):
        return {"status": "duplicate", "reservation": None}
    reservation = None
    items = [_inbox_put(event)]
    if etype.startswith(("incident.", "part.")):
        items.append(_timeline_put(event))
    if etype in HANDLERS:
        items += HANDLERS[etype](ddb, event)
    elif etype == "part.reserve.requested":
        reservation = reserve(ddb, event)  # idempotente por linea; el inbox se marca al final
    elif etype == "kpi.daily":
        kpis(ddb, event)  # PutItem por clave: idempotente
    elif etype not in ("part.reserved", "part.shortage"):
        status = "ignored"  # maintenance.order.suggested, fuel.anomaly: incremento T18
        try:
            _transact(ddb, items)
        except Duplicate:
            return {"status": "duplicate", "reservation": None}
        return {"status": status, "reservation": None}
    try:
        _transact(ddb, items)
    except Duplicate:
        return {"status": "duplicate", "reservation": reservation}
    return {"status": "applied", "reservation": reservation}


def parse_body(raw: str) -> dict:
    body = json.loads(raw)
    if isinstance(body, dict) and body.get("Type") == "Notification" and "Message" in body:
        body = json.loads(body["Message"])
    return body
