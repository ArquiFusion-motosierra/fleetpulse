"""fplib.ids.supply() (T37): catalogo de abastecimiento coherente con el catalogo de repuestos."""
from fplib import ids


def test_supply_loads_and_references_known_parts_and_models():
    s = ids.supply()
    assert s["approval_threshold_usd"] == 1000
    parts = {p["part_id"] for p in ids.load()["parts"]}
    suppliers = {x["supplier_id"] for x in s["suppliers"]}
    for row in s["supplier_parts"]:
        assert row["part_id"] in parts and row["supplier_id"] in suppliers
    models = {m["model_id"] for m in ids.load()["models"]}
    assert set(s["downtime_cost_usd_h"]) <= models
    tenants = set(ids.tenants())
    for t in s["transit_h"]:
        assert t["from"] in tenants and t["to"] in tenants and t["hours"] > 0


def test_part_policy_and_cheapest_supplier():
    assert ids.part_policy("PRT-04") == {"criticality": "A", "safety_stock": 1}
    assert ids.part_policy("PRT-99") == {"criticality": "C", "safety_stock": 0}  # sin fila: valores por defecto
    offers = ids.supplier_parts("PRT-04")
    assert offers[0]["supplier_id"] == "SUP-HIDRA" and offers[0]["lead_time_d"] == 7
