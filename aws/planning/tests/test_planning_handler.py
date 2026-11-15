"""Lambda `plan` de daily-planning contra moto: ordenes en fp-outbox, pedidos en fp-registry e idempotencia."""
import os
import sys
from decimal import Decimal

from planning_support import DAY, EX001_KIT, NOW_ISO, order_events, reorders, seed_kit_stock, seed_machine, \
    seed_reading, seed_stock

from fplib import schema
from planning import handler


def run(ddb, event=None):
    return handler.handler(event if event is not None else {}, client=ddb.client)


def test_empty_registry_plans_nothing_and_writes_nothing(ddb):
    out = run(ddb)
    assert out["day"] == DAY and out["machines"] == 0
    assert out["orders"] == [] and out["reorders"] == []
    assert ddb.outbox.scan()["Count"] == 0 and ddb.inbox.scan()["Count"] == 0
    assert ddb.registry.scan()["Count"] == 0


def test_machine_due_by_hours_emits_order_event_valid_against_contract(ddb):
    seed_machine(ddb.state, "EX-001", "EX", "NORTE", 1508)
    seed_kit_stock(ddb.registry, "NORTE")
    out = run(ddb)
    assert out["orders"] == ["ORD-EX-001-20261121"]
    (env,) = order_events(ddb.outbox)
    schema.validate("envelope", env)
    schema.validate("maintenance.order", env)
    assert env["source"] == "aws.daily-planning" and env["machine_id"] == "EX-001"
    assert env["aggregate_id"] == "ORD-EX-001-20261121" and env["occurred_at"] == NOW_ISO
    p = env["payload"]
    assert p["kind"] == "preventive" and p["due_at"] == "2026-11-21T13:00:00Z"
    assert p["hours_since_service"] == 421 and p["service_interval_h"] == 500 and p["parts"] == EX001_KIT
    claim = ddb.inbox.get_item(Key={"message_id": f"planning:{DAY}:ORDER#EX-001"})["Item"]
    assert claim["consumer"] == "planning"
    # stock holgado: no hay pedido sugerido
    assert out["reorders"] == [] and reorders(ddb.registry) == []


def test_machine_not_due_writes_nothing(ddb):
    seed_machine(ddb.state, "EX-001", "EX", "NORTE", 1097)
    seed_kit_stock(ddb.registry, "NORTE")
    out = run(ddb)
    assert out["machines"] == 1 and out["due"] == []
    assert order_events(ddb.outbox) == [] and reorders(ddb.registry) == []


def test_usage_rate_from_readings_moves_the_due_date(ddb):
    # 20 h/dia en vez de 12: faltan 79 h -> 3,95 dias
    seed_machine(ddb.state, "EX-001", "EX", "NORTE", 1508, last_ts="2026-11-15T10:00:00Z")
    seed_reading(ddb.state, "EX-001", "2026-11-13T10:00:00Z", 1468)
    seed_reading(ddb.state, "EX-001", "2026-11-14T10:00:00Z", 1488)
    seed_reading(ddb.state, "EX-001", "2026-11-11T10:00:00Z", 1300)  # fuera de la ventana de 3 dias
    seed_kit_stock(ddb.registry, "NORTE")
    out = run(ddb)
    assert out["orders"] == ["ORD-EX-001-20261118"]


def test_shortfall_writes_suggested_reorder_in_registry(ddb):
    seed_machine(ddb.state, "EX-001", "EX", "NORTE", 1508)
    seed_kit_stock(ddb.registry, "NORTE", overrides={"PRT-02": (2, 2)})  # el filtro hidraulico queda en 1 < 2
    out = run(ddb)
    assert out["reorders"] == [{"part_id": "PRT-02", "zone_id": "NORTE", "qty": 1}]
    (r,) = reorders(ddb.registry)
    assert r["PK"] == "REORDER#PRT-02#NORTE" and r["SK"] == NOW_ISO
    assert r["GSI1SK"] == f"{NOW_ISO}#PRT-02#NORTE"
    assert r["source"] == "planning" and r["qty"] == 1 and r["forecast_14d"] == 1
    assert (r["available"], r["reorder_point"], r["on_order"], r["day"]) == (2, 2, 0, DAY)
    assert r["machine_ids"] == ["EX-001"]
    assert ddb.inbox.get_item(Key={"message_id": f"planning:{DAY}:REORDER#PRT-02#NORTE"}).get("Item")


def test_missing_stock_row_reorders_the_whole_demand(ddb):
    seed_machine(ddb.state, "EX-001", "EX", "NORTE", 1508)
    out = run(ddb)
    assert sorted((r["part_id"], r["qty"]) for r in out["reorders"]) == [("PRT-01", 1), ("PRT-02", 1), ("PRT-11", 1)]


def test_low_stock_without_demand_is_reordered(ddb):
    seed_stock(ddb.registry, "PRT-04", "SUR", 0, 1)
    out = run(ddb)
    assert out["reorders"] == [{"part_id": "PRT-04", "zone_id": "SUR", "qty": 1}]


def test_pending_reorder_counts_as_on_order(ddb):
    seed_machine(ddb.state, "EX-001", "EX", "NORTE", 1508)
    seed_kit_stock(ddb.registry, "NORTE", overrides={"PRT-02": (2, 2)})
    ddb.registry.put_item(Item={"PK": "REORDER#PRT-02#NORTE", "SK": "2026-11-14T16:00:00Z", "part_id": "PRT-02",
                                "zone_id": "NORTE", "qty": 3, "source": "shortage",
                                "GSI1PK": "REORDERS", "GSI1SK": "2026-11-14T16:00:00Z#PRT-02#NORTE"})
    out = run(ddb)
    assert out["reorders"] == []
    assert len(reorders(ddb.registry)) == 1


def test_rerun_same_day_does_not_duplicate(ddb):
    seed_machine(ddb.state, "EX-001", "EX", "NORTE", 1508)
    seed_kit_stock(ddb.registry, "NORTE", overrides={"PRT-02": (2, 2)})
    first = run(ddb)
    second = run(ddb)
    assert first["orders"] == ["ORD-EX-001-20261121"] and second["orders"] == []
    assert second["already_planned"] == ["EX-001"]
    assert second["reorders"] == []  # el pedido de la primera corrida ya cuenta como en curso
    assert len(order_events(ddb.outbox)) == 1
    assert len(reorders(ddb.registry)) == 1


def test_concurrent_rerun_reorder_claim_prevents_duplicate(ddb, monkeypatch):
    """Dos corridas a la vez: la segunda no ve el pedido de la primera, pero el reclamo de fp-inbox la frena."""
    seed_stock(ddb.registry, "PRT-04", "SUR", 0, 1)
    run(ddb)
    monkeypatch.setattr(handler.store, "on_order", lambda client, since: {})
    out = run(ddb)
    assert out["reorders"] == [] and out["reorders_already_planned"] == ["PRT-04#SUR"]
    assert len(reorders(ddb.registry)) == 1


def test_machine_with_open_order_is_skipped_but_counts_for_demand(ddb):
    seed_machine(ddb.state, "EX-001", "EX", "NORTE", 1508)
    seed_kit_stock(ddb.registry, "NORTE", overrides={"PRT-02": (1, 1)})
    # orden de ayer ya materializada por registry
    ddb.registry.put_item(Item={"PK": "ORDER#EX-001", "SK": "2026-11-20T13:00:00Z", "machine_id": "EX-001",
                                "order_id": "ORD-EX-001-20261120", "status": "suggested",
                                "GSI1PK": "ORDERS", "GSI1SK": "2026-11-20T13:00:00Z#EX-001"})
    out = run(ddb)
    assert out["orders"] == [] and out["skipped_open"] == ["EX-001"]
    assert order_events(ddb.outbox) == []
    assert out["reorders"] == [{"part_id": "PRT-02", "zone_id": "NORTE", "qty": 1}]


def test_dry_run_computes_without_writing(ddb):
    seed_machine(ddb.state, "EX-001", "EX", "NORTE", 1508)
    seed_machine(ddb.state, "EX-002", "EX", "NORTE", 1000, last_service_h=990)
    out = run(ddb, {"dry_run": True})
    assert out["dry_run"] is True
    assert [d["machine_id"] for d in out["due"]] == ["EX-001"]
    assert out["orders"] == ["ORD-EX-001-20261121"]
    assert len(out["reorders"]) == 3
    assert ddb.outbox.scan()["Count"] == 0 and ddb.inbox.scan()["Count"] == 0
    assert reorders(ddb.registry) == []


def test_step_functions_input_shape_and_horizon(ddb):
    seed_machine(ddb.state, "EX-001", "EX", "NORTE", 1508)
    out = run(ddb, {"input": {"horizon_days": 3, "dry_run": True}, "execution": "manual-1"})
    assert out["horizon_days"] == 3 and out["due"] == []
    out = run(ddb, {"input": {}, "execution": "manual-2"})
    (env,) = order_events(ddb.outbox)
    assert env["trace_id"] == "manual-2" and out["orders"] == ["ORD-EX-001-20261121"]


def test_order_events_materialize_in_registry_through_the_registry_lambda(ddb):
    """Contrato con aws/registry: el evento que emite planning termina en ORDER#<maquina>/<due_at> (GSI1 ORDERS)."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "registry"))
    from reg.apply import apply

    seed_machine(ddb.state, "EX-001", "EX", "NORTE", 1508)
    seed_kit_stock(ddb.registry, "NORTE")
    run(ddb)
    (env,) = order_events(ddb.outbox)
    assert apply(ddb.client, env)["status"] == "applied"
    item = ddb.registry.get_item(Key={"PK": "ORDER#EX-001", "SK": "2026-11-21T13:00:00Z"})["Item"]
    assert item["order_id"] == "ORD-EX-001-20261121" and item["status"] == "suggested"
    assert item["GSI1PK"] == "ORDERS" and item["hours_since_service"] == Decimal(421)
    # con la orden ya en fp-registry, la siguiente corrida no sugiere otra para la misma maquina
    out = run(ddb, {"dry_run": True})
    assert out["skipped_open"] == ["EX-001"]
