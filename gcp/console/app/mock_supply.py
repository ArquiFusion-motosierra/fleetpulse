"""Abastecimiento en modo de prueba (T41): la historia de la demo sin AWS, con la forma y las reglas de la API
`/supply/*` del diseno (docs/superpowers/plans/2026-11-15-repuestos-abastecimiento.md, 5.3, 5.4 y 6.2).

EX-017 (mineria, NORTE) espera la bomba PRT-04, que esta en 0 en los 3 almacenes de mina y en 3 en agro. Hay una
sugerencia de transferencia desde FUNDO-A (16 h, USD 180) contra la compra a HidraAndes (7 d, USD 6.800), una orden
de compra chica de filtros que aprueba el admin del parque y una orden de industria ya pedida.
Se usa con MOCK=1 o, con la consola en modo AWS, con SUPPLY_MOCK=1 mientras la API /supply no este desplegada.
"""
from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timedelta, timezone

from app.mock_reports import BadRequest, Conflict, Forbidden, NotFound

# Copia de contracts/supply.yaml (T37) con lo que usa la consola; tests/test_console.py la compara si existe.
with open(os.path.join(os.path.dirname(__file__), "supply.json"), encoding="utf-8") as _f:
    SUPPLY = json.load(_f)
with open(os.path.join(os.path.dirname(__file__), "fleet.json"), encoding="utf-8") as _f:
    FLEET = json.load(_f)

KINDS = {"purchase-orders": "purchase_order", "transfers": "transfer"}
READ_ROLES = {
    "stock": ("technician", "supervisor", "management", "admin", "maintenance_head"),
    "suggestions": ("admin", "supervisor", "maintenance_head"),
    "purchase_order": ("admin", "supervisor", "management", "maintenance_head"),
    "transfer": ("admin", "supervisor", "maintenance_head"),
    "suppliers": ("admin", "maintenance_head"),
    "kpis": ("admin", "maintenance_head", "management"),
}
RECEIVE_ROLES = ("admin", "technician", "maintenance_head")
INCIDENT = "INC-EX-017-20261115T1430"
DEMO_DOWNTIME_USD_H = SUPPLY["downtime_cost_usd_h"]["EX"]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(d: datetime) -> str:
    return d.isoformat().replace("+00:00", "Z")


def _require(user: dict, roles: tuple[str, ...]) -> None:
    if user.get("role") not in roles:
        raise Forbidden(f"requiere rol: {', '.join(roles)}")


class MockSupply:
    def __init__(self, incident_id: str = INCIDENT):
        """`incident_id`: el incidente abierto de EX-017 en el mock de incidentes (MOCK=1), para que el enlace abra."""
        self.zone_tenant = {z["zone_id"]: z["tenant_id"] for z in FLEET["zones"]}
        self.parts = {p["part_id"]: p for p in FLEET["parts"]}
        policy = {p["part_id"]: p for p in SUPPLY["part_policy"]}
        self.stock: dict[tuple[str, str], dict] = {}
        for s in FLEET["part_stock"]:
            pol = policy.get(s["part_id"], {"criticality": "C", "safety_stock": 0})
            for zone, tenant in self.zone_tenant.items():
                if tenant != s["tenant_id"]:
                    continue
                demo_zero = s["part_id"] == "PRT-04" and tenant == "mineria"  # perfil --demo-supply (T37)
                self.stock[(s["part_id"], zone)] = {
                    "part_id": s["part_id"], "zone_id": zone, "tenant_id": tenant,
                    "on_hand": 0 if demo_zero else s["on_hand_per_zone"], "reserved": 0, "on_order": 0, "in_transit": 0,
                    "reorder_point": s["reorder_point"], "safety_stock": pol["safety_stock"],
                    "criticality": pol["criticality"]}
        self.moves: list[dict] = []
        self.docs: dict[str, dict] = {}
        t0 = _now() - timedelta(minutes=20)
        stamp = t0.strftime("%Y%m%dT%H%M")
        prt04 = _cheapest("PRT-04")
        transit = next(t for t in SUPPLY["transit_h"] if {t["from"], t["to"]} == {"mineria", "agro"})
        buy_h = prt04["lead_time_d"] * 24
        options = [
            {"source_kind": "transfer", "from_zone": "FUNDO-A", "eta_h": transit["hours"], "cost_usd": transit["freight_usd"],
             "total_cost_usd": transit["freight_usd"] + transit["hours"] * DEMO_DOWNTIME_USD_H},
            {"source_kind": "purchase", "supplier_id": prt04["supplier_id"], "eta_h": buy_h, "cost_usd": prt04["unit_price_usd"],
             "total_cost_usd": prt04["unit_price_usd"] + buy_h * DEMO_DOWNTIME_USD_H}]
        avoided = buy_h - transit["hours"]
        self._add({"kind": "transfer", "id": f"TRF-FUNDO-A-NORTE-PRT-04-{stamp}", "from_zone": "FUNDO-A",
                   "from_tenant": "agro", "zone_id": "NORTE", "tenant_id": "mineria", "part_id": "PRT-04", "qty": 1,
                   "eta_h": transit["hours"], "cost_usd": transit["freight_usd"], "source": "shortage",
                   "incident_id": incident_id, "machine_id": "EX-017", "options": options,
                   "reason": "La bomba esta en 0 en los 3 almacenes de mina; agro FUNDO-A tiene 3 y queda sobre su stock de seguridad.",
                   "approver_roles": ["maintenance_head"], "downtime_avoided_h": avoided,
                   "downtime_avoided_usd": avoided * DEMO_DOWNTIME_USD_H}, t0)
        prt01 = _cheapest("PRT-01")
        qty = max(prt01["min_qty"], 10)
        self._add({"kind": "purchase_order", "id": f"PO-SUR-PRT-01-{stamp}", "zone_id": "SUR", "tenant_id": "mineria",
                   "part_id": "PRT-01", "qty": qty, "supplier_id": prt01["supplier_id"], "eta_h": prt01["lead_time_d"] * 24,
                   "cost_usd": qty * prt01["unit_price_usd"], "unit_price_usd": prt01["unit_price_usd"], "source": "planning",
                   "options": [{"source_kind": "purchase", "supplier_id": prt01["supplier_id"],
                                "eta_h": prt01["lead_time_d"] * 24, "cost_usd": qty * prt01["unit_price_usd"],
                                "total_cost_usd": qty * prt01["unit_price_usd"]}],
                   "reason": "Preventivos de los proximos 14 dias dejan SUR bajo su punto de reorden.",
                   "approver_roles": ["admin", "supervisor", "maintenance_head"],
                   "downtime_avoided_h": 0, "downtime_avoided_usd": 0}, t0 + timedelta(minutes=2))
        prt08 = _cheapest("PRT-08")
        po = self._add({"kind": "purchase_order", "id": f"PO-PLANTA-1-PRT-08-{stamp}", "zone_id": "PLANTA-1",
                        "tenant_id": "industria", "part_id": "PRT-08", "qty": 3, "supplier_id": prt08["supplier_id"],
                        "eta_h": prt08["lead_time_d"] * 24, "cost_usd": 3 * prt08["unit_price_usd"],
                        "unit_price_usd": prt08["unit_price_usd"], "source": "manual", "options": [],
                        "reason": "Pedido manual del admin de industria.", "approver_roles": ["admin", "supervisor", "maintenance_head"],
                        "downtime_avoided_h": 0, "downtime_avoided_usd": 0}, t0 - timedelta(days=2))
        self._advance(po, "approved", "maintenance_head", t0 - timedelta(days=2) + timedelta(minutes=30))
        self._advance(po, "ordered", "supply-flow", t0 - timedelta(days=2) + timedelta(minutes=31))
        self.stock[("PRT-08", "PLANTA-1")]["on_order"] += 3

    # ---- internos ----
    def _add(self, d: dict, at: datetime) -> dict:
        d |= {"status": "suggested", "created_at": _iso(at), "updated_at": _iso(at),
              "eta": _iso(at + timedelta(hours=d["eta_h"])), "approved_by": None,
              "timeline": [{"type": "supply.suggested", "at": _iso(at), "by": "supply-sourcing"}]}
        self.docs[d["id"]] = d
        return d

    def _advance(self, d: dict, status: str, by: str, at: datetime | None = None) -> None:
        at = at or _now()
        d["status"], d["updated_at"] = status, _iso(at)
        d["timeline"].append({"type": f"supply.{status}", "at": _iso(at), "by": by})

    def _move(self, part, zone, kind, qty, ref):
        self.moves.append({"part_id": part, "zone_id": zone, "tenant_id": self.zone_tenant[zone], "kind": kind,
                           "qty": qty, "ref": ref, "at": _iso(_now())})

    @staticmethod
    def _visible(d: dict, tenants: list[str]) -> bool:
        return d["tenant_id"] in tenants or d.get("from_tenant") in tenants

    def _doc(self, user, tenants, kind, doc_id) -> dict:
        d = self.docs.get(doc_id)
        if not d or d["kind"] != kind or not self._visible(d, tenants):
            raise NotFound("not found")  # de otro parque: como si no existiera
        return d

    def _row(self, s: dict) -> dict:
        part = self.parts[s["part_id"]]
        avail = s["on_hand"] - s["reserved"]
        rop = max(s["reorder_point"], s["safety_stock"])
        demand_d = max(rop, 1) / 7  # ASUNCION: demanda diaria de prueba = punto de reorden en una semana
        status = "stockout" if avail <= 0 else "low" if avail <= rop else "ok"
        return {**s, "sku": part["sku"], "name": part["name"], "available": avail, "rop": rop,
                "days_of_coverage": round(avail / demand_d, 1), "status": status,
                "unit_cost_usd": part.get("unit_cost_usd", 0)}

    @staticmethod
    def _public(d: dict) -> dict:
        return copy.deepcopy(d)

    # ---- lectura ----
    def list_stock(self, user, tenants, zone=None, part=None):
        _require(user, READ_ROLES["stock"])
        rows = [self._row(s) for s in self.stock.values() if s["tenant_id"] in tenants
                and (not zone or s["zone_id"] == zone) and (not part or s["part_id"] == part)]
        order = list(self.zone_tenant)  # orden del catalogo: mineria, agro, industria
        return sorted(rows, key=lambda r: (r["part_id"], order.index(r["zone_id"])))

    def suggestions(self, user, tenants):
        _require(user, READ_ROLES["suggestions"])
        rows = [d for d in self.docs.values() if d["status"] == "suggested" and self._visible(d, tenants)]
        return [self._public(d) for d in sorted(rows, key=lambda d: d["created_at"], reverse=True)]

    def list_docs(self, user, tenants, kind, status=None):
        _require(user, READ_ROLES[kind])
        rows = [d for d in self.docs.values() if d["kind"] == kind and self._visible(d, tenants)
                and (not status or d["status"] == status)]
        return [self._public(d) for d in sorted(rows, key=lambda d: d["created_at"], reverse=True)]

    def get_doc(self, user, tenants, kind, doc_id):
        _require(user, READ_ROLES[kind])
        return self._public(self._doc(user, tenants, kind, doc_id))

    def suppliers(self, user):
        _require(user, READ_ROLES["suppliers"])
        return [{**s, "parts": [{k: p[k] for k in ("part_id", "unit_price_usd", "lead_time_d", "min_qty")}
                                for p in SUPPLY["supplier_parts"] if p["supplier_id"] == s["supplier_id"]]}
                for s in SUPPLY["suppliers"]]

    def kpis(self, user, tenants):
        _require(user, READ_ROLES["kpis"])
        done = [d for d in self.docs.values() if d["tenant_id"] in tenants and d["status"] in ("approved", "ordered",
                                                                                               "in_transit", "received")]
        rows = [self._row(s) for s in self.stock.values() if s["tenant_id"] in tenants]
        value = {t: round(sum(r["on_hand"] * r["unit_cost_usd"] for r in rows if r["tenant_id"] == t)) for t in tenants}
        return {"downtime_avoided_h": sum(d["downtime_avoided_h"] for d in done),
                "downtime_avoided_usd": sum(d["downtime_avoided_usd"] for d in done),
                "open_stockouts": sum(1 for r in rows if r["status"] == "stockout"),
                "inventory_value_usd": value, "fill_rate": 0.92}  # ASUNCION: fill rate fijo en el mock

    # ---- acciones ----
    def act(self, user, tenants, kind_path, doc_id, action, body=None):
        kind = KINDS.get(kind_path)
        if not kind or action not in ("approve", "reject", "receive"):
            raise BadRequest("bad action")
        d = self._doc(user, tenants, kind, doc_id)
        by = user.get("email") or user["role"]
        if action in ("approve", "reject"):
            _require(user, tuple(d["approver_roles"]))
            if user["role"] != "maintenance_head" and user.get("tenant_id") != d["tenant_id"]:
                raise Forbidden("TENANT_FORBIDDEN")
            if d["status"] != "suggested":
                raise Conflict(f"estado {d['status']}")
            if action == "reject":
                self._advance(d, "cancelled", by)
                return self._public(d)
            src = self.stock[(d["part_id"], d["from_zone"])] if kind == "transfer" else None
            if src and src["on_hand"] - src["reserved"] < d["qty"] + src["safety_stock"] and not (body or {}).get("override"):
                raise Conflict("el almacen de origen quedaria bajo su stock de seguridad")
            d["approved_by"] = by
            self._advance(d, "approved", by)
            if src:
                src["on_hand"] -= d["qty"]
                self.stock[(d["part_id"], d["zone_id"])]["in_transit"] += d["qty"]
                self._move(d["part_id"], d["from_zone"], "transfer_out", -d["qty"], d["id"])
                self._advance(d, "in_transit", "supply-flow")
            else:
                self.stock[(d["part_id"], d["zone_id"])]["on_order"] += d["qty"]
                self._advance(d, "ordered", "supply-flow")
            d["eta"] = _iso(_now() + timedelta(hours=d["eta_h"]))
            return self._public(d)
        _require(user, RECEIVE_ROLES)
        if user["role"] != "maintenance_head" and user.get("tenant_id") != d["tenant_id"]:
            raise Forbidden("solo el parque destino confirma la recepcion")
        if d["status"] not in ("in_transit", "ordered"):
            raise Conflict(f"estado {d['status']}")
        dst = self.stock[(d["part_id"], d["zone_id"])]
        dst["in_transit" if kind == "transfer" else "on_order"] -= d["qty"]
        dst["on_hand"] += d["qty"]
        self._move(d["part_id"], d["zone_id"], "transfer_in" if kind == "transfer" else "receipt", d["qty"], d["id"])
        self._advance(d, "received", by)
        if d.get("incident_id"):  # AllocateBackorders: queda reservado para quien lo esperaba
            dst["reserved"] += d["qty"]
            d["timeline"].append({"type": "part.reserved", "at": _iso(_now()), "by": "supply-flow",
                                  "detail": f"{d['qty']} x {d['part_id']} para {d['incident_id']}"})
        return self._public(d)


def _cheapest(part_id: str) -> dict:
    """Oferta mas barata de un repuesto en supply.json."""
    rows = sorted((p for p in SUPPLY["supplier_parts"] if p["part_id"] == part_id),
                  key=lambda p: (p["unit_price_usd"], p["lead_time_d"]))
    if not rows:
        raise BadRequest(f"sin proveedor para {part_id}")
    return rows[0]
