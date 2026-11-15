"""API de lectura para la consola (HTTP API, payload v2). Header x-api-key = fleetpulse/api/console-key.

GET /machines, /machines/{id}, /incidents, /incidents/{id}, /stock, /reorders, /orders, /kpis/{day}
Claves segun el modelo fijado en docs/superpowers/plans/2026-11-14-aws-core-pipeline.md:
- fp-state MACHINE#<id>/META (GSI1 ZONE#<zona>/MACHINE#<id>): posicion y status = severidad (lo escriben ingest y score).
- fp-registry INCIDENT#<id>/META (GSI1 MACHINE#<id>/INC#<opened_at>, GSI2 STATUS#<open|closed>/<opened_at>), EV#, RSV#,
  STOCK#<part>#<zone>/META, REORDER# (GSI1 REORDERS), KPI#<day>.

Multi-tenancy (docs/superpowers/plans/2026-11-15-multi-tenancy.md): `?tenant=<id>[,<id>...]` acota cada ruta a esos
parques. Lo pone el BFF de la consola (unico dueno de la console-key) a partir del token del usuario ya validado por
`GET /whoami`; sin `tenant` responde el parque por defecto (mineria), asi una consola anterior al multi-tenancy sigue
viendo lo mismo. Maquinas y stock se leen por las zonas del parque (las zonas pertenecen a un parque); incidentes por
GSI3 `TENANT#<tenant>` (mas, en mineria, los incidentes viejos sin GSI3 de GSI2); pedidos, ordenes y KPIs se filtran
por el parque de su zona o maquina. Una maquina o un incidente de otro parque responde 404.
"""
from __future__ import annotations

import json
import logging
import os

from fplib import auth, ids

from reg import store
from reg.store import REGISTRY, STATE, vals

log = logging.getLogger()
log.setLevel(logging.INFO)

KEY_SECRET = os.environ.get("CONSOLE_KEY_SECRET", "fleetpulse/api/console-key")
HIDDEN = {"PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK", "GSI3PK", "GSI3SK", "baseline", "baseline_n"}
_ddb = None


def ddb():
    global _ddb
    if _ddb is None:
        _ddb = store.client()
    return _ddb


def resp(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"content-type": "application/json", "cache-control": "no-store"},
            "body": store.dumps(body)}


def clean(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in HIDDEN}


def tenants_of(qs: dict) -> tuple[str, ...]:
    """Parques pedidos en `?tenant=` (uno o varios separados por coma); sin parametro, el por defecto."""
    raw = (qs or {}).get("tenant") or ""
    wanted = tuple(dict.fromkeys(t for t in raw.split(",") if t)) or (ids.default_tenant(),)
    if any(t not in ids.tenants() for t in wanted):
        raise ValueError("unknown tenant")
    return wanted


def _zones(tenants) -> list[str]:
    return [z for t in tenants for z in ids.zones(t)]


# ---------- consultas ----------

def tenant_incidents(client, tenants) -> list[dict]:
    """Incidentes de los parques (GSI3 `TENANT#<t>` / `INC#`), mas reciente primero. En el parque por defecto se suman
    los incidentes anteriores al multi-tenancy (sin GSI3PK) que estan en GSI2, hasta correr scripts/backfill_tenant.py."""
    rows: dict[str, dict] = {}
    for t in tenants:
        for i in store.query_all(client, TableName=REGISTRY, IndexName="GSI3", ScanIndexForward=False,
                                 KeyConditionExpression="GSI3PK = :p AND begins_with(GSI3SK, :i)",
                                 ExpressionAttributeValues=vals(p=f"TENANT#{t}", i="INC#")):
            rows[i["PK"]] = i
    if ids.default_tenant() in tenants:
        for s in ("open", "closed"):
            for i in store.query_all(client, TableName=REGISTRY, IndexName="GSI2", ScanIndexForward=False,
                                     KeyConditionExpression="GSI2PK = :p", ExpressionAttributeValues=vals(p=f"STATUS#{s}")):
                if not i.get("GSI3PK") and ids.tenant_of_item(i) in tenants:
                    rows.setdefault(i["PK"], i)
    return sorted(rows.values(), key=lambda r: r.get("opened_at", ""), reverse=True)


def open_incidents(client, tenants=None) -> list[dict]:
    return [i for i in tenant_incidents(client, tenants or (ids.default_tenant(),)) if i.get("status") == "open"]


def list_machines(client, tenants=None) -> list[dict]:
    tenants = tenants or (ids.default_tenant(),)
    open_by_machine = {}
    for i in open_incidents(client, tenants):
        open_by_machine.setdefault(i["machine_id"], i["PK"].split("#", 1)[1])
    rows = []
    for zone in _zones(tenants):
        metas = store.query_all(client, TableName=STATE, IndexName="GSI1",
                                KeyConditionExpression="GSI1PK = :z AND begins_with(GSI1SK, :m)",
                                ExpressionAttributeValues=vals(z=f"ZONE#{zone}", m="MACHINE#"))
        for m in metas:
            mid = m.get("machine_id") or m["PK"].split("#", 1)[1]
            rows.append({
                "machine_id": mid, "model": m.get("model"), "zone_id": m.get("zone_id", zone),
                "lat": m.get("lat"), "lon": m.get("lon"),
                # ASUNCION: engine_status y fuel_remaining_ratio en META si ingest los guarda; si no, null.
                "status": m.get("engine_status"), "severity": m.get("status") or "ok", "score": m.get("score"),
                "hours_total": m.get("hours_total"), "fuel_remaining_ratio": m.get("fuel_remaining_ratio"),
                "open_incident_id": open_by_machine.get(mid), "updated_at": m.get("updated_at") or m.get("last_ts"),
                "tenant_id": ids.tenant_of_zone(zone),
            })
    return sorted(rows, key=lambda r: r["machine_id"])


def incident_brief(i: dict) -> dict:
    return clean(i) | {"incident_id": i["PK"].split("#", 1)[1], "tenant_id": ids.tenant_of_item(i)}


def machine_detail(client, mid: str, tenants=None) -> dict | None:
    meta = store.get(client, f"MACHINE#{mid}", "META", table=STATE)
    if not meta or ids.tenant_of_item(meta) not in (tenants or (ids.default_tenant(),)):
        return None
    incs = store.query_all(client, TableName=REGISTRY, IndexName="GSI1", ScanIndexForward=False,
                           KeyConditionExpression="GSI1PK = :p AND begins_with(GSI1SK, :i)",
                           ExpressionAttributeValues=vals(p=f"MACHINE#{mid}", i="INC#"))
    briefs = [incident_brief(i) for i in incs]
    opened = next((b["incident_id"] for b in briefs if b.get("status") == "open"), None)
    return clean(meta) | {"machine_id": mid, "tenant_id": ids.tenant_of_item(meta),
                          "severity": meta.get("status") or "ok",
                          "status": meta.get("engine_status"), "open_incident_id": opened,
                          "state": clean(meta), "incidents": briefs,
                          "recurrences": sum(1 for b in briefs if b.get("recurrence_flag"))}


def list_incidents(client, status: str | None, limit: int, tenants=None) -> list[dict]:
    parts = {"open": ["open"], "assigned": ["open"], "closed": ["closed"]}.get(status, ["open", "closed"])
    rows = [r for r in tenant_incidents(client, tenants or (ids.default_tenant(),)) if r.get("status") in parts]
    if status == "assigned":
        rows = [r for r in rows if r.get("tech_id")]
    rows.sort(key=lambda r: r.get("opened_at", ""), reverse=True)
    return [incident_brief(r) for r in rows[:limit]]


def timeline_entry(item: dict) -> dict:
    """EV# guarda el sobre completo en payload (JSON string) y event_type; se toleran items viejos (type + payload)."""
    raw = item.get("payload")
    body = json.loads(raw) if isinstance(raw, str) else (raw or {})
    envelope = body if isinstance(body, dict) and "event_type" in body and "payload" in body else None
    return {"type": item.get("event_type") or item.get("type") or (envelope or {}).get("event_type"),
            "source": item.get("source") or (envelope or {}).get("source"),
            "ts": item["SK"].split("#")[1],
            "payload": envelope["payload"] if envelope else body}


def incident_detail(client, inc: str, tenants=None) -> dict | None:
    items = store.query_all(client, TableName=REGISTRY, KeyConditionExpression="PK = :p",
                            ExpressionAttributeValues=vals(p=f"INCIDENT#{inc}"))
    meta = next((i for i in items if i["SK"] == "META"), None)
    if not meta or ids.tenant_of_item(meta) not in (tenants or (ids.default_tenant(),)):
        return None
    out = incident_brief(meta)
    out["timeline"] = sorted((timeline_entry(i) for i in items if i["SK"].startswith("EV#")), key=lambda t: t["ts"])
    rsv = store.query_all(client, TableName=REGISTRY, KeyConditionExpression="PK = :p",
                          ExpressionAttributeValues=vals(p=f"RSV#{inc}"))
    out["reservations"] = [{"reservation_id": r.get("reservation_id"), "part_id": r["part_id"],
                            "zone_id": r["zone_id"], "qty": r["qty"], "status": r["status"]} for r in rsv]
    return out


def list_stock(client, tenants=None) -> list[dict]:
    tenants = tenants or (ids.default_tenant(),)
    catalog = ids.load()
    parts = {p["part_id"]: p for p in catalog["parts"]}
    keys = [store.key(f"STOCK#{s['part_id']}#{z}", "META") for t in tenants
            for s in catalog["part_stock"] if (s.get("tenant_id") or ids.default_tenant()) == t for z in ids.zones(t)]
    found = []
    for i in range(0, len(keys), 100):
        pending = {REGISTRY: {"Keys": keys[i:i + 100]}}
        while pending:
            r = client.batch_get_item(RequestItems=pending)
            found += [store.des(x) for x in r.get("Responses", {}).get(REGISTRY, [])]
            pending = r.get("UnprocessedKeys") or None
    out = []
    for s in found:
        _, part, zone = s["PK"].split("#")
        avail = s.get("available", s["on_hand"] - s["reserved"])
        out.append({"part_id": part, "zone_id": zone, "tenant_id": ids.tenant_of_zone(zone),
                    "sku": parts[part]["sku"], "name": parts[part]["name"],
                    "on_hand": s["on_hand"], "reserved": s["reserved"], "available": avail,
                    "reorder_point": s["reorder_point"], "below_reorder": avail <= s["reorder_point"]})
    return sorted(out, key=lambda r: (r["part_id"], r["zone_id"]))


def _in(tenants, rows: list[dict]) -> list[dict]:
    tenants = tenants or (ids.default_tenant(),)
    return [r for r in rows if ids.tenant_of_item(r) in tenants]


def list_reorders(client, limit: int = 50, tenants=None) -> list[dict]:
    rows = store.query_all(client, TableName=REGISTRY, IndexName="GSI1", ScanIndexForward=False,
                           KeyConditionExpression="GSI1PK = :p", ExpressionAttributeValues=vals(p="REORDERS"))
    return [clean(r) for r in _in(tenants, rows)[:limit]]


def list_orders(client, limit: int = 50, tenants=None) -> list[dict]:
    rows = store.query_all(client, TableName=REGISTRY, IndexName="GSI1",
                           KeyConditionExpression="GSI1PK = :p", ExpressionAttributeValues=vals(p="ORDERS"))
    return [clean(r) for r in _in(tenants, rows)[:limit]]


def kpis(client, day: str, tenants=None) -> list[dict]:
    rows = store.query_all(client, TableName=REGISTRY, KeyConditionExpression="PK = :p",
                           ExpressionAttributeValues=vals(p=f"KPI#{day}"))
    rows = [{"machine_id": r["SK"], **r} for r in rows]  # SK = machine_id
    return [clean(r) for r in _in(tenants, rows)]


# ---------- entrada ----------

def handler(event, context=None, client=None, secrets=None):
    if not auth.check_api_key(event.get("headers") or {}, KEY_SECRET, client=secrets):
        return resp(401, {"error": "UNAUTHORIZED"})
    client = client or ddb()
    route = event.get("routeKey", "")
    params = event.get("pathParameters") or {}
    qs = event.get("queryStringParameters") or {}
    try:
        tenants = tenants_of(qs)
        if route == "GET /machines":
            return resp(200, {"machines": list_machines(client, tenants), "tenants": list(tenants)})
        if route == "GET /machines/{id}":
            d = machine_detail(client, params["id"], tenants)
            return resp(200, d) if d else resp(404, {"error": "MACHINE_NOT_FOUND"})
        if route == "GET /incidents":
            limit = max(1, min(int(qs.get("limit", 50)), 200))
            return resp(200, {"incidents": list_incidents(client, qs.get("status"), limit, tenants)})
        if route == "GET /incidents/{id}":
            d = incident_detail(client, params["id"], tenants)
            return resp(200, d) if d else resp(404, {"error": "INCIDENT_NOT_FOUND"})
        if route == "GET /stock":
            return resp(200, {"stock": list_stock(client, tenants)})
        if route == "GET /reorders":
            return resp(200, {"reorders": list_reorders(client, tenants=tenants)})
        if route == "GET /orders":
            return resp(200, {"orders": list_orders(client, tenants=tenants)})
        if route == "GET /kpis/{day}":
            return resp(200, {"day": params["day"], "machines": kpis(client, params["day"], tenants)})
    except ValueError:
        return resp(400, {"error": "BAD_REQUEST"})
    return resp(404, {"error": "ROUTE_NOT_FOUND"})
