"""Lambda `supply-api` (T40): rutas `/supply/*` en la HTTP API fp-api (payload v2) detras del autorizador JWT `cognito`.
API Gateway ya valido el token; aqui se leen las claims (fplib.authz): rol `custom:role` y parque por los grupos
`tenant-<id>`; `maintenance_head` (o el grupo `maintenance-head`) ve y gestiona todos los parques.

Rutas y roles (docs/superpowers/plans/2026-11-15-repuestos-abastecimiento.md, 5.4 y 6.2; las formas de respuesta son
las de gcp/console/app/mock_supply.py, que es lo que pinta la pestana Abastecimiento):
- `GET /supply/stock?tenant=&zone=&part=` (todos menos operator) -> `{stock: [...]}` con on_order, in_transit,
  safety_stock, rop, days_of_coverage y status (ok | low | stockout) (api/supply_calc.py).
- `GET /supply/suggestions?tenant=` (admin, supervisor, maintenance_head) -> `{suggestions}`: PO y TRF en
  `suggested` cuyo destino u origen es del parque.
- `GET /supply/purchase-orders?status=&tenant=` y `/{id}` (admin, supervisor, management, maintenance_head);
  `GET /supply/transfers?status=&tenant=` y `/{id}` (admin, supervisor, maintenance_head) -> `{purchase_orders}`,
  `{transfers}` o la PO/TRF con su `timeline`.
- `POST /supply/purchase-orders` (admin, maintenance_head) `{zone_id, part_id, qty, supplier_id, reason?}` y
  `POST /supply/transfers` (maintenance_head) `{from_zone, to_zone, part_id, qty, reason?}`: escriben el META en
  `suggested` (source manual) y arrancan `supply-flow` (`SUPPLY_FLOW_ARN`) con nombre de ejecucion = id. 201.
- `POST /supply/{purchase-orders|transfers}/{id}/approve | reject` (roles `approver_roles` del documento, tabla 5.4,
  y del parque destino salvo el jefe): toma la espera `approval` y llama SendTaskSuccess / SendTaskFailure con el
  `task_token` guardado (salida `{approved_by, role, override}`; rechazo con error `SupplyRejected` y causa
  `{reason, by}`). `approve` de una transferencia que dejaria al donante bajo su stock de seguridad -> 409
  salvo `{"override": true}`. `receive` (`receiver_roles` del documento; por defecto admin o technician del parque destino y
  maintenance_head): toma la espera
  `receipt` (status ordered | in_transit) y llama SendTaskSuccess. Responden la PO/TRF con el estado que deja la
  decision (`approved`, `cancelled`, `received`) y `flow_pending: true`: el flujo escribe el resto.
- `GET /supply/suppliers` (admin, maintenance_head) -> `{suppliers}` de supply.yaml con sus repuestos.
- `GET /supply/movements?zone=&part=&tenant=` (admin, maintenance_head) -> `{movements}` del ledger `MOVE#`.
- `GET /supply/kpis?tenant=` (admin, maintenance_head, management) -> parada evitada (h y USD), quiebres abiertos,
  valor de inventario por parque y fill rate.

Errores (como T36): 401 sin claims; 403 por rol (`FORBIDDEN`) o por `?tenant=` / zona de otro parque
(`TENANT_FORBIDDEN`), con log `supply_prohibido` (nunca el token); 404 para una PO/TRF de otro parque pedida por ID
(como si no existiera); 409 estado invalido (`INVALID_STATE`, `TASK_EXPIRED`, `ALREADY_EXISTS`,
`SOURCE_BELOW_SAFETY`); 400 cuerpo o filtro invalido; 502 si Step Functions no responde (la decision se deshace).
"""
from __future__ import annotations

import base64
import json
import os
import re
from datetime import timedelta
from types import SimpleNamespace

from botocore.exceptions import ClientError

from api import supply_calc as calc
from api import supply_store as store
from fplib import authz, clock, ids, log

ALL_PARK_ROLES = ("technician", "supervisor", "management", "admin", authz.GLOBAL_ROLE)
READ_ROLES = {
    "stock": ALL_PARK_ROLES,
    "suggestions": ("admin", "supervisor", authz.GLOBAL_ROLE),
    "purchase_order": ("admin", "supervisor", "management", authz.GLOBAL_ROLE),
    "transfer": ("admin", "supervisor", authz.GLOBAL_ROLE),
    "suppliers": ("admin", authz.GLOBAL_ROLE),
    "movements": ("admin", authz.GLOBAL_ROLE),
    "kpis": ("admin", "management", authz.GLOBAL_ROLE),
}
CREATE_ROLES = {"purchase_order": ("admin", authz.GLOBAL_ROLE), "transfer": (authz.GLOBAL_ROLE,)}
RECEIVE_ROLES = ("admin", "technician", authz.GLOBAL_ROLE)
KINDS = {"purchase-orders": "purchase_order", "transfers": "transfer"}
STATUSES = ("suggested", "approved", "rejected", "ordered", "in_transit", "received", "cancelled")
DOC_RE = re.compile(r"^(PO|TRF)-[A-Z0-9-]{4,80}$")
ZONE_RE = re.compile(r"^[A-Z0-9-]{2,20}$")
PART_RE = re.compile(r"^PRT-[0-9]{2}$")
MAX_QTY, MAX_REASON, MAX_USER = 1000, 300, 128
DEFAULT_FLOW_ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:supply-flow"


class HttpError(Exception):
    def __init__(self, status: int, body: dict):
        super().__init__(body.get("error", ""))
        self.status, self.body = status, body


def resp(code: int, body: dict) -> dict:
    return {"statusCode": code, "headers": {"content-type": "application/json", "cache-control": "no-store"},
            "body": json.dumps(calc.plain(body), default=str)}


def _role(event: dict) -> str | None:
    return authz.GLOBAL_ROLE if authz.is_global(event) else authz.role_of(event)


def _require(event: dict, roles) -> str:
    role = _role(event)
    if role not in roles:
        raise authz.Forbidden("se requiere uno de los roles: " + ", ".join(roles))
    return role


def _user(event: dict) -> str:
    return authz.user_of(event)[:MAX_USER] or (_role(event) or "")


def _qs(event: dict) -> dict:
    return event.get("queryStringParameters") or {}


def _scope(event: dict) -> tuple[str, ...]:
    return authz.scope(event, _qs(event).get("tenant"))


def _body(event: dict) -> dict:
    raw = event.get("body") or ""
    try:
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        out = json.loads(raw or "{}")
    except ValueError:
        raise HttpError(400, {"error": "BAD_BODY", "detail": "invalid JSON body"}) from None
    if not isinstance(out, dict):
        raise HttpError(400, {"error": "BAD_BODY", "detail": "body must be a JSON object"})
    return out


def _filters(event: dict) -> tuple[str | None, str | None]:
    qs = _qs(event)
    zone, part = qs.get("zone") or None, qs.get("part") or None
    if (zone and not ZONE_RE.match(zone)) or (part and not PART_RE.match(part)):
        raise HttpError(400, {"error": "BAD_FILTER"})
    return zone, part


def _not_found(doc_id: str) -> HttpError:
    return HttpError(404, {"error": "NOT_FOUND", "id": doc_id[:100]})


# ---------- lectura ----------

def _stock(deps, tenants, zone=None, part=None) -> list[dict]:
    parts = {p["part_id"]: p for p in ids.load()["parts"]}
    order = {z: i for i, z in enumerate(ids.zones())}
    rows = []
    for item in store.stock_items(deps.registry, tenants, zone, part):
        _, part_id, z = item["PK"].split("#", 2)
        rows.append(calc.stock_row(item, parts.get(part_id, {"part_id": part_id}),
                                   store.consumed_30d(deps.registry, part_id, z)))
    return sorted(rows, key=lambda r: (r["part_id"], order.get(r["zone_id"], 99)))


def get_stock(event, deps):
    _require(event, READ_ROLES["stock"])
    zone, part = _filters(event)
    return resp(200, {"stock": _stock(deps, _scope(event), zone, part)})


def _docs(deps, tenants, kind: str, status: str | None = None) -> list[dict]:
    prefix = "TRF" if kind == "transfer" else "PO"
    out = []
    for meta in store.list_metas(deps.registry, tenants, prefix):
        if status and meta.get("status") != status:
            continue
        doc_id = meta["PK"].split("#", 1)[1]
        doc = calc.public_doc(meta, store.events_of(deps.registry, doc_id))
        if calc.visible(doc, tenants):
            out.append(doc)
    return sorted(out, key=lambda d: d.get("created_at") or "", reverse=True)


def _status(event) -> str | None:
    status = _qs(event).get("status") or None
    if status and status not in STATUSES:
        raise HttpError(400, {"error": "BAD_FILTER", "allowed": list(STATUSES)})
    return status


def list_suggestions(event, deps):
    _require(event, READ_ROLES["suggestions"])
    tenants = _scope(event)
    rows = _docs(deps, tenants, "purchase_order", "suggested") + _docs(deps, tenants, "transfer", "suggested")
    return resp(200, {"suggestions": sorted(rows, key=lambda d: d.get("created_at") or "", reverse=True)})


def list_docs(kind: str):
    key = "purchase_orders" if kind == "purchase_order" else "transfers"

    def route(event, deps):
        _require(event, READ_ROLES[kind])
        status = _status(event)
        return resp(200, {key: _docs(deps, _scope(event), kind, status)})
    return route


def _load_doc(event, deps, kind: str) -> dict:
    """PO/TRF pedida por ID; 404 si no existe, es de otro tipo o no toca al parque del usuario."""
    doc_id = (event.get("pathParameters") or {}).get("id") or ""
    if not DOC_RE.match(doc_id) or calc.kind_of(doc_id) != kind:
        raise _not_found(doc_id)
    meta, events = store.get_doc(deps.registry, doc_id)
    if meta is None:
        raise _not_found(doc_id)
    doc = calc.public_doc(meta, events)
    own = authz.tenant_of(event)
    if own is not None and not calc.visible(doc, (own,)):
        log.event("supply_otro_parque", status=404, id=doc_id, tenant=doc["tenant_id"], role=_role(event))
        raise _not_found(doc_id)
    doc["_task_token"] = meta.get("task_token")
    return doc


def _out(doc: dict, **overlay) -> dict:
    return {k: v for k, v in doc.items() if k != "_task_token"} | overlay


def get_doc(kind: str):
    def route(event, deps):
        _require(event, READ_ROLES[kind])
        return resp(200, _out(_load_doc(event, deps, kind)))
    return route


def list_suppliers(event, deps):
    _require(event, READ_ROLES["suppliers"])
    cat = ids.supply()
    rows = [{**s, "parts": [{k: p[k] for k in ("part_id", "unit_price_usd", "lead_time_d", "min_qty")}
                            for p in cat.get("supplier_parts") or [] if p["supplier_id"] == s["supplier_id"]]}
            for s in cat.get("suppliers") or []]
    return resp(200, {"suppliers": rows})


def list_movements(event, deps):
    _require(event, READ_ROLES["movements"])
    zone, part = _filters(event)
    tenants = _scope(event)
    rows = []
    for item in store.stock_items(deps.registry, tenants, zone, part):
        _, part_id, z = item["PK"].split("#", 2)
        for m in store.movements(deps.registry, part_id, z):
            ts, _, ref = m["SK"].partition("#")
            rows.append({"part_id": part_id, "zone_id": z, "tenant_id": m.get("tenant_id") or ids.tenant_of_zone(z),
                         "kind": m.get("kind"), "qty": m.get("qty"), "ref": m.get("ref") or ref, "at": ts})
    rows.sort(key=lambda r: r["at"], reverse=True)
    return resp(200, {"movements": rows[:store.MAX_DOCS]})


def get_kpis(event, deps):
    _require(event, READ_ROLES["kpis"])
    tenants = _scope(event)
    docs = _docs(deps, tenants, "purchase_order") + _docs(deps, tenants, "transfer")
    return resp(200, calc.kpis(_stock(deps, tenants), docs, tenants))


# ---------- creacion manual ----------

def _qty(body: dict) -> int:
    qty = body.get("qty")
    if not isinstance(qty, int) or isinstance(qty, bool) or not 1 <= qty <= MAX_QTY:
        raise HttpError(400, {"error": "BAD_BODY", "detail": f"qty must be an integer 1..{MAX_QTY}"})
    return qty


def _zone(body: dict, key: str) -> str:
    zone = body.get(key)
    if not isinstance(zone, str) or zone not in ids.zones():
        raise HttpError(400, {"error": "BAD_BODY", "detail": f"unknown {key}"})
    return zone


def _part(body: dict) -> str:
    part = body.get("part_id")
    if not isinstance(part, str) or part not in {p["part_id"] for p in ids.load()["parts"]}:
        raise HttpError(400, {"error": "BAD_BODY", "detail": "unknown part_id"})
    return part


def _reason(body: dict, user: str) -> str:
    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise HttpError(400, {"error": "BAD_BODY", "detail": "reason must be text"})
    return (reason or f"Pedido manual de {user}")[:MAX_REASON]


def _start(deps, item: dict, kind: str) -> dict:
    """Escribe el META (id nuevo) y arranca supply-flow con nombre = id; si Step Functions falla, borra el META."""
    try:
        store.put_new(deps.registry, item)
    except store.ConditionFailed:
        raise HttpError(409, {"error": "ALREADY_EXISTS", "id": item["id"]}) from None
    arn = os.environ.get("SUPPLY_FLOW_ARN", DEFAULT_FLOW_ARN)
    try:
        deps.sfn.start_execution(stateMachineArn=arn, name=item["id"],
                                 input=json.dumps({"kind": "po" if kind == "purchase_order" else "transfer",
                                                   "id": item["id"]}))
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        store.delete(deps.registry, item["id"])
        log.error("supply_flujo_no_inicio", id=item["id"], error=code)
        if code == "ExecutionAlreadyExists":
            raise HttpError(409, {"error": "ALREADY_EXISTS", "id": item["id"]}) from None
        raise HttpError(502, {"error": "FLOW_UNAVAILABLE"}) from None
    log.event("supply_manual_creada", id=item["id"], kind=kind, tenant=item["tenant_id"], by=item["created_by"])
    return item


def _stamp() -> tuple[str, str]:
    now = clock.now()
    return clock.iso(now), now.strftime("%Y%m%dT%H%M")


def create_po(event, deps):
    _require(event, CREATE_ROLES["purchase_order"])
    body, user = _body(event), _user(event)
    zone, part, qty = _zone(body, "zone_id"), _part(body), _qty(body)
    tenant = ids.tenant_of_zone(zone)
    authz.require_tenant_access(event, tenant)
    offer = next((o for o in ids.supplier_parts(part) if o["supplier_id"] == body.get("supplier_id")), None)
    if offer is None:
        raise HttpError(400, {"error": "BAD_BODY", "detail": "supplier_id does not offer this part",
                              "allowed": [o["supplier_id"] for o in ids.supplier_parts(part)]})
    if qty < offer.get("min_qty", 1):
        raise HttpError(400, {"error": "BAD_BODY", "detail": f"qty below supplier min_qty {offer['min_qty']}"})
    now, stamp = _stamp()
    doc_id = f"PO-{zone}-{part}-{stamp}"
    total, eta_h = qty * offer["unit_price_usd"], offer["lead_time_d"] * 24
    item = {"PK": f"PO#{doc_id}", "SK": "META", "id": doc_id, "kind": "po", "status": "suggested",
            "tenant_id": tenant, "zone_id": zone, "part_id": part, "qty": qty, "supplier_id": offer["supplier_id"],
            "unit_price_usd": offer["unit_price_usd"], "total_usd": total, "cost_usd": total,
            "lead_time_d": offer["lead_time_d"], "eta_h": eta_h,
            "eta": clock.iso(clock.parse(now) + timedelta(hours=eta_h)), "source": "manual",
            "options": [{"source_kind": "purchase", "supplier_id": offer["supplier_id"], "eta_h": eta_h,
                         "cost_usd": total, "total_cost_usd": total}],
            "reason": _reason(body, user),
            "approver_roles": calc.approver_roles("purchase_order", total, ids.part_policy(part)["criticality"]),
            "downtime_avoided_h": 0, "downtime_avoided_usd": 0, "created_by": user, "created_at": now,
            "updated_at": now, "GSI2PK": "SUPPLY#suggested", "GSI2SK": now,
            "GSI3PK": f"TENANT#{tenant}", "GSI3SK": f"PO#{now}"}
    return resp(201, _out(calc.public_doc(_start(deps, item, "purchase_order"))))


def create_transfer(event, deps):
    _require(event, CREATE_ROLES["transfer"])
    body, user = _body(event), _user(event)
    src, dst, part, qty = _zone(body, "from_zone"), _zone(body, "to_zone"), _part(body), _qty(body)
    if src == dst:
        raise HttpError(400, {"error": "BAD_BODY", "detail": "from_zone and to_zone must differ"})
    tenant, from_tenant = ids.tenant_of_zone(dst), ids.tenant_of_zone(src)
    trip = calc.transit(src, dst)
    now, stamp = _stamp()
    doc_id = f"TRF-{src}-{dst}-{part}-{stamp}"
    item = {"PK": f"TRF#{doc_id}", "SK": "META", "id": doc_id, "kind": "transfer", "status": "suggested",
            "tenant_id": tenant, "zone_id": dst, "to_zone": dst, "from_zone": src, "from_tenant": from_tenant,
            "part_id": part, "qty": qty, "freight_usd": trip["freight_usd"], "cost_usd": trip["freight_usd"],
            "transit_h": trip["hours"], "eta_h": trip["hours"],
            "eta": clock.iso(clock.parse(now) + timedelta(hours=trip["hours"])), "source": "manual",
            "options": [{"source_kind": "transfer", "from_zone": src, "eta_h": trip["hours"],
                         "cost_usd": trip["freight_usd"], "total_cost_usd": trip["freight_usd"]}],
            "reason": _reason(body, user), "approver_roles": list(calc.HEAD_ONLY),
            "downtime_avoided_h": 0, "downtime_avoided_usd": 0, "created_by": user, "created_at": now,
            "updated_at": now, "GSI1PK": f"TRFFROM#{from_tenant}", "GSI1SK": now,
            "GSI2PK": "SUPPLY#suggested", "GSI2SK": now, "GSI3PK": f"TENANT#{tenant}", "GSI3SK": f"TRF#{now}"}
    return resp(201, _out(calc.public_doc(_start(deps, item, "transfer"))))


# ---------- decisiones ----------

def _own_destination(event, doc: dict, why: str) -> None:
    own = authz.tenant_of(event)
    if own is not None and own != doc["tenant_id"]:
        raise authz.TenantForbidden(why)


def _send(deps, doc: dict, waiting_for: str, call: str, **kwargs) -> None:
    """Despierta al flujo; token vencido -> 409 TASK_EXPIRED, otro error -> se devuelve la espera y 502."""
    try:
        getattr(deps.sfn, call)(taskToken=doc["_task_token"], **kwargs)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        log.error("supply_token_fallo", id=doc["id"], call=call, error=code)
        if code in ("TaskTimedOut", "InvalidToken", "TaskDoesNotExist"):
            raise HttpError(409, {"error": "TASK_EXPIRED", "detail": "el flujo ya no espera esta decision"}) from None
        store.restore_wait(deps.registry, doc["id"], waiting_for)
        raise HttpError(502, {"error": "FLOW_UNAVAILABLE"}) from None


def _invalid(doc: dict) -> HttpError:
    return HttpError(409, {"error": "INVALID_STATE", "detail": f"estado {doc.get('status')}",
                           "status": doc.get("status"), "waiting_for": doc.get("waiting_for")})


def _source_ok(deps, doc: dict) -> bool:
    """El donante queda sobre su stock de seguridad despues de despachar (condicion de Ship, 4.2)."""
    src = store.get_stock(deps.registry, doc["part_id"], doc["from_zone"]) or {}
    on_hand, reserved = int(src.get("on_hand", 0)), int(src.get("reserved", 0))
    available = int(src.get("available", on_hand - reserved))
    safety = int(src.get("safety_stock", ids.part_policy(doc["part_id"])["safety_stock"]))
    return available >= int(doc["qty"]) + safety


def decide(kind: str, action: str):
    def route(event, deps):
        doc = _load_doc(event, deps, kind)
        user = _user(event)
        if action == "receive":
            _require(event, tuple(doc.get("receiver_roles") or RECEIVE_ROLES))
            _own_destination(event, doc, "solo el parque destino confirma la recepcion")
            if doc.get("waiting_for") != "receipt" or doc.get("status") not in ("ordered", "in_transit") \
                    or not doc["_task_token"]:
                raise _invalid(doc)
            try:
                store.take_wait(deps.registry, doc["id"], "receipt", ("ordered", "in_transit"), doc["_task_token"],
                                received_by=user, received_at=clock.iso(clock.now()))
            except store.ConditionFailed:
                raise _invalid(doc) from None
            _send(deps, doc, "receipt", "send_task_success",
                  output=json.dumps({"received_by": user}))
            log.event("supply_recibida", id=doc["id"], by=user, role=_role(event))
            return resp(200, _out(doc, status="received", received_by=user, waiting_for=None, flow_pending=True))
        _require(event, tuple(doc["approver_roles"]))
        _own_destination(event, doc, "solo el parque destino decide")
        if doc.get("waiting_for") != "approval" or doc.get("status") != "suggested" or not doc["_task_token"]:
            raise _invalid(doc)
        body = _body(event) if event.get("body") else {}
        override = body.get("override") is True
        if action == "approve" and kind == "transfer" and not override and not _source_ok(deps, doc):
            raise HttpError(409, {"error": "SOURCE_BELOW_SAFETY",
                                  "detail": "el almacen de origen quedaria bajo su stock de seguridad"})
        fields = {"approved_by": user, "decided_at": clock.iso(clock.now())}
        if action == "approve" and override:
            fields["override"] = True
        if action == "reject":
            fields = {"rejected_by": user, "decided_at": fields["decided_at"]}
        try:
            store.take_wait(deps.registry, doc["id"], "approval", ("suggested",), doc["_task_token"], **fields)
        except store.ConditionFailed:
            raise _invalid(doc) from None
        if action == "approve":
            _send(deps, doc, "approval", "send_task_success",
                  output=json.dumps({"approved_by": user, "role": _role(event), "override": override}))
            log.event("supply_aprobada", id=doc["id"], by=user, role=_role(event), override=override)
            return resp(200, _out(doc, status="approved", approved_by=user, waiting_for=None, flow_pending=True))
        reason = body.get("reason") if isinstance(body.get("reason"), str) else "rechazada en la consola"
        _send(deps, doc, "approval", "send_task_failure", error="SupplyRejected",
              cause=json.dumps({"reason": reason[:MAX_REASON], "by": user}))
        log.event("supply_rechazada", id=doc["id"], by=user, role=_role(event))
        return resp(200, _out(doc, status="cancelled", rejected_by=user, waiting_for=None, flow_pending=True))
    return route


# ---------- entrada ----------

ROUTES = {
    "GET /supply/stock": get_stock,
    "GET /supply/suggestions": list_suggestions,
    "GET /supply/suppliers": list_suppliers,
    "GET /supply/movements": list_movements,
    "GET /supply/kpis": get_kpis,
    "POST /supply/purchase-orders": create_po,
    "POST /supply/transfers": create_transfer,
}
for _path, _kind in KINDS.items():
    ROUTES[f"GET /supply/{_path}"] = list_docs(_kind)
    ROUTES[f"GET /supply/{_path}/{{id}}"] = get_doc(_kind)
    for _action in ("approve", "reject", "receive"):
        ROUTES[f"POST /supply/{_path}/{{id}}/{_action}"] = decide(_kind, _action)


def handler(event, context=None, registry=None, sfn=None):
    route = event.get("routeKey", "")
    fn = ROUTES.get(route)
    if fn is None:
        return resp(404, {"error": "ROUTE_NOT_FOUND"})
    if not authz.claims(event):  # el authorizer no deja pasar sin token; por si la ruta se publica sin el
        log.error("supply_no_autorizado", status=401, route=route)
        return resp(401, {"error": "UNAUTHORIZED"})
    deps = SimpleNamespace(registry=registry or store.registry_table(), sfn=sfn)
    if deps.sfn is None and route.startswith("POST"):
        deps.sfn = store.sfn_client()
    try:
        return fn(event, deps)
    except authz.TenantForbidden as exc:
        log.event("supply_prohibido", status=403, route=route, role=_role(event), tenant=authz.tenant_of(event),
                  detail=str(exc), **(event.get("pathParameters") or {}))
        return resp(403, {"error": "TENANT_FORBIDDEN", "detail": str(exc)})
    except authz.Forbidden as exc:
        log.event("supply_prohibido", status=403, route=route, role=_role(event), tenant=authz.tenant_of(event),
                  detail=str(exc), **(event.get("pathParameters") or {}))
        return resp(403, {"error": "FORBIDDEN", "detail": str(exc)})
    except HttpError as exc:
        return resp(exc.status, exc.body)
