"""supply-flow de punta a punta: la ASL tal cual (supply_asl) con las Lambdas reales sobre moto, iniciada por
supply-sourcing. La API (T40) se imita con send_task_success / send_task_failure y el token guardado en el META."""
import json
from types import SimpleNamespace

import pytest
from supply_asl import Sns, StateMachine
from supply_support import INC, PO, TRF, get, outbox_events, query, seed_demo, shortage, sqs, validate

from supply import consumer, docs, flow

LAMBDAS = {"LoadArn": flow.load, "WaitApprovalArn": flow.wait_approval, "ApproveArn": flow.approve,
           "CancelArn": flow.cancel, "PlaceOrderArn": flow.place_order, "ShipArn": flow.ship,
           "WaitReceiptArn": flow.wait_receipt, "ReceiveArn": flow.receive,
           "AllocateBackordersArn": flow.allocate_backorders}
WAITS = ("WaitApprovalArn", "WaitReceiptArn")


@pytest.fixture
def sf(supply_ddb):
    sns = Sns()
    lambdas = {k: (lambda f, k: (lambda ev: f(ev, ddb=supply_ddb, sns=sns) if k in WAITS else f(ev, ddb=supply_ddb)))(f, k)
               for k, f in LAMBDAS.items()}
    machine = StateMachine(lambdas, sns)

    def shortage_in(**kw):
        assert consumer.handler(sqs(shortage(**kw)), ddb=supply_ddb, sfn=machine) == {"batchItemFailures": []}

    return SimpleNamespace(ddb=supply_ddb, sns=sns, machine=machine, shortage=shortage_in)


def meta(sf, doc_id):
    return docs.load(sf.ddb, doc_id)


def stock(sf, zone, part="PRT-04"):
    return get(sf.ddb, f"STOCK#{part}#{zone}")


def api(sf, doc_id, waiting_for, output=None, error=None, cause=None):
    """Lo que hara la API: lee el token de la espera actual y responde a Step Functions."""
    d = meta(sf, doc_id)
    assert d["waiting_for"] == waiting_for
    if error:
        return sf.machine.send_task_failure(taskToken=d["task_token"], error=error, cause=cause)
    return sf.machine.send_task_success(taskToken=d["task_token"], output=json.dumps(output or {}))


def test_transfer_from_agro_end_to_end_reserves_the_pump_for_ex017(sf):
    seed_demo(sf.ddb)
    sf.shortage()
    ex = sf.machine.executions[TRF]
    assert (ex.status, ex.waiting_in, ex.timeout_s) == ("WAITING", "WaitForApproval", 86400)
    d = meta(sf, TRF)
    assert d["status"] == "suggested" and d["waiting_for"] == "approval" and d["task_token"] == ex.token
    (ask,) = sf.sns.calls
    assert ask["MessageAttributes"]["role"]["StringValue"] == "maintenance_head"
    assert ask["MessageAttributes"]["tenant_id"]["StringValue"] == "mineria"
    assert TRF in ask["Message"] and ex.token not in ask["Message"]

    api(sf, TRF, "approval", {"approved_by": "jefe@fleetpulse.pe", "role": "maintenance_head"})
    assert (ex.status, ex.waiting_in) == ("WAITING", "WaitForReceipt")
    d = meta(sf, TRF)
    assert (d["status"], d["approved_by"], d["waiting_for"], d["GSI2PK"]) == (
        "in_transit", "jefe@fleetpulse.pe", "receipt", "SUPPLY#in_transit")
    assert ex.timeout_s == 2 * 16 * 3600
    donor, dest = stock(sf, "FUNDO-A"), stock(sf, "NORTE")
    assert (donor["on_hand"], donor["available"]) == (2, 2) and dest["in_transit"] == 1
    (out,) = query(sf.ddb, "MOVE#PRT-04#FUNDO-A")
    assert (out["kind"], out["qty"], out["ref"], out["tenant_id"]) == ("transfer_out", -1, TRF, "agro")
    assert [c["MessageAttributes"]["role"]["StringValue"] for c in sf.sns.calls] == ["maintenance_head", "admin"]

    api(sf, TRF, "receipt", {"received_by": "admin@mineria.pe"})
    assert ex.status == "SUCCEEDED", (ex.error, ex.cause)
    assert ex.visited == ["Load", "Status?", "WaitForApproval", "Approve", "Kind?", "Ship", "NotifyShipped",
                          "WaitForReceipt", "Receive", "AllocateBackorders", "NotifyReceived", "Received"]
    d = meta(sf, TRF)
    assert (d["status"], d["received_by"], "task_token" in d, "waiting_for" in d) == (
        "received", "admin@mineria.pe", False, False)
    dest = stock(sf, "NORTE")
    assert (dest["on_hand"], dest["reserved"], dest["available"], dest["in_transit"]) == (1, 1, 0, 0)
    (inn,) = query(sf.ddb, "MOVE#PRT-04#NORTE")
    assert (inn["kind"], inn["qty"], inn["ref"], inn["tenant_id"]) == ("transfer_in", 1, TRF, "mineria")
    assert not query(sf.ddb, "BACKORDER#PRT-04#NORTE")
    rsv = get(sf.ddb, f"RSV#{INC}", "PRT-04#NORTE")
    assert (rsv["status"], rsv["qty"], rsv["ref"]) == ("reserved", 1, TRF)

    events = outbox_events(sf.ddb)
    assert [e["event_type"] for e in events] == ["supply.suggested", "supply.approved", "transfer.shipped",
                                                 "supply.received", "part.reserved"]
    for e in events:
        validate(e, "envelope")
        validate(e, "part" if e["event_type"].startswith("part.") else "supply")
    reserved = events[-1]
    assert (reserved["source"], reserved["incident_id"], reserved["machine_id"]) == ("aws.supply", INC, "EX-017")
    assert reserved["payload"]["reservations"][0]["reservation_id"] == rsv["reservation_id"]
    assert [e["payload"]["status"] for e in events[:4]] == ["suggested", "approved", "in_transit", "received"]
    incident_ev = [r for r in query(sf.ddb, f"INCIDENT#{INC}") if r["SK"].startswith("EV#")]
    assert [r["event_type"] for r in incident_ev] == ["part.reserved"]
    timeline = [r["event_type"] for r in query(sf.ddb, f"TRF#{TRF}") if r["SK"].startswith("EV#")]
    assert sorted(timeline) == sorted(["supply.suggested", "supply.approved", "transfer.shipped", "supply.received"])

    tech, admin = sf.sns.to("technician"), sf.sns.to("admin")
    assert "Tu repuesto llego" in tech[0]["Message"] and INC in tech[0]["Message"]
    assert tech[0]["MessageAttributes"]["tenant_id"]["StringValue"] == "mineria" and len(admin) == 2


def test_purchase_order_end_to_end(sf):
    seed_demo(sf.ddb, agro=0)
    sf.shortage()
    ex = sf.machine.executions[PO]
    api(sf, PO, "approval", {"approved_by": "jefe@fleetpulse.pe"})
    d = meta(sf, PO)
    assert (d["status"], d["eta"], d["ordered_at"]) == ("ordered", "2026-11-22T14:35:00Z", "2026-11-15T14:35:00Z")
    assert stock(sf, "NORTE")["on_order"] == 1 and ex.timeout_s == 2 * 168 * 3600
    (supplier,) = sf.sns.to("supplier")
    assert "SUP-HIDRA" in supplier["Message"]
    api(sf, PO, "receipt", {"received_by": "tec@mineria.pe"})
    assert ex.status == "SUCCEEDED"
    assert "PlaceOrder" in ex.visited and "NotifySupplier" in ex.visited and "Ship" not in ex.visited
    dest = stock(sf, "NORTE")
    assert (dest["on_hand"], dest["reserved"], dest["on_order"]) == (1, 1, 0)
    (mv,) = query(sf.ddb, "MOVE#PRT-04#NORTE")
    assert (mv["kind"], mv["qty"]) == ("receipt", 1)
    types = [e["event_type"] for e in outbox_events(sf.ddb)]
    assert types == ["supply.suggested", "supply.approved", "po.ordered", "supply.received", "part.reserved"]


def test_rejection_cancels_and_frees_the_backorder(sf):
    seed_demo(sf.ddb)
    sf.shortage()
    ex = sf.machine.executions[TRF]
    api(sf, TRF, "approval", error="SupplyRejected", cause=json.dumps({"reason": "agro lo necesita", "by": "jefe"}))
    assert ex.status == "SUCCEEDED" and ex.visited[-3:] == ["Cancel", "NotifyCancelled", "Cancelled"]
    d = meta(sf, TRF)
    assert (d["status"], d["cancel_reason"], d["cancelled_by"], "task_token" in d) == (
        "cancelled", "agro lo necesita", "jefe", False)
    assert not query(sf.ddb, "BACKORDER#PRT-04#NORTE")
    assert stock(sf, "FUNDO-A")["available"] == 3
    cancelled = outbox_events(sf.ddb, "supply.cancelled")[0]
    validate(cancelled, "supply")
    assert cancelled["payload"]["reason"] == "agro lo necesita"
    assert "agro lo necesita" in sf.sns.to("admin")[0]["Message"]


def test_no_answer_in_24h_cancels(sf):
    seed_demo(sf.ddb)
    sf.shortage()
    sf.machine.executions[TRF].time_out()
    assert meta(sf, TRF)["cancel_reason"] == "sin respuesta en 24 h"
    assert sf.machine.executions[TRF].status == "SUCCEEDED"


def test_donor_drained_before_approval_cancels_unless_override(sf):
    seed_demo(sf.ddb)
    sf.shortage()
    sf.ddb.update_item(TableName="fp-registry", Key={"PK": {"S": "STOCK#PRT-04#FUNDO-A"}, "SK": {"S": "META"}},
                       UpdateExpression="SET available = :a", ExpressionAttributeValues={":a": {"N": "1"}})
    api(sf, TRF, "approval", {"approved_by": "jefe"})
    ex = sf.machine.executions[TRF]
    assert ex.status == "SUCCEEDED" and "Cancel" in ex.visited
    assert sf.machine.invocations["ShipArn"] == 1  # InsufficientStock no se reintenta
    d = meta(sf, TRF)
    assert d["status"] == "cancelled" and d["cancel_reason"] == "stock insuficiente en FUNDO-A al despachar"
    assert stock(sf, "FUNDO-A")["available"] == 1 and stock(sf, "NORTE").get("in_transit", 0) == 0


def test_head_override_ships_below_safety_stock(sf):
    seed_demo(sf.ddb)
    sf.shortage()
    sf.ddb.update_item(TableName="fp-registry", Key={"PK": {"S": "STOCK#PRT-04#FUNDO-A"}, "SK": {"S": "META"}},
                       UpdateExpression="SET available = :a", ExpressionAttributeValues={":a": {"N": "1"}})
    api(sf, TRF, "approval", {"approved_by": "jefe", "override": True})
    assert meta(sf, TRF)["status"] == "in_transit" and stock(sf, "FUNDO-A")["available"] == 0


def test_late_receipt_warns_the_head_waits_once_more_then_alerts(sf):
    seed_demo(sf.ddb)
    sf.shortage()
    api(sf, TRF, "approval", {"approved_by": "jefe"})
    ex = sf.machine.executions[TRF]
    ex.time_out()
    assert (ex.status, ex.waiting_in) == ("WAITING", "WaitForReceiptLate")
    assert "Atrasada" in sf.sns.to("maintenance_head")[-1]["Subject"]
    assert meta(sf, TRF)["waiting_for"] == "receipt"
    api(sf, TRF, "receipt", {"received_by": "x"})  # la segunda espera tambien recibe
    assert ex.status == "SUCCEEDED"


def test_second_late_receipt_fails_with_alert(sf):
    seed_demo(sf.ddb)
    sf.shortage()
    api(sf, TRF, "approval", {})
    ex = sf.machine.executions[TRF]
    ex.time_out()
    ex.time_out()
    assert (ex.status, ex.error, ex.visited[-2:]) == ("FAILED", "SupplyFlowFailed", ["PublishAlert", "Failed"])
    alert = [c for c in sf.sns.calls if c["TopicArn"].endswith(":fp-alerts")]
    assert alert and TRF in alert[0]["Subject"]


def test_steps_are_idempotent_on_retry(sf):
    seed_demo(sf.ddb)
    sf.shortage()
    api(sf, TRF, "approval", {"approved_by": "jefe"})
    again = flow.ship({"id": TRF}, ddb=sf.ddb)  # Step Functions reintenta tras un timeout de la Lambda
    assert again["status"] == "in_transit" and stock(sf, "FUNDO-A")["available"] == 2
    api(sf, TRF, "receipt", {})
    flow.receive({"id": TRF, "receipt": {}}, ddb=sf.ddb)
    assert flow.allocate_backorders({"id": TRF}, ddb=sf.ddb)["allocated"] == []
    dest = stock(sf, "NORTE")
    assert (dest["on_hand"], dest["reserved"], dest["available"]) == (1, 1, 0)
    assert len(outbox_events(sf.ddb, "supply.received")) == 1 and len(outbox_events(sf.ddb, "part.reserved")) == 1
    with pytest.raises(docs.InvalidState):
        flow.place_order({"id": TRF}, ddb=sf.ddb)  # ya recibida: no vuelve a pedirse


def test_execution_for_already_closed_order_ends_quietly(sf):
    seed_demo(sf.ddb)
    sf.shortage()
    sf.machine.executions[TRF].time_out()  # cancelada
    ex = sf.machine.start_execution(stateMachineArn="arn:aws:states:us-east-1:0:stateMachine:supply-flow",
                                    name="again", input=json.dumps({"id": TRF, "kind": "transfer"}))
    assert ex and sf.machine.executions["again"].visited == ["Load", "Status?", "AlreadyClosed"]


def test_receipt_timeout_honours_demo_warp(monkeypatch):
    monkeypatch.setenv("DEMO_WARP", "60")
    assert flow._receipt_timeout_s(16) == 1920
    assert flow._receipt_timeout_s(0) == flow.MIN_RECEIPT_TIMEOUT_S


def test_order_born_approved_skips_the_approval_wait(sf):
    """POST manual de la API (T40) que ya nace aprobado: la ejecucion va directo a PlaceOrder."""
    seed_demo(sf.ddb, agro=0)
    sf.shortage()
    sf.ddb.update_item(TableName="fp-registry", Key={"PK": {"S": f"PO#{PO}"}, "SK": {"S": "META"}},
                       UpdateExpression="SET #s = :a", ExpressionAttributeNames={"#s": "status"},
                       ExpressionAttributeValues={":a": {"S": "approved"}})
    sf.machine.start_execution(stateMachineArn="arn:aws:states:us-east-1:0:stateMachine:supply-flow",
                               name="manual", input=json.dumps({"id": PO, "kind": "po"}))
    ex = sf.machine.executions["manual"]
    assert ex.visited[:4] == ["Load", "Status?", "Kind?", "PlaceOrder"] and ex.waiting_in == "WaitForReceipt"
    assert meta(sf, PO)["status"] == "ordered"


def test_t40_rejection_shape_plain_cause_also_cancels(sf):
    """T40 manda error `Rejected` y el motivo en texto plano; la API ya quito waiting_for antes de responder."""
    seed_demo(sf.ddb)
    sf.shortage()
    api(sf, TRF, "approval", error="Rejected", cause="agro lo necesita")
    d = meta(sf, TRF)
    assert (d["status"], d["cancel_reason"], "cancelled_by" in d) == ("cancelled", "agro lo necesita", False)


def test_t40_approval_output_shape(sf):
    seed_demo(sf.ddb)
    sf.shortage()
    api(sf, TRF, "approval", {"decision": "approved", "approved_by": "jefe@fleetpulse.pe", "override": False})
    assert meta(sf, TRF)["approved_by"] == "jefe@fleetpulse.pe"
    api(sf, TRF, "receipt", {"decision": "received", "received_by": "admin@mineria.pe"})
    assert meta(sf, TRF)["received_by"] == "admin@mineria.pe"
