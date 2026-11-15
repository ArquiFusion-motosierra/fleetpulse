"""Pruebas puras de api/supply_calc.py (T40): quien aprueba (5.4), traslados, metricas de stock y la forma publica de
una PO/TRF escrita con campos minimos por supply-flow."""
from decimal import Decimal

import supply_api_support  # noqa: F401 (agrega api/ y fplib al sys.path)

from api import supply_calc as calc


def test_quien_aprueba():
    assert calc.approver_roles("transfer", 0, "C") == ["maintenance_head"]
    assert calc.approver_roles("purchase_order", 999, "B") == ["admin", "supervisor", "maintenance_head"]
    assert calc.approver_roles("purchase_order", 1000, "C") == ["maintenance_head"]
    assert calc.approver_roles("purchase_order", 10, "A") == ["maintenance_head"]


def test_traslado_entre_parques_y_dentro_de_uno():
    assert calc.transit("FUNDO-A", "NORTE") == {"hours": 16, "freight_usd": 180}
    assert calc.transit("NORTE", "FUNDO-A") == {"hours": 16, "freight_usd": 180}
    assert calc.transit("NORTE", "SUR") == {"hours": 2, "freight_usd": 0}


def test_fila_de_stock_sin_consumo():
    item = {"PK": "STOCK#PRT-04#FUNDO-A", "on_hand": Decimal(3), "reserved": Decimal(0), "reorder_point": Decimal(1)}
    row = calc.stock_row(item, {"part_id": "PRT-04", "sku": "S", "name": "Bomba", "unit_cost_usd": 6500})
    assert row["status"] == "ok" and row["available"] == 3 and row["rop"] == 1
    assert row["days_of_coverage"] == 21.0  # demanda de prueba: rop en una semana
    assert row["on_order"] == 0 and row["in_transit"] == 0 and row["tenant_id"] == "agro"


def test_fila_de_stock_bajo_y_quiebre():
    part = {"part_id": "PRT-01", "unit_cost_usd": 45}
    low = calc.stock_row({"PK": "STOCK#PRT-01#NORTE", "on_hand": 4, "reserved": 1, "reorder_point": 4}, part)
    assert low["status"] == "low" and low["available"] == 3
    out = calc.stock_row({"PK": "STOCK#PRT-01#NORTE", "on_hand": 2, "reserved": 2, "reorder_point": 4,
                          "in_transit": 5}, part, consumed_30d=60)
    assert out["status"] == "stockout" and out["days_of_coverage"] == 2.5 and out["rop"] >= 4


def test_doc_minimo_de_supply_flow_completa_las_claves_de_la_consola():
    meta = {"PK": "PO#PO-SUR-PRT-07-20261115T1400", "SK": "META", "status": "ordered", "tenant_id": "mineria",
            "zone_id": "SUR", "part_id": "PRT-07", "qty": Decimal(1), "supplier_id": "SUP-MOTOR",
            "total_usd": Decimal("1250.5"), "lead_time_d": Decimal(5), "created_at": "2026-11-15T14:00:00Z",
            "task_token": "secreto", "GSI3PK": "TENANT#mineria", "waiting_for": "receipt"}
    doc = calc.public_doc(meta, [])
    assert doc["id"] == "PO-SUR-PRT-07-20261115T1400" and doc["kind"] == "purchase_order"
    assert doc["cost_usd"] == 1250.5 and doc["eta_h"] == 120 and doc["eta"] == "2026-11-20T14:00:00Z"
    assert doc["approver_roles"] == ["maintenance_head"]  # PO >= 1000
    assert doc["updated_at"] == doc["created_at"] and doc["timeline"][0]["type"] == "supply.suggested"
    assert "task_token" not in doc and "GSI3PK" not in doc and doc["qty"] == 1


def test_trf_con_to_zone_y_eventos_del_flujo():
    meta = {"PK": "TRF#TRF-FUNDO-A-NORTE-PRT-04-20261115T1440", "SK": "META", "status": "in_transit",
            "to_zone": "NORTE", "from_zone": "FUNDO-A", "part_id": "PRT-04", "qty": 1, "freight_usd": 180,
            "eta": "2026-11-16T06:40:00Z", "created_at": "2026-11-15T14:40:00Z"}
    ev = [{"SK": "EV#2026-11-15T14:55:00Z#b", "event_type": "transfer.shipped", "by": "supply-flow",
           "payload": '{"occurred_at": "2026-11-15T14:55:00Z", "source": "aws.supply"}'},
          {"SK": "EV#2026-11-15T14:40:00Z#a", "event_type": "supply.suggested", "payload": "{}"}]
    doc = calc.public_doc(meta, ev)
    assert doc["zone_id"] == "NORTE" and doc["tenant_id"] == "mineria" and doc["from_tenant"] == "agro"
    assert doc["cost_usd"] == 180 and doc["eta_h"] == 16.0
    assert [e["type"] for e in doc["timeline"]] == ["supply.suggested", "supply.in_transit"]
    assert doc["timeline"][1]["event_type"] == "transfer.shipped"


def test_kpis_por_alcance():
    stock = [{"tenant_id": "mineria", "on_hand": 2, "unit_cost_usd": 10, "status": "ok"},
             {"tenant_id": "mineria", "on_hand": 0, "unit_cost_usd": 10, "status": "stockout"},
             {"tenant_id": "agro", "on_hand": 5, "unit_cost_usd": 1, "status": "ok"}]
    docs = [{"tenant_id": "mineria", "status": "received", "downtime_avoided_h": 152, "downtime_avoided_usd": 68400},
            {"tenant_id": "mineria", "status": "suggested", "downtime_avoided_h": 99, "downtime_avoided_usd": 1}]
    k = calc.kpis(stock, docs, ("mineria",))
    assert k["downtime_avoided_h"] == 152 and k["downtime_avoided_usd"] == 68400
    assert k["open_stockouts"] == 1 and k["inventory_value_usd"] == {"mineria": 20} and k["fill_rate"] == 0.5
