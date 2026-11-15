"""Calculo puro de daily-planning: vencimiento por horas, uso diario, desgaste, kit de repuestos y reorden."""
from datetime import date

import pytest
from planning_support import EX001_KIT, NOW

from fplib import ids
from planning import plan

TODAY = date(2026, 11, 15)
BOM = ids.load()["bom"]


def meta(hours_total, status="ok", **kw):
    return {"machine_id": "EX-001", "model": "EX", "zone_id": "NORTE", "hours_total": hours_total, "status": status,
            **kw}


def assess(m, rate=12.0, horizon=14, catalog=None):
    return plan.assess(m, catalog if catalog is not None else ids.machines()["EX-001"], 500, BOM, rate, TODAY,
                       horizon)


def test_lima_day_and_due_at_use_lima_morning():
    assert plan.lima_day(NOW) == TODAY  # 05:00 de Lima
    assert plan.lima_day(NOW.replace(hour=3)) == date(2026, 11, 14)  # 22:00 del dia anterior en Lima
    assert plan.due_at(date(2026, 11, 21)) == "2026-11-21T13:00:00Z"


def test_due_by_hours_from_catalog_last_service():
    # 1508 - 1087 = 421 h desde el servicio; faltan 79 h a 12 h/dia -> 6,6 dias
    e = assess(meta(1508))
    assert e["hours_since_service"] == 421
    assert e["service_interval_h"] == 500
    assert e["days_to_due"] == pytest.approx(79 / 12, abs=0.01)
    assert e["due_at"] == "2026-11-21T13:00:00Z"
    assert e["order_id"] == "ORD-EX-001-20261121"
    assert e["parts"] == EX001_KIT
    assert e["reason"].startswith("Preventivo a 7 dias: 421 h desde el ultimo servicio (intervalo 500 h)")
    assert len(e["reason"]) <= 300


def test_meta_last_service_wins_over_catalog():
    e = assess(meta(2000, last_service_h=1600))
    assert e["hours_since_service"] == 400
    assert e["due_at"] == "2026-11-23T13:00:00Z"  # 100 h / 12 = 8,3 dias
    assert e["reason"].startswith("Preventivo a 14 dias")


def test_not_due_beyond_horizon():
    assert assess(meta(1097)) is None  # 10 h desde el servicio: faltan 490 h, ~41 dias
    assert assess(meta(1508), horizon=5) is None


def test_overdue_is_due_today():
    e = assess(meta(1700))
    assert e["hours_since_service"] == 613
    assert e["days_to_due"] == 0
    assert e["due_at"] == "2026-11-15T13:00:00Z"
    assert "vencido" in e["reason"]


def test_wear_from_severity_brings_due_date_forward():
    ok = assess(meta(1460))
    worn = assess(meta(1460, status="schedule"))
    assert worn["days_to_due"] < ok["days_to_due"]
    assert "schedule" in worn["reason"]
    assert plan.wear("immediate") > plan.wear("schedule") > plan.wear("observe") > plan.wear("ok") == 1.0
    assert plan.wear(None) == 1.0


def test_no_service_history_is_not_planned():
    assert assess(meta(1508), catalog={}) is None


def test_hours_per_day_from_oldest_reading_clamped_and_default():
    m = meta(1508, last_ts="2026-11-15T10:00:00Z")
    assert plan.hours_per_day(m, {"ts": "2026-11-13T10:00:00Z", "cumulative_operating_hours": 1468}) == 20
    assert plan.hours_per_day(m, {"ts": "2026-11-13T10:00:00Z", "cumulative_operating_hours": 1400}) == 24
    assert plan.hours_per_day(m, {"ts": "2026-11-13T10:00:00Z", "cumulative_operating_hours": 1508}) == 1
    assert plan.hours_per_day(m, None) == plan.DEFAULT_HOURS_PER_DAY
    # menos de 6 h de historia: no alcanza para estimar
    assert plan.hours_per_day(m, {"ts": "2026-11-15T08:00:00Z", "cumulative_operating_hours": 1507}) == 12
    assert plan.hours_per_day(meta(1508), {"ts": "2026-11-13T10:00:00Z", "cumulative_operating_hours": 1468}) == 12


def test_service_kit_crosses_replacement_multiples():
    assert plan.service_kit("EX", 1087, 1587, BOM) == EX001_KIT
    # 1900 -> 2400 cruza 2000 h: entra el aceite hidraulico (4) ademas de los filtros
    kit = plan.service_kit("EX", 1900, 2400, BOM)
    assert {"part_id": "PRT-10", "qty": 4} in kit
    assert [p["part_id"] for p in kit] == sorted(p["part_id"] for p in kit)
    # TO cada 400 h: un servicio que no cruza 500 h no lleva filtros
    assert plan.service_kit("TO", 510, 910, BOM) == []


def test_demand_sums_kits_by_part_and_zone():
    a = {"zone_id": "NORTE", "parts": [{"part_id": "PRT-01", "qty": 1}, {"part_id": "PRT-02", "qty": 1}]}
    b = {"zone_id": "NORTE", "parts": [{"part_id": "PRT-01", "qty": 2}]}
    c = {"zone_id": "SUR", "parts": [{"part_id": "PRT-01", "qty": 1}]}
    assert plan.demand([a, b, c]) == {("PRT-01", "NORTE"): 3, ("PRT-02", "NORTE"): 1, ("PRT-01", "SUR"): 1}


def test_reorders_only_below_reorder_point_counting_pending_orders():
    stock = {("PRT-01", "NORTE"): {"available": 12, "reorder_point": 4},   # holgado
             ("PRT-02", "NORTE"): {"available": 3, "reorder_point": 3},    # la demanda lo deja bajo el punto
             ("PRT-03", "NORTE"): {"available": 1, "reorder_point": 2},    # ya estaba bajo, sin demanda
             ("PRT-11", "NORTE"): {"available": 1, "reorder_point": 2}}    # bajo, pero hay un pedido en curso
    demand = {("PRT-01", "NORTE"): 1, ("PRT-02", "NORTE"): 2, ("PRT-05", "SUR"): 2}  # PRT-05 SUR sin fila de stock
    out = plan.reorders(stock, demand, {("PRT-11", "NORTE"): 5})
    assert [(r["part_id"], r["zone_id"], r["qty"]) for r in out] == [
        ("PRT-02", "NORTE", 2), ("PRT-03", "NORTE", 1), ("PRT-05", "SUR", 2)]
    r = out[0]
    assert (r["available"], r["reorder_point"], r["demand_14d"], r["on_order"]) == (3, 3, 2, 0)
