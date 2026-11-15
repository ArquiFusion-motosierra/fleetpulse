"""Pruebas de la Lambda `supply-api` (T40): cada ruta `/supply/*`, roles, aislamiento por parque, 404 cruzado y 409.
fp-registry en moto y Step Functions con un doble (sin red)."""
import boto3
import pytest
from moto import mock_aws
from supply_api_support import (
    ADMIN_AGRO,
    ADMIN_IND,
    ADMIN_MIN,
    DOC_KEYS,
    HEAD,
    INC,
    KPI_KEYS,
    MGMT_MIN,
    NOW,
    OPERATOR,
    PO_IND,
    PO_SUR,
    STOCK_KEYS,
    SUP_MIN,
    TECH_AGRO,
    TECH_MIN,
    TOKEN,
    TRF,
    TRF_KEYS,
    FakeSfn,
    api_event,
    body_of,
    capture_logs,
    log_lines,
    make_registry,
    seed_demo,
    seed_stock,
)

from fplib import clock

FLOW_ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:supply-flow"


@pytest.fixture
def supplyapi(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "x"), ("AWS_SECRET_ACCESS_KEY", "x"), ("AWS_DEFAULT_REGION", "us-east-1"),
                 ("REGISTRY_TABLE", "fp-registry"), ("SUPPLY_FLOW_ARN", FLOW_ARN)):
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(clock, "now", lambda: NOW)
    from api import supply_api

    with mock_aws():
        reg = make_registry(boto3.resource("dynamodb", region_name="us-east-1"))
        seed_stock(reg)
        seed_demo(reg)
        sfn = FakeSfn()

        class Svc:
            registry = reg

            def __init__(self):
                self.sfn = sfn

            def call(self, route, claims=None, **kw):
                resp = supply_api.handler(api_event(route, claims, **kw), registry=reg, sfn=self.sfn)
                return resp["statusCode"], body_of(resp)

            def meta(self, doc_id):
                pk = ("TRF#" if doc_id.startswith("TRF-") else "PO#") + doc_id
                return reg.get_item(Key={"PK": pk, "SK": "META"}).get("Item")

        yield Svc()


# ---------- entrada ----------

def test_sin_claims_401_y_ruta_desconocida_404(supplyapi):
    assert supplyapi.call("GET /supply/stock")[0] == 401
    assert supplyapi.call("GET /supply/nada", HEAD) == (404, {"error": "ROUTE_NOT_FOUND"})


# ---------- stock ----------

def test_stock_del_parque_con_la_forma_de_la_consola(supplyapi):
    status, body = supplyapi.call("GET /supply/stock", ADMIN_MIN)
    assert status == 200
    rows = body["stock"]
    assert rows and {r["tenant_id"] for r in rows} == {"mineria"}
    assert STOCK_KEYS <= set(rows[0])
    pump = [r for r in rows if r["part_id"] == "PRT-04"]
    assert [r["zone_id"] for r in pump] == ["NORTE", "CENTRO", "SUR"]  # orden del catalogo
    assert all(r["status"] == "stockout" and r["available"] == 0 for r in pump)
    assert pump[0]["criticality"] == "A" and pump[0]["safety_stock"] == 1
    assert rows == sorted(rows, key=lambda r: r["part_id"])


def test_stock_jefe_ve_los_tres_parques_y_filtra(supplyapi):
    _, body = supplyapi.call("GET /supply/stock", HEAD)
    assert {r["tenant_id"] for r in body["stock"]} == {"mineria", "agro", "industria"}
    _, body = supplyapi.call("GET /supply/stock", HEAD, qs={"tenant": "agro", "part": "PRT-04"})
    assert [(r["zone_id"], r["status"]) for r in body["stock"]] == [("FUNDO-A", "ok"), ("FUNDO-B", "ok"),
                                                                    ("PACKING", "ok")]
    _, body = supplyapi.call("GET /supply/stock", HEAD, qs={"zone": "NORTE", "part": "PRT-04"})
    assert len(body["stock"]) == 1


def test_stock_tecnico_si_operador_no(supplyapi):
    assert supplyapi.call("GET /supply/stock", TECH_MIN)[0] == 200
    status, body = supplyapi.call("GET /supply/stock", OPERATOR)
    assert status == 403 and body["error"] == "FORBIDDEN"


def test_stock_tenant_ajeno_403_con_log_sin_token(supplyapi, caplog):
    capture_logs(caplog)
    status, body = supplyapi.call("GET /supply/stock", ADMIN_MIN, qs={"tenant": "agro"})
    assert status == 403 and body["error"] == "TENANT_FORBIDDEN"
    (line,) = log_lines(caplog, "supply_prohibido")
    assert line["status"] == 403 and line["tenant"] == "mineria"
    assert TOKEN not in caplog.text and "Bearer" not in caplog.text


def test_stock_filtro_invalido_400(supplyapi):
    assert supplyapi.call("GET /supply/stock", HEAD, qs={"zone": "norte; drop"})[0] == 400


def test_stock_demanda_por_consumo_baja_la_cobertura(supplyapi):
    supplyapi.registry.put_item(Item={"PK": "MOVE#PRT-01#NORTE", "SK": "2026-11-10T10:00:00Z#INC-X", "qty": -30,
                                      "kind": "consumption"})
    supplyapi.registry.put_item(Item={"PK": "MOVE#PRT-01#NORTE", "SK": "2026-09-01T10:00:00Z#INC-OLD", "qty": -300,
                                      "kind": "consumption"})  # fuera de los 30 dias
    _, body = supplyapi.call("GET /supply/stock", HEAD, qs={"zone": "NORTE", "part": "PRT-01"})
    (row,) = body["stock"]
    assert row["demand_d"] == 1.0
    assert row["days_of_coverage"] == row["available"] / 1.0
    assert row["rop"] >= row["reorder_point"]


# ---------- sugerencias y listados ----------

def test_sugerencias_por_parque_destino_u_origen(supplyapi):
    _, body = supplyapi.call("GET /supply/suggestions", ADMIN_MIN)
    assert [d["id"] for d in body["suggestions"]] == [PO_SUR, TRF]  # mas reciente primero
    trf = body["suggestions"][1]
    assert TRF_KEYS <= set(trf)
    assert DOC_KEYS <= set(body["suggestions"][0])
    assert trf["kind"] == "transfer" and trf["cost_usd"] == 180 and trf["eta_h"] == 16
    assert trf["options"][0]["source_kind"] == "transfer" and trf["incident_id"] == INC
    assert "task_token" not in trf and TOKEN not in str(body)
    _, body = supplyapi.call("GET /supply/suggestions", ADMIN_AGRO)  # donante: ve la transferencia saliente
    assert [d["id"] for d in body["suggestions"]] == [TRF]
    _, body = supplyapi.call("GET /supply/suggestions", ADMIN_IND)
    assert body["suggestions"] == []
    _, body = supplyapi.call("GET /supply/suggestions", HEAD)
    assert len(body["suggestions"]) == 2


def test_sugerencias_roles(supplyapi):
    assert supplyapi.call("GET /supply/suggestions", SUP_MIN)[0] == 200
    assert supplyapi.call("GET /supply/suggestions", MGMT_MIN)[0] == 403
    assert supplyapi.call("GET /supply/suggestions", TECH_MIN)[0] == 403


def test_listado_de_ordenes_con_estado_y_linea_de_tiempo(supplyapi):
    _, body = supplyapi.call("GET /supply/purchase-orders", HEAD)
    assert [d["id"] for d in body["purchase_orders"]] == [PO_SUR, PO_IND]
    ind = body["purchase_orders"][1]
    assert ind["cost_usd"] == 285 and ind["eta_h"] == 120 and ind["updated_at"] == "2026-11-13T15:11:00Z"
    assert [e["type"] for e in ind["timeline"]] == ["supply.suggested", "supply.approved", "supply.ordered"]
    sur = body["purchase_orders"][0]
    assert sur["timeline"][0]["type"] == "supply.suggested"  # sin EV#: se arma desde created_at
    _, body = supplyapi.call("GET /supply/purchase-orders", HEAD, qs={"status": "ordered"})
    assert [d["id"] for d in body["purchase_orders"]] == [PO_IND]
    _, body = supplyapi.call("GET /supply/purchase-orders", ADMIN_IND)
    assert [d["id"] for d in body["purchase_orders"]] == [PO_IND]
    assert supplyapi.call("GET /supply/purchase-orders", HEAD, qs={"status": "perdida"})[0] == 400


def test_listado_de_transferencias_y_roles(supplyapi):
    _, body = supplyapi.call("GET /supply/transfers", ADMIN_AGRO)
    assert [d["id"] for d in body["transfers"]] == [TRF]
    _, body = supplyapi.call("GET /supply/transfers", ADMIN_IND)
    assert body["transfers"] == []
    assert supplyapi.call("GET /supply/transfers", MGMT_MIN)[0] == 403  # management ve PO, no TRF
    assert supplyapi.call("GET /supply/purchase-orders", MGMT_MIN)[0] == 200


def test_detalle_por_id_y_404_cruzado(supplyapi, caplog):
    capture_logs(caplog)
    status, doc = supplyapi.call("GET /supply/purchase-orders/{id}", ADMIN_IND, path={"id": PO_IND})
    assert status == 200 and doc["id"] == PO_IND and DOC_KEYS <= set(doc) and "task_token" not in doc
    status, body = supplyapi.call("GET /supply/purchase-orders/{id}", ADMIN_MIN, path={"id": PO_IND})
    assert status == 404 and body["error"] == "NOT_FOUND"
    assert log_lines(caplog, "supply_otro_parque")
    assert supplyapi.call("GET /supply/transfers/{id}", ADMIN_AGRO, path={"id": TRF})[0] == 200  # origen
    assert supplyapi.call("GET /supply/transfers/{id}", HEAD, path={"id": PO_SUR})[0] == 404  # otro tipo
    assert supplyapi.call("GET /supply/purchase-orders/{id}", HEAD,
                          path={"id": "PO-NORTE-PRT-01-20990101T0000"})[0] == 404


# ---------- aprobar / rechazar ----------

def test_jefe_aprueba_la_transferencia(supplyapi, caplog):
    capture_logs(caplog)
    status, doc = supplyapi.call("POST /supply/transfers/{id}/approve", HEAD, path={"id": TRF}, body={})
    assert status == 200
    assert doc["status"] == "approved" and doc["flow_pending"] is True and doc["approved_by"]
    ((name, kw),) = supplyapi.sfn.calls
    assert name == "send_task_success" and kw["taskToken"] == TOKEN
    assert '"decision": "approved"' in kw["output"] and '"override": false' in kw["output"]
    meta = supplyapi.meta(TRF)
    assert "waiting_for" not in meta and meta["approved_by"] == doc["approved_by"]
    assert TOKEN not in caplog.text
    # doble clic: la espera ya se tomo
    status, body = supplyapi.call("POST /supply/transfers/{id}/approve", HEAD, path={"id": TRF}, body={})
    assert status == 409 and body["error"] == "INVALID_STATE"
    assert len(supplyapi.sfn.calls) == 1


def test_transferencia_la_aprueba_solo_el_jefe(supplyapi):
    status, body = supplyapi.call("POST /supply/transfers/{id}/approve", ADMIN_MIN, path={"id": TRF}, body={})
    assert status == 403 and body["error"] == "FORBIDDEN"
    status, _ = supplyapi.call("POST /supply/transfers/{id}/approve", ADMIN_AGRO, path={"id": TRF}, body={})
    assert status == 403  # el donante la ve pero no la aprueba
    status, _ = supplyapi.call("POST /supply/transfers/{id}/approve", ADMIN_IND, path={"id": TRF}, body={})
    assert status == 404  # ni destino ni origen: como si no existiera
    assert supplyapi.sfn.calls == []


def test_donante_bajo_seguridad_409_salvo_override(supplyapi):
    supplyapi.registry.update_item(Key={"PK": "STOCK#PRT-04#FUNDO-A", "SK": "META"},
                                   UpdateExpression="SET available = :a", ExpressionAttributeValues={":a": 1})
    status, body = supplyapi.call("POST /supply/transfers/{id}/approve", HEAD, path={"id": TRF}, body={})
    assert status == 409 and body["error"] == "SOURCE_BELOW_SAFETY"
    status, doc = supplyapi.call("POST /supply/transfers/{id}/approve", HEAD, path={"id": TRF},
                                 body={"override": True})
    assert status == 200 and '"override": true' in supplyapi.sfn.calls[0][1]["output"]
    assert supplyapi.meta(TRF)["override"] is True


def test_admin_del_parque_aprueba_po_chica(supplyapi):
    status, doc = supplyapi.call("POST /supply/purchase-orders/{id}/approve", SUP_MIN, path={"id": PO_SUR})
    assert status == 200 and doc["status"] == "approved"
    assert supplyapi.sfn.calls[0][1]["taskToken"] == TOKEN + "-sur"


def test_po_de_otro_parque_404_y_rol_sin_permiso_403(supplyapi):
    assert supplyapi.call("POST /supply/purchase-orders/{id}/approve", ADMIN_AGRO, path={"id": PO_SUR})[0] == 404
    assert supplyapi.call("POST /supply/purchase-orders/{id}/approve", MGMT_MIN, path={"id": PO_SUR})[0] == 403
    assert supplyapi.call("POST /supply/purchase-orders/{id}/approve", TECH_MIN, path={"id": PO_SUR})[0] == 403


def test_rechazo_manda_send_task_failure(supplyapi):
    status, doc = supplyapi.call("POST /supply/purchase-orders/{id}/reject", ADMIN_MIN, path={"id": PO_SUR},
                                 body={"reason": "ya hay filtros en camino"})
    assert status == 200 and doc["status"] == "cancelled"
    ((name, kw),) = supplyapi.sfn.calls
    assert name == "send_task_failure" and kw["error"] == "Rejected" and "ya hay filtros" in kw["cause"]
    assert supplyapi.meta(PO_SUR)["rejected_by"]


def test_aprobar_algo_ya_pedido_409(supplyapi):
    status, body = supplyapi.call("POST /supply/purchase-orders/{id}/approve", HEAD, path={"id": PO_IND})
    assert status == 409 and body["status"] == "ordered"


def test_token_vencido_409_y_error_transitorio_devuelve_la_espera(supplyapi):
    supplyapi.sfn.fail = {"send_task_success": "ThrottlingException"}
    status, body = supplyapi.call("POST /supply/purchase-orders/{id}/approve", ADMIN_MIN, path={"id": PO_SUR})
    assert status == 502 and body["error"] == "FLOW_UNAVAILABLE"
    assert supplyapi.meta(PO_SUR)["waiting_for"] == "approval"  # se puede reintentar
    supplyapi.sfn.fail = {"send_task_success": "TaskTimedOut"}
    status, body = supplyapi.call("POST /supply/purchase-orders/{id}/approve", ADMIN_MIN, path={"id": PO_SUR})
    assert status == 409 and body["error"] == "TASK_EXPIRED"


# ---------- recepcion ----------

def test_recepcion_por_el_parque_destino(supplyapi):
    assert supplyapi.call("POST /supply/purchase-orders/{id}/receive", ADMIN_MIN, path={"id": PO_IND})[0] == 404
    status, body = supplyapi.call("POST /supply/purchase-orders/{id}/receive", MGMT_MIN, path={"id": PO_SUR})
    assert status == 403
    status, doc = supplyapi.call("POST /supply/purchase-orders/{id}/receive", ADMIN_IND, path={"id": PO_IND})
    assert status == 200 and doc["status"] == "received" and doc["received_by"]
    ((name, kw),) = supplyapi.sfn.calls
    assert name == "send_task_success" and kw["taskToken"] == TOKEN + "-ind" and "received" in kw["output"]
    status, body = supplyapi.call("POST /supply/purchase-orders/{id}/receive", HEAD, path={"id": PO_IND})
    assert status == 409  # doble clic


def test_recepcion_de_algo_sugerido_409(supplyapi):
    status, body = supplyapi.call("POST /supply/purchase-orders/{id}/receive", TECH_MIN, path={"id": PO_SUR})
    assert status == 409 and body["error"] == "INVALID_STATE"


def test_donante_no_confirma_la_recepcion(supplyapi):
    supplyapi.registry.update_item(Key={"PK": f"TRF#{TRF}", "SK": "META"},
                                   UpdateExpression="SET #s = :s, waiting_for = :w",
                                   ExpressionAttributeNames={"#s": "status"},
                                   ExpressionAttributeValues={":s": "in_transit", ":w": "receipt"})
    status, body = supplyapi.call("POST /supply/transfers/{id}/receive", TECH_AGRO, path={"id": TRF})
    assert status == 403 and body["error"] == "TENANT_FORBIDDEN"
    assert supplyapi.call("POST /supply/transfers/{id}/receive", TECH_MIN, path={"id": TRF})[0] == 200


# ---------- creacion manual ----------

def test_po_manual_inicia_supply_flow(supplyapi):
    status, doc = supplyapi.call("POST /supply/purchase-orders", ADMIN_MIN,
                                 body={"zone_id": "CENTRO", "part_id": "PRT-01", "qty": 5, "supplier_id": "SUP-FILTR"})
    assert status == 201
    assert doc["id"] == "PO-CENTRO-PRT-01-20261115T1500" and doc["status"] == "suggested"
    assert doc["cost_usd"] == 5 * 42 and doc["eta_h"] == 48 and doc["source"] == "manual"
    assert doc["approver_roles"] == ["admin", "supervisor", "maintenance_head"]
    ((name, kw),) = supplyapi.sfn.calls
    assert name == "start_execution" and kw["name"] == doc["id"] and kw["stateMachineArn"] == FLOW_ARN
    assert '"kind": "po"' in kw["input"]
    meta = supplyapi.meta(doc["id"])
    assert meta["GSI3PK"] == "TENANT#mineria" and meta["status"] == "suggested"
    _, body = supplyapi.call("GET /supply/suggestions", ADMIN_MIN)
    assert doc["id"] in [d["id"] for d in body["suggestions"]]
    # mismo minuto, mismo id: no se duplica
    status, body = supplyapi.call("POST /supply/purchase-orders", ADMIN_MIN,
                                  body={"zone_id": "CENTRO", "part_id": "PRT-01", "qty": 5, "supplier_id": "SUP-FILTR"})
    assert status == 409 and body["error"] == "ALREADY_EXISTS"


def test_po_manual_cara_o_critica_la_aprueba_el_jefe(supplyapi):
    status, doc = supplyapi.call("POST /supply/purchase-orders", HEAD,
                                 body={"zone_id": "NORTE", "part_id": "PRT-04", "qty": 1, "supplier_id": "SUP-HIDRA"})
    assert status == 201 and doc["approver_roles"] == ["maintenance_head"]


def test_po_manual_validaciones(supplyapi):
    base = {"zone_id": "CENTRO", "part_id": "PRT-01", "qty": 5, "supplier_id": "SUP-FILTR"}
    status, body = supplyapi.call("POST /supply/purchase-orders", ADMIN_MIN, body=base | {"zone_id": "FUNDO-A"})
    assert status == 403 and body["error"] == "TENANT_FORBIDDEN"
    assert supplyapi.call("POST /supply/purchase-orders", SUP_MIN, body=base)[0] == 403
    for bad in ({"qty": 0}, {"qty": "5"}, {"qty": 2}, {"supplier_id": "SUP-HIDRA"}, {"zone_id": "LUNA"},
                {"part_id": "PRT-99"}):
        assert supplyapi.call("POST /supply/purchase-orders", ADMIN_MIN, body=base | bad)[0] == 400, bad
    assert supplyapi.call("POST /supply/purchase-orders", ADMIN_MIN, body="no es json")[0] == 400
    assert supplyapi.sfn.calls == []


def test_po_manual_si_step_functions_falla_no_queda_el_meta(supplyapi):
    supplyapi.sfn.fail = {"start_execution": "ServiceUnavailable"}
    status, body = supplyapi.call("POST /supply/purchase-orders", ADMIN_MIN,
                                  body={"zone_id": "CENTRO", "part_id": "PRT-01", "qty": 5, "supplier_id": "SUP-FILTR"})
    assert status == 502
    assert supplyapi.meta("PO-CENTRO-PRT-01-20261115T1500") is None


def test_transferencia_manual_solo_jefe(supplyapi):
    body = {"from_zone": "FUNDO-B", "to_zone": "CENTRO", "part_id": "PRT-04", "qty": 1}
    assert supplyapi.call("POST /supply/transfers", ADMIN_MIN, body=body)[0] == 403
    status, doc = supplyapi.call("POST /supply/transfers", HEAD, body=body)
    assert status == 201
    assert doc["id"] == "TRF-FUNDO-B-CENTRO-PRT-04-20261115T1500"
    assert doc["tenant_id"] == "mineria" and doc["from_tenant"] == "agro"
    assert doc["eta_h"] == 16 and doc["cost_usd"] == 180 and doc["approver_roles"] == ["maintenance_head"]
    assert '"kind": "transfer"' in supplyapi.sfn.calls[0][1]["input"]
    _, listed = supplyapi.call("GET /supply/transfers", ADMIN_AGRO)  # el donante la ve por GSI1 TRFFROM#
    assert doc["id"] in [d["id"] for d in listed["transfers"]]
    assert supplyapi.call("POST /supply/transfers", HEAD, body=body | {"to_zone": "FUNDO-B"})[0] == 400


# ---------- proveedores, movimientos, KPIs ----------

def test_proveedores(supplyapi):
    status, body = supplyapi.call("GET /supply/suppliers", ADMIN_MIN)
    assert status == 200
    hidra = next(s for s in body["suppliers"] if s["supplier_id"] == "SUP-HIDRA")
    assert {"part_id": "PRT-04", "unit_price_usd": 6800, "lead_time_d": 7, "min_qty": 1} in hidra["parts"]
    assert supplyapi.call("GET /supply/suppliers", SUP_MIN)[0] == 403


def test_movimientos_por_parque(supplyapi):
    supplyapi.registry.put_item(Item={"PK": "MOVE#PRT-04#FUNDO-A", "SK": f"2026-11-15T14:50:00Z#{TRF}", "qty": -1,
                                      "kind": "transfer_out", "ref": TRF, "tenant_id": "agro"})
    _, body = supplyapi.call("GET /supply/movements", ADMIN_AGRO, qs={"part": "PRT-04"})
    assert body["movements"] == [{"part_id": "PRT-04", "zone_id": "FUNDO-A", "tenant_id": "agro",
                                  "kind": "transfer_out", "qty": -1, "ref": TRF, "at": "2026-11-15T14:50:00Z"}]
    _, body = supplyapi.call("GET /supply/movements", ADMIN_MIN, qs={"part": "PRT-04"})
    assert body["movements"] == []
    assert supplyapi.call("GET /supply/movements", ADMIN_MIN, qs={"tenant": "agro"})[0] == 403
    assert supplyapi.call("GET /supply/movements", TECH_MIN)[0] == 403


def test_kpis(supplyapi):
    status, k = supplyapi.call("GET /supply/kpis", HEAD)
    assert status == 200 and KPI_KEYS <= set(k)
    assert k["downtime_avoided_h"] == 4 and k["downtime_avoided_usd"] == 600  # solo la PO ya pedida
    assert k["open_stockouts"] == 3  # PRT-04 en los 3 almacenes de mina
    assert set(k["inventory_value_usd"]) == {"mineria", "agro", "industria"}
    assert 0 < k["fill_rate"] < 1
    _, k = supplyapi.call("GET /supply/kpis", MGMT_MIN)
    assert set(k["inventory_value_usd"]) == {"mineria"} and k["downtime_avoided_h"] == 0
    assert supplyapi.call("GET /supply/kpis", SUP_MIN)[0] == 403
    assert supplyapi.call("GET /supply/kpis", ADMIN_MIN, qs={"tenant": "industria"})[0] == 403
