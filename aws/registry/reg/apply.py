"""Lambda registry: lo que le toca en fp-registry segun el modelo fijado en
docs/superpowers/plans/2026-11-14-aws-core-pipeline.md (seccion "Data model locked here").

- part.reserve.requested -> reserva atomica por linea en STOCK#<part>#<zone>/META (available >= qty; DynamoDB no
  admite aritmetica en condiciones, por eso se guarda available = on_hand - reserved), zona mas cercana, y si no hay:
  REORDER + part.shortage. Escribe part.reserved / part.shortage en fp-outbox junto con su EV# (misma transaccion).
- incident.closed -> consume lo reservado (RSV#<incident_id>) y descuenta lo usado; libera lo que no se uso.
- kpi.daily -> KPI#<day>/<machine_id>.
El META del incidente, sus EV# y los tecnicos los escribe incident-flow / la API de cierre (Benjamin), no esta Lambda.
Idempotencia: fp-inbox con message_id = "registry:<event_id>" en la misma transaccion que los cambios.
"""
from __future__ import annotations

import time
import uuid

from botocore.exceptions import ClientError
from fplib import clock, outbox

from reg import store
from reg.store import INBOX, OUTBOX, REGISTRY, dumps, key, ser, vals

HANDLED = ("part.reserve.requested", "incident.closed", "kpi.daily")
ZONE_COORDS = {"NORTE": (-13.9000, -71.5600), "CENTRO": (-13.9200, -71.5450), "SUR": (-13.9400, -71.5550)}


def inbox_id(event_id: str) -> str:
    return f"registry:{event_id}"


def _inbox_put(event: dict) -> dict:
    return {"Put": {"TableName": INBOX, "ConditionExpression": "attribute_not_exists(message_id)",
                    "Item": ser({"message_id": inbox_id(event["event_id"]), "consumer": "registry",
                                 "processed_at": clock.iso(clock.now()), "ttl": int(time.time()) + store.TTL_7D})}}


def _ev_put(e: dict) -> dict:
    return {"Put": {"TableName": REGISTRY, "Item": ser({
        "PK": f"INCIDENT#{e['incident_id']}", "SK": f"EV#{e['occurred_at']}#{e['event_id']}",
        # Misma forma que incident-flow (comentario de Benjamin en #9): payload = sobre completo, como en fp-outbox
        "event_type": e["event_type"], "source": e["source"], "payload": dumps(e)})}}


def _stock_update(part: str, zone: str, expr: str, cond: str | None = None, **values) -> dict:
    u = {"TableName": REGISTRY, "Key": key(f"STOCK#{part}#{zone}", "META"), "UpdateExpression": expr,
         "ExpressionAttributeValues": vals(**values)}
    if cond:
        u["ConditionExpression"] = cond
    return {"Update": u}


class Duplicate(Exception):
    pass


def _transact(ddb, items: list[dict]) -> None:
    """items[0] es siempre el claim de fp-inbox: si su condicion falla, el evento ya se aplico."""
    try:
        ddb.transact_write_items(TransactItems=items)
    except ClientError as exc:
        reasons = exc.response.get("CancellationReasons") or []
        if exc.response["Error"]["Code"] == "TransactionCanceledException" and reasons \
                and reasons[0].get("Code") == "ConditionalCheckFailed":
            raise Duplicate() from exc
        raise


def already_applied(ddb, event_id: str) -> bool:
    return "Item" in ddb.get_item(TableName=INBOX, Key={"message_id": {"S": inbox_id(event_id)}})


# ---------- reserva ----------

def zones_by_distance(origin: str) -> list[str]:
    o = ZONE_COORDS.get(origin, ZONE_COORDS["CENTRO"])
    return sorted(ZONE_COORDS, key=lambda z: (z != origin, (ZONE_COORDS[z][0] - o[0]) ** 2
                                              + (ZONE_COORDS[z][1] - o[1]) ** 2))


def reserve(ddb, e: dict) -> dict:
    p, inc = e["payload"], e["incident_id"]
    existing = store.query_all(ddb, TableName=REGISTRY, KeyConditionExpression="PK = :pk",
                               ExpressionAttributeValues=vals(pk=f"RSV#{inc}"))
    reserved, missing = [], []
    for idx, line in enumerate(p["lines"]):
        part, qty = line["part_id"], int(line["qty"])
        rid = f"{e['event_id']}#{idx}"
        prior = next((r for r in existing if r.get("reservation_id") == rid), None)
        if prior:  # reintento: esta linea ya se reservo
            reserved.append({"reservation_id": rid, "part_id": part, "zone_id": prior["zone_id"], "qty": qty})
            continue
        done = False
        for zone in zones_by_distance(p["zone_id"]):
            try:
                ddb.transact_write_items(TransactItems=[
                    _stock_update(part, zone, "SET reserved = reserved + :q, available = available - :q",
                                  "attribute_exists(PK) AND available >= :q", q=qty),
                    {"Put": {"TableName": REGISTRY, "ConditionExpression": "attribute_not_exists(PK)",
                             "Item": ser({"PK": f"RSV#{inc}", "SK": f"{part}#{zone}", "reservation_id": rid,
                                          "incident_id": inc, "part_id": part, "zone_id": zone, "qty": qty,
                                          "status": "reserved", "created_at": e["occurred_at"]})}},
                ])
                reserved.append({"reservation_id": rid, "part_id": part, "zone_id": zone, "qty": qty})
                done = True
                break
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "TransactionCanceledException":
                    raise
        if not done:
            missing.append({"part_id": part, "qty": qty})
            stock = store.get(ddb, f"STOCK#{part}#{p['zone_id']}", "META") or {}
            ddb.put_item(TableName=REGISTRY, Item=ser({
                "PK": f"REORDER#{part}#{p['zone_id']}", "SK": e["occurred_at"], "part_id": part,
                "zone_id": p["zone_id"], "qty": qty + int(stock.get("reorder_point", 0)), "source": "shortage",
                "incident_id": inc, "created_at": e["occurred_at"],
                "GSI1PK": "REORDERS", "GSI1SK": f"{e['occurred_at']}#{part}#{p['zone_id']}"}))
    return {"reserved": reserved, "missing": missing, "reorder_suggested": bool(missing)}


def follow_up_events(e: dict, reservation: dict) -> list[dict]:
    """part.reserved / part.shortage (contracts/part.schema.json)."""
    base = {"schema_version": 1, "source": "aws.registry", "aggregate_type": "part_reservation",
            "aggregate_id": e["aggregate_id"], "occurred_at": clock.iso(clock.now()),
            "incident_id": e["incident_id"], "machine_id": e["machine_id"]}
    out = []
    if reservation["reserved"]:
        out.append({"event_id": str(uuid.uuid4()), "event_type": "part.reserved", **base,
                    "payload": {"reservations": reservation["reserved"]}})
    if reservation["missing"]:
        out.append({"event_id": str(uuid.uuid4()), "event_type": "part.shortage", **base,
                    "payload": {"missing": reservation["missing"],
                                "reorder_suggested": reservation["reorder_suggested"]}})
    return out


# ---------- cierre ----------

def consume_items(ddb, e: dict) -> list[dict]:
    """Una transaccion no puede tocar dos veces el mismo item: se agrupa por (repuesto, zona)."""
    p, inc = e["payload"], e["incident_id"]
    rsv = store.query_all(ddb, TableName=REGISTRY, KeyConditionExpression="PK = :pk",
                          ExpressionAttributeValues=vals(pk=f"RSV#{inc}"))
    reserved = [r for r in rsv if r.get("status") == "reserved"]
    used: dict[tuple[str, str], int] = {}
    for line in p.get("parts_used") or []:
        k = (line["part_id"], line["zone_id"])
        used[k] = used.get(k, 0) + int(line["qty"])
    held: dict[tuple[str, str], int] = {}
    items = []
    for r in reserved:
        k = (r["part_id"], r["zone_id"])
        held[k] = held.get(k, 0) + int(r["qty"])
        status = "consumed" if k in used else "released"  # lo reservado que no se uso vuelve al stock libre
        items.append({"Update": {"TableName": REGISTRY, "Key": key(r["PK"], r["SK"]),
                                 "UpdateExpression": "SET #st = :s", "ExpressionAttributeNames": {"#st": "status"},
                                 "ExpressionAttributeValues": vals(s=status)}})
    for part, zone in sorted(set(used) | set(held)):
        q, h = used.get((part, zone), 0), held.get((part, zone), 0)
        items.append(_stock_update(part, zone,
                                   "SET on_hand = on_hand - :q, reserved = reserved - :h, available = available + :d",
                                   q=q, h=h, d=h - q))
        if q:
            items.append({"Put": {"TableName": REGISTRY, "Item": ser({
                "PK": f"MOVE#{part}#{zone}", "SK": f"{p['closed_at']}#{inc}", "qty": q, "kind": "consumption",
                "incident_id": inc})}})
    return items


# ---------- entrada ----------

def apply(ddb, event: dict) -> dict:
    etype = event["event_type"]
    if etype not in HANDLED:
        return {"status": "ignored", "follow_up": []}
    if already_applied(ddb, event["event_id"]):
        return {"status": "duplicate", "follow_up": []}
    items, follow = [_inbox_put(event)], []
    if etype == "part.reserve.requested":
        reservation = reserve(ddb, event)  # idempotente por linea (reservation_id = <event_id>#<linea>)
        follow = follow_up_events(event, reservation)
        for f in follow:
            items += [outbox.transact_items(f, table=OUTBOX), _ev_put(f)]
    elif etype == "incident.closed":
        items += consume_items(ddb, event)
    elif etype == "kpi.daily":
        day = event["payload"]["day"]
        for row in event["payload"]["machines"]:  # PutItem por clave: idempotente
            ddb.put_item(TableName=REGISTRY, Item=ser({"PK": f"KPI#{day}", "SK": row["machine_id"], **row}))
    try:
        _transact(ddb, items)
    except Duplicate:
        return {"status": "duplicate", "follow_up": []}
    return {"status": "applied", "follow_up": [f["event_type"] for f in follow]}
