"""EOQ-lite y punto de reorden (diseno de abastecimiento, 5.2)."""
from supply import policy


def test_expensive_pump_orders_just_what_is_needed():
    p = policy.plan(d=policy.daily_demand(1), lead_time_d=7, criticality="A", safety_stock=1, reorder_point=1,
                    unit_price_usd=6800, min_qty=1, available=0)
    assert p["eoq"] == 1 and p["order_qty"] == p["rop"] + p["ss"]
    assert p["ss"] >= 1 and p["days_of_coverage"] == 0


def test_cheap_filters_order_in_lots():
    p = policy.plan(d=1.0, lead_time_d=2, criticality="C", safety_stock=3, reorder_point=4, unit_price_usd=42,
                    min_qty=5, available=2)
    assert 10 <= p["eoq"] <= 60 and p["order_qty"] == p["eoq"]
    assert p["rop"] == max(4, 2 + p["ss"])


def test_no_order_above_reorder_point_and_infinite_coverage_without_demand():
    p = policy.plan(d=0, lead_time_d=3, criticality="B", safety_stock=2, reorder_point=2, unit_price_usd=175,
                    min_qty=2, available=5, on_order=1)
    assert (p["eoq"], p["order_qty"], p["days_of_coverage"], p["position"]) == (0, 0, None, 6)
    below = policy.plan(d=0, lead_time_d=3, criticality="B", safety_stock=2, reorder_point=2, unit_price_usd=175,
                        min_qty=2, available=1)
    assert below["order_qty"] == 3  # ROP 2 + SS 2 - posicion 1, sobre min_qty
