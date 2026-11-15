"""API de lectura para la consola (HTTP API, payload v2). Header x-api-key = fleetpulse/api/console-key.

GET /machines, /machines/{id}, /incidents, /incidents/{id}, /stock, /reorders, /kpis/{day}
Estado y posicion salen de fp-state (los escribe ingest); severidad, incidentes y stock de fp-registry.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import time

from reg import store
from reg.store import REGISTRY, STATE, vals

log = logging.getLogger()
log.setLevel(logging.INFO)

KEY_SECRET = os.environ.get("CONSOLE_KEY_SECRET", "fleetpulse/api/console-key")
_cache: dict[str, tuple[float, object]] = {}
_ddb = None


def ddb():
    global _ddb
    if _ddb is None:
        _ddb = store.client()
    return _ddb


def api_key(secrets=None) -> str:
    hit = _cache.get("key")
    if hit and time.monotonic() - hit[0] < 300:
        return hit[1]
    if secrets is None:
        import boto3
        secrets = boto3.client("secretsmanager")
    value = json.loads(secrets.get_secret_value(SecretId=KEY_SECRET)["SecretString"])["api_key"]
    _cache["key"] = (time.monotonic(), value)
    return value


def resp(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"content-type": "application/json", "cache-control": "no-store"},
            "body": store.dumps(body)}


# ---------- consultas ----------

def registry_machines(client) -> list[dict]:
    return store.query_all(client, TableName=REGISTRY, IndexName="GSI1",
                           KeyConditionExpression="GSI1PK = :p", ExpressionAttributeValues=vals(p="MACHINES"))


def state_machines(client, ids: list[str]) -> dict[str, dict]:
    # ASUNCION: fp-state usa claves PK/SK = MACHINE#<id>/META con atributos zone, lat, lon, status,
    # hours_total, fuel_remaining_ratio, updated_at (documento de diseno). Si faltan, se usa la zona del seed.
    out = {}
    for i in range(0, len(ids), 100):
        keys = [store.key(f"MACHINE#{m}", "META") for m in ids[i:i + 100]]
        pending = {STATE: {"Keys": keys}}
        while pending:
            r = client.batch_get_item(RequestItems=pending)
            for item in r.get("Responses", {}).get(STATE, []):
                d = store.des(item)
                out[d["PK"].split("#", 1)[1]] = d
            pending = r.get("UnprocessedKeys") or None
    return out


def list_machines(client) -> list[dict]:
    reg = registry_machines(client)
    state = state_machines(client, [m["PK"].split("#", 1)[1] for m in reg])
    rows = []
    for m in reg:
        mid = m["PK"].split("#", 1)[1]
        s = state.get(mid, {})
        rows.append({
            "machine_id": mid, "model": m.get("model"), "zone_id": s.get("zone") or m.get("zone_id"),
            "lat": s.get("lat", m.get("lat")), "lon": s.get("lon", m.get("lon")),
            "status": s.get("status", "off"), "severity": m.get("severity", "ok"), "score": m.get("score"),
            "hours_total": s.get("hours_total", m.get("hours_total")),
            "fuel_remaining_ratio": s.get("fuel_remaining_ratio"),
            "open_incident_id": m.get("open_incident_id"), "updated_at": s.get("updated_at"),
        })
    return sorted(rows, key=lambda r: r["machine_id"])


def clean_incident(i: dict) -> dict:
    return {k: v for k, v in i.items() if k not in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK")} | {
        "incident_id": i["PK"].split("#", 1)[1]}


def machine_detail(client, mid: str) -> dict | None:
    m = store.get(client, f"MACHINE#{mid}", "META")
    if not m:
        return None
    incs = store.query_all(client, TableName=REGISTRY, IndexName="GSI2", ScanIndexForward=False,
                           KeyConditionExpression="GSI2PK = :p", ExpressionAttributeValues=vals(p=f"MACHINE#{mid}"))
    s = state_machines(client, [mid]).get(mid, {})
    detail = {k: v for k, v in m.items() if k not in ("PK", "SK", "GSI1PK", "GSI1SK")}
    detail.update({"machine_id": mid, "state": {k: v for k, v in s.items() if k not in ("PK", "SK")},
                   "incidents": [clean_incident(i) for i in incs],
                   "recurrences": sum(1 for i in incs if i.get("recurrence_flag"))})
    return detail


def list_incidents(client, status: str | None, limit: int) -> list[dict]:
    kwargs = {"TableName": REGISTRY, "IndexName": "GSI1", "ScanIndexForward": False,
              "KeyConditionExpression": "GSI1PK = :p", "ExpressionAttributeValues": vals(p="INCIDENTS")}
    if status:
        kwargs["FilterExpression"] = "#st = :s"
        kwargs["ExpressionAttributeNames"] = {"#st": "status"}
        kwargs["ExpressionAttributeValues"] = vals(p="INCIDENTS", s=status)
    return [clean_incident(i) for i in store.query_all(client, **kwargs)[:limit]]


def incident_detail(client, inc: str) -> dict | None:
    items = store.query_all(client, TableName=REGISTRY, KeyConditionExpression="PK = :p",
                            ExpressionAttributeValues=vals(p=f"INCIDENT#{inc}"))
    meta = next((i for i in items if i["SK"] == "META"), None)
    if not meta:
        return None
    out = clean_incident(meta)
    out["timeline"] = [{"type": i["type"], "source": i.get("source"), "ts": i["SK"].split("#")[1],
                        "payload": json.loads(i["payload"])} for i in items if i["SK"].startswith("EVT#")]
    out["reservations"] = [{"reservation_id": i["SK"][4:], "part_id": i["part_id"], "zone_id": i["zone_id"],
                            "qty": i["qty"], "status": i["status"]} for i in items if i["SK"].startswith("RSV#")]
    return out


def list_stock(client) -> list[dict]:
    rows = store.query_all(client, TableName=REGISTRY, KeyConditionExpression="PK = :p",
                           ExpressionAttributeValues=vals(p="STOCK"))
    out = []
    for r in rows:
        part, zone = r["SK"].split("#")
        avail = r.get("available", r["on_hand"] - r["reserved"])
        out.append({"part_id": part, "zone_id": zone, "sku": r.get("sku"), "name": r.get("name"),
                    "on_hand": r["on_hand"], "reserved": r["reserved"], "available": avail,
                    "reorder_point": r["reorder_point"], "below_reorder": avail <= r["reorder_point"]})
    return out


def list_reorders(client, limit: int = 50) -> list[dict]:
    rows = store.query_all(client, TableName=REGISTRY, KeyConditionExpression="PK = :p", ScanIndexForward=False,
                           ExpressionAttributeValues=vals(p="REORDER"))
    return [{k: v for k, v in r.items() if k not in ("PK", "SK")} for r in rows[:limit]]


def kpis(client, day: str) -> list[dict]:
    rows = store.query_all(client, TableName=REGISTRY, KeyConditionExpression="PK = :p",
                           ExpressionAttributeValues=vals(p=f"KPI#{day}"))
    return [{k: v for k, v in r.items() if k not in ("PK", "SK")} for r in rows]


# ---------- entrada ----------

def handler(event, context=None, client=None, secrets=None):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not hmac.compare_digest(headers.get("x-api-key", ""), api_key(secrets)):
        return resp(401, {"error": "UNAUTHORIZED"})
    client = client or ddb()
    route = event.get("routeKey", "")
    params = event.get("pathParameters") or {}
    qs = event.get("queryStringParameters") or {}
    try:
        if route == "GET /machines":
            return resp(200, {"machines": list_machines(client)})
        if route == "GET /machines/{id}":
            d = machine_detail(client, params["id"])
            return resp(200, d) if d else resp(404, {"error": "MACHINE_NOT_FOUND"})
        if route == "GET /incidents":
            limit = max(1, min(int(qs.get("limit", 50)), 200))
            return resp(200, {"incidents": list_incidents(client, qs.get("status"), limit)})
        if route == "GET /incidents/{id}":
            d = incident_detail(client, params["id"])
            return resp(200, d) if d else resp(404, {"error": "INCIDENT_NOT_FOUND"})
        if route == "GET /stock":
            return resp(200, {"stock": list_stock(client)})
        if route == "GET /reorders":
            return resp(200, {"reorders": list_reorders(client)})
        if route == "GET /kpis/{day}":
            return resp(200, {"day": params["day"], "machines": kpis(client, params["day"])})
    except ValueError:
        return resp(400, {"error": "BAD_REQUEST"})
    return resp(404, {"error": "ROUTE_NOT_FOUND"})
