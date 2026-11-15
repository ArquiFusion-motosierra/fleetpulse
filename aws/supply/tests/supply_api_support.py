"""Constantes, ayudas y dobles de las pruebas de la API `/supply/*` (T40): la historia de la demo de
gcp/console/app/mock_supply.py (EX-017 en NORTE espera la bomba PRT-04; transferencia desde agro FUNDO-A contra la
compra a HidraAndes; PO chica de filtros en SUR; PO de industria ya pedida) sembrada en un fp-registry de moto.

Nombre unico a proposito: CI corre las pruebas de todos los servicios en una sola sesion de pytest. Tampoco hay
conftest.py: el sys.path del servicio (api/) y de fplib se agrega aqui, al importar este modulo.
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

from botocore.exceptions import ClientError

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(HERE, ".."), os.path.join(HERE, "..", "..", "shared", "python")):
    _p = os.path.normpath(_p)
    if _p not in sys.path:
        sys.path.append(_p)

NOW = datetime(2026, 11, 15, 15, 0, 0, tzinfo=timezone.utc)
CREATED = "2026-11-15T14:40:00Z"
INC = "INC-EX-017-20261115T1430"
TRF = "TRF-FUNDO-A-NORTE-PRT-04-20261115T1440"
PO_SUR = "PO-SUR-PRT-01-20261115T1442"
PO_IND = "PO-PLANTA-1-PRT-08-20261113T1440"
TOKEN = "tok-secreto-no-loguear"


def claims(role, tenant=None, **extra):
    c = {"custom:role": role, "email": f"{role}.{tenant or 'all'}@example.com", "sub": f"sub-{role}"}
    if tenant:
        c["cognito:groups"] = f"[tenant-{tenant}]"
    return c | extra


HEAD = claims("maintenance_head")
ADMIN_MIN = claims("admin", "mineria")
ADMIN_AGRO = claims("admin", "agro")
ADMIN_IND = claims("admin", "industria")
SUP_MIN = claims("supervisor", "mineria")
MGMT_MIN = claims("management", "mineria")
TECH_MIN = claims("technician", "mineria", **{"custom:tech_id": "TEC-01"})
TECH_AGRO = claims("technician", "agro")
OPERATOR = claims("operator", "mineria")

# Claves que pinta la consola (MockSupply: _row, _add, kpis); la API debe devolver al menos estas.
STOCK_KEYS = {"part_id", "zone_id", "tenant_id", "on_hand", "reserved", "on_order", "in_transit", "reorder_point",
              "safety_stock", "criticality", "sku", "name", "available", "rop", "days_of_coverage", "status",
              "unit_cost_usd"}
DOC_KEYS = {"kind", "id", "zone_id", "tenant_id", "part_id", "qty", "eta_h", "cost_usd", "source", "options",
            "reason", "approver_roles", "downtime_avoided_h", "downtime_avoided_usd", "status", "created_at",
            "updated_at", "eta", "approved_by", "timeline"}
TRF_KEYS = DOC_KEYS | {"from_zone", "from_tenant"}
KPI_KEYS = {"downtime_avoided_h", "downtime_avoided_usd", "open_stockouts", "inventory_value_usd", "fill_rate"}


def make_registry(res):
    gsis = ("GSI1", "GSI2", "GSI3")
    attrs = [("PK", "S"), ("SK", "S")] + [(f"{g}{k}", "S") for g in gsis for k in ("PK", "SK")]
    return res.create_table(
        TableName="fp-registry", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": t} for a, t in attrs],
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        GlobalSecondaryIndexes=[{"IndexName": g, "Projection": {"ProjectionType": "ALL"},
                                 "KeySchema": [{"AttributeName": f"{g}PK", "KeyType": "HASH"},
                                               {"AttributeName": f"{g}SK", "KeyType": "RANGE"}]} for g in gsis])


def dec(o):
    return json.loads(json.dumps(o), parse_float=Decimal)


def seed_stock(reg):
    """Stock del catalogo (como seed.py de registry) con PRT-04 en 0 en mineria (perfil --demo-supply)."""
    from fplib import ids

    for s in ids.load()["part_stock"]:
        tenant = s.get("tenant_id") or ids.default_tenant()
        for zone in ids.zones(tenant):
            on_hand = 0 if (s["part_id"] == "PRT-04" and tenant == "mineria") else s["on_hand_per_zone"]
            reg.put_item(Item={"PK": f"STOCK#{s['part_id']}#{zone}", "SK": "META", "on_hand": on_hand, "reserved": 0,
                               "available": on_hand, "reorder_point": s["reorder_point"], "tenant_id": tenant})


def seed_doc(reg, doc_id, events=(), **attrs):
    is_trf = doc_id.startswith("TRF-")
    item = {"PK": ("TRF#" if is_trf else "PO#") + doc_id, "SK": "META", "created_at": CREATED, **attrs}
    item["GSI3PK"], item["GSI3SK"] = f"TENANT#{item['tenant_id']}", ("TRF#" if is_trf else "PO#") + item["created_at"]
    if is_trf:
        item["GSI1PK"], item["GSI1SK"] = f"TRFFROM#{item['from_tenant']}", item["created_at"]
    reg.put_item(Item=dec(item))
    for i, (etype, at) in enumerate(events):
        env = {"event_type": etype, "occurred_at": at, "source": "aws.supply", "payload": {"status": "x"}}
        reg.put_item(Item={"PK": item["PK"], "SK": f"EV#{at}#ev{i}", "event_type": etype, "payload": json.dumps(env)})


def seed_demo(reg):
    """Las tres ordenes de mock_supply.py con los campos del contrato con T39."""
    options = [{"source_kind": "transfer", "from_zone": "FUNDO-A", "eta_h": 16, "cost_usd": 180,
                "total_cost_usd": 180 + 16 * 450},
               {"source_kind": "purchase", "supplier_id": "SUP-HIDRA", "eta_h": 168, "cost_usd": 6800,
                "total_cost_usd": 6800 + 168 * 450}]
    seed_doc(reg, TRF, status="suggested", waiting_for="approval", task_token=TOKEN, tenant_id="mineria",
             zone_id="NORTE", to_zone="NORTE", from_zone="FUNDO-A", from_tenant="agro", part_id="PRT-04", qty=1,
             freight_usd=180, transit_h=16, eta="2026-11-16T06:40:00Z", source="shortage", incident_id=INC,
             machine_id="EX-017", options=options, reason="La bomba esta en 0 en mina; agro FUNDO-A tiene 3.",
             approver_roles=["maintenance_head"], downtime_avoided_h=152, downtime_avoided_usd=68400,
             events=[("supply.suggested", CREATED)])
    seed_doc(reg, PO_SUR, status="suggested", waiting_for="approval", task_token=TOKEN + "-sur",
             tenant_id="mineria", zone_id="SUR", part_id="PRT-01", qty=10, supplier_id="SUP-FILTR",
             unit_price_usd=42, total_usd=420, lead_time_d=2, source="planning", created_at="2026-11-15T14:42:00Z",
             options=[], reason="Preventivos de 14 dias.", approver_roles=["admin", "supervisor", "maintenance_head"])
    seed_doc(reg, PO_IND, status="ordered", waiting_for="receipt", task_token=TOKEN + "-ind",
             tenant_id="industria", zone_id="PLANTA-1", part_id="PRT-08", qty=3, supplier_id="SUP-MOTOR",
             unit_price_usd=95, total_usd=285, lead_time_d=5, source="manual", created_at="2026-11-13T14:40:00Z",
             updated_at="2026-11-13T15:11:00Z", approved_by="jefe@example.com", downtime_avoided_h=4,
             downtime_avoided_usd=600, approver_roles=["admin", "supervisor", "maintenance_head"],
             events=[("supply.suggested", "2026-11-13T14:40:00Z"), ("supply.approved", "2026-11-13T15:10:00Z"),
                     ("po.ordered", "2026-11-13T15:11:00Z")])


def api_event(route, claims_=None, body=None, path=None, qs=None):
    ev = {"routeKey": route, "pathParameters": path, "queryStringParameters": qs,
          "body": json.dumps(body) if isinstance(body, dict) else body, "isBase64Encoded": False}
    if claims_ is not None:
        ev["requestContext"] = {"authorizer": {"jwt": {"claims": claims_}}}
    return ev


def body_of(resp):
    return json.loads(resp["body"])


def log_lines(caplog, msg):
    out = []
    for r in caplog.records:
        try:
            line = json.loads(r.getMessage())
        except ValueError:
            continue
        if line.get("msg") == msg:
            out.append(line)
    return out


def capture_logs(caplog):
    caplog.set_level(logging.INFO, logger="fplib")


class FakeSfn:
    """Step Functions sin red: registra cada llamada; `fail = {"send_task_success": "TaskTimedOut"}` la hace fallar."""

    def __init__(self, fail=None):
        self.calls, self.fail = [], dict(fail or {})

    def _call(self, name, kwargs):
        if name in self.fail:
            raise ClientError({"Error": {"Code": self.fail[name], "Message": "x"}}, name)
        self.calls.append((name, kwargs))
        return {}

    def start_execution(self, **kw):
        out = self._call("start_execution", kw)
        return out | {"executionArn": f"{kw['stateMachineArn']}:{kw['name']}".replace("stateMachine", "execution")}

    def send_task_success(self, **kw):
        return self._call("send_task_success", kw)

    def send_task_failure(self, **kw):
        return self._call("send_task_failure", kw)
