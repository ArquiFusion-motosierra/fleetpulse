"""supply-sourcing: faltante -> opciones comparadas -> sugerencia PO o TRF, BACKORDER#, eventos e inicio de
supply-flow (diseno de abastecimiento, 5.3 y 5.4)."""
import pytest
from supply_support import (INC, NOW, PO, SHORTAGE_AT, TRF, FakeSfn, get, outbox_events, query, seed_demo,
                            seed_stock, shortage, sqs, validate)

from supply import consumer, sourcing, store


@pytest.fixture
def sfn():
    return FakeSfn()


def run(ddb, sfn, *events, raw=True):
    return consumer.handler(sqs(*events, raw=raw), ddb=ddb, sfn=sfn)


def test_demo_shortage_suggests_transfer_from_agro_with_both_options(supply_ddb, sfn):
    seed_demo(supply_ddb)
    assert run(supply_ddb, sfn, shortage()) == {"batchItemFailures": []}

    doc = get(supply_ddb, f"TRF#{TRF}")
    assert (doc["kind"], doc["status"], doc["from_zone"], doc["from_tenant"], doc["zone_id"], doc["tenant_id"]) == (
        "transfer", "suggested", "FUNDO-A", "agro", "NORTE", "mineria")
    assert (doc["trf_id"], doc["id"], doc["to_zone"], doc["qty"], doc["eta_h"], doc["freight_usd"]) == (
        TRF, TRF, "NORTE", 1, 16, 180)
    assert doc["options"] == [
        {"source_kind": "transfer", "from_zone": "FUNDO-A", "eta_h": 16, "cost_usd": 180, "total_cost_usd": 7380},
        {"source_kind": "transfer", "from_zone": "FUNDO-B", "eta_h": 16, "cost_usd": 180, "total_cost_usd": 7380},
        {"source_kind": "transfer", "from_zone": "PACKING", "eta_h": 16, "cost_usd": 180, "total_cost_usd": 7380},
        {"source_kind": "purchase", "supplier_id": "SUP-HIDRA", "eta_h": 168, "cost_usd": 6800, "total_cost_usd": 82400}]
    assert (doc["downtime_avoided_h"], doc["downtime_avoided_usd"]) == (152, 68400)
    assert doc["approver_roles"] == ["maintenance_head"] and doc["receiver_roles"] == ["admin", "technician", "maintenance_head"]
    assert (doc["incident_id"], doc["machine_id"], doc["source"], doc["created_at"]) == (INC, "EX-017", "shortage",
                                                                                         "2026-11-15T14:35:00Z")
    assert "task_token" not in doc and "waiting_for" not in doc
    # indices: estado, parque destino y salientes del donante
    assert (doc["GSI2PK"], doc["GSI3PK"], doc["GSI3SK"], doc["GSI1PK"]) == (
        "SUPPLY#suggested", "TENANT#mineria", "TRF#2026-11-15T14:35:00Z", "TRFFROM#agro")

    (bo,) = query(supply_ddb, "BACKORDER#PRT-04#NORTE")
    assert (bo["SK"], bo["qty"], bo["machine_id"], bo["ref"], bo["tenant_id"]) == (INC, 1, "EX-017", TRF, "mineria")

    (ev,) = outbox_events(supply_ddb)
    validate(ev, "envelope")
    validate(ev, "supply")
    assert (ev["event_type"], ev["aggregate_type"], ev["aggregate_id"], ev["incident_id"], ev["tenant_id"]) == (
        "supply.suggested", "transfer", TRF, INC, "mineria")
    assert ev["payload"]["options"] == doc["options"] and ev["payload"]["from_zone"] == "FUNDO-A"
    timeline = [r for r in query(supply_ddb, f"TRF#{TRF}") if r["SK"].startswith("EV#")]
    assert [t["event_type"] for t in timeline] == ["supply.suggested"]

    assert sfn.started == [{"arn": "arn:aws:states:us-east-1:000000000000:stateMachine:supply-flow", "name": TRF,
                            "input": {"id": TRF, "kind": "transfer"}}]
    assert get(supply_ddb, "STOCK#PRT-04#FUNDO-A")["available"] == 3  # sugerir no mueve stock


def test_retry_of_same_shortage_creates_nothing_new(supply_ddb, sfn):
    seed_demo(supply_ddb)
    e = shortage()
    run(supply_ddb, sfn, e)
    assert run(supply_ddb, sfn, e, raw=False) == {"batchItemFailures": []}  # mismo mensaje, envuelto por SNS
    assert len(outbox_events(supply_ddb)) == 1 and len(sfn.started) == 1
    assert len(query(supply_ddb, "BACKORDER#PRT-04#NORTE")) == 1


def test_other_incident_same_minute_joins_the_existing_order(supply_ddb, sfn):
    seed_demo(supply_ddb)
    run(supply_ddb, sfn, shortage())
    other = "INC-EX-018-20261115T1430"
    run(supply_ddb, sfn, shortage(incident_id=other, machine_id="EX-018"))
    bos = {b["SK"]: b for b in query(supply_ddb, "BACKORDER#PRT-04#NORTE")}
    assert set(bos) == {INC, other} and bos[other]["ref"] == TRF
    assert len(outbox_events(supply_ddb)) == 1


def test_without_donor_buys_from_supplier_and_head_approves_criticality_a(supply_ddb, sfn):
    seed_demo(supply_ddb, agro=1)  # agro queda en su stock de seguridad (1): no se le saca
    run(supply_ddb, sfn, shortage())
    doc = get(supply_ddb, f"PO#{PO}")
    assert (doc["kind"], doc["po_id"], doc["supplier_id"], doc["qty"], doc["unit_price_usd"], doc["total_usd"]) == (
        "po", PO, "SUP-HIDRA", 1, 6800, 6800)
    assert (doc["eta_h"], doc["lead_time_d"], doc["eta"]) == (168, 7, "2026-11-22T14:35:00Z")
    assert doc["options"] == [{"source_kind": "purchase", "supplier_id": "SUP-HIDRA", "eta_h": 168, "cost_usd": 6800,
                               "total_cost_usd": 82400}]
    assert doc["downtime_avoided_h"] == 0 and doc["approver_roles"] == ["maintenance_head"]
    assert (doc["GSI3SK"], "GSI1PK" in doc) == ("PO#2026-11-15T14:35:00Z", False)
    (ev,) = outbox_events(supply_ddb)
    validate(ev, "supply")
    assert ev["aggregate_type"] == "purchase_order" and ev["payload"]["supplier_id"] == "SUP-HIDRA"
    assert sfn.started[0]["input"] == {"id": PO, "kind": "po"}


def test_cheap_b_part_po_is_approved_by_park_admin_and_respects_min_qty(supply_ddb, sfn):
    for z in ("NORTE", "CENTRO", "SUR", "FUNDO-A", "FUNDO-B", "PACKING", "PLANTA-1", "PLANTA-2", "ALMACEN"):
        seed_stock(supply_ddb, "PRT-03", z, 0)
    run(supply_ddb, sfn, shortage(missing=[{"part_id": "PRT-03", "qty": 1}]))
    doc = get(supply_ddb, "PO#PO-NORTE-PRT-03-20261115T1434")
    assert (doc["qty"], doc["needed_qty"], doc["total_usd"]) == (2, 1, 350)  # min_qty 2 de SUP-HIDRA
    assert doc["approver_roles"] == ["admin", "supervisor", "maintenance_head"]
    (bo,) = query(supply_ddb, "BACKORDER#PRT-03#NORTE")
    assert bo["qty"] == 1  # el incidente espera lo que le falto, no el lote


def test_transfer_that_leaves_donor_under_reorder_point_also_suggests_its_refill(supply_ddb, sfn):
    seed_demo(supply_ddb, agro=2, agro_reorder=2)
    run(supply_ddb, sfn, shortage())
    refill = get(supply_ddb, "PO#PO-FUNDO-A-PRT-04-20261115T1434")
    assert (refill["tenant_id"], refill["zone_id"], refill["replenishes"], refill["qty"]) == ("agro", "FUNDO-A", TRF, 2)
    assert "incident_id" not in refill and refill["approver_roles"] == ["maintenance_head"]
    assert not query(supply_ddb, "BACKORDER#PRT-04#FUNDO-A")
    assert [s["name"] for s in sfn.started] == [TRF, "PO-FUNDO-A-PRT-04-20261115T1434"]
    assert [e["event_type"] for e in outbox_events(supply_ddb)] == ["supply.suggested", "supply.suggested"]


def test_reorder_from_registry_gets_the_ref(supply_ddb, sfn):
    seed_demo(supply_ddb)
    supply_ddb.put_item(TableName="fp-registry", Item=store.ser({
        "PK": "REORDER#PRT-04#NORTE", "SK": "2026-11-15T14:34:09Z", "part_id": "PRT-04", "zone_id": "NORTE",
        "qty": 2, "source": "shortage", "incident_id": INC}))
    run(supply_ddb, sfn, shortage())
    assert query(supply_ddb, "REORDER#PRT-04#NORTE")[0]["ref"] == TRF


def test_no_options_and_ignored_events_ack_the_message(supply_ddb, sfn):
    e = shortage(missing=[{"part_id": "PRT-99", "qty": 1}])  # sin proveedor ni stock
    other = {**shortage(), "event_type": "part.reserved"}
    old = shortage()
    del old["payload"]["zone_id"]  # evento anterior a T38
    assert run(supply_ddb, sfn, e, other, old) == {"batchItemFailures": []}
    assert not outbox_events(supply_ddb) and not sfn.started


def test_bad_message_is_a_partial_failure(supply_ddb, sfn):
    seed_demo(supply_ddb)
    bad = {"Records": [{"messageId": "m-bad", "body": "{no json"}]}
    assert consumer.handler(bad, ddb=supply_ddb, sfn=sfn) == {"batchItemFailures": [{"itemIdentifier": "m-bad"}]}
    invalid = shortage()
    invalid["payload"]["missing"] = []  # contrato: minItems 1
    assert run(supply_ddb, sfn, invalid) == {"batchItemFailures": [{"itemIdentifier": "m-0"}]}


def test_ids_and_rank_are_deterministic():
    assert sourcing.trf_id("FUNDO-A", "NORTE", "PRT-04", NOW) == "TRF-FUNDO-A-NORTE-PRT-04-20261115T1435"
    assert sourcing.po_id("NORTE", "PRT-04", NOW) == "PO-NORTE-PRT-04-20261115T1435"
    assert sourcing.transit("agro", "mineria") == {"hours": 16.0, "freight_usd": 180.0}  # tabla simetrica
    assert sourcing.transit("mineria", "mineria") == {"hours": 2, "freight_usd": 0}
    assert sourcing.downtime_cost_h("EX-017") == 450 and sourcing.downtime_cost_h(None) == 0
    assert SHORTAGE_AT.startswith("2026-11-15T14:34")
