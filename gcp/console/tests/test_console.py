import importlib
import io
import json
import os
import urllib.error

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")


@pytest.fixture
def mock_client(monkeypatch):
    monkeypatch.setenv("MOCK", "1")
    monkeypatch.setenv("STATIC_DIR", "/nonexistent")
    import app.main as main
    importlib.reload(main)
    return TestClient(main.app)


def test_fleet_json_in_sync_with_contracts():
    yaml = pytest.importorskip("yaml")
    with open(os.path.join(ROOT, "contracts", "ids.yaml"), encoding="utf-8") as f:
        expected = yaml.safe_load(f)
    with open(os.path.join(ROOT, "gcp", "console", "app", "fleet.json"), encoding="utf-8") as f:
        assert json.load(f) == expected, "cp simulator/fpsim/ids.json gcp/console/app/fleet.json"


def test_mock_machines_and_incidents(mock_client):
    assert mock_client.get("/api/health").json()["mode"] == "mock"
    machines = mock_client.get("/api/machines").json()["machines"]
    assert len(machines) == 100
    ex = next(m for m in machines if m["machine_id"] == "EX-017")
    assert ex["severity"] == "schedule" and ex["open_incident_id"]
    detail = mock_client.get("/api/machines/EX-009").json()
    assert detail["recurrences"] == 1 and len(detail["incidents"]) == 2
    open_incs = mock_client.get("/api/incidents?status=assigned").json()["incidents"]
    assert {i["machine_id"] for i in open_incs} >= {"EX-017", "EX-003", "VQ-011", "VQ-014", "TO-002"}


def test_close_updates_machine_and_stock(mock_client):
    inc = mock_client.get("/api/machines/EX-017").json()["open_incident_id"]
    before = {(s["part_id"], s["zone_id"]): s for s in mock_client.get("/api/stock").json()["stock"]}
    assert before[("PRT-04", "NORTE")]["reserved"] == 1
    r = mock_client.post(f"/api/incidents/{inc}/close", json={"closed_by": "TEC-01", "cause_summary": "Bomba cambiada"})
    assert r.status_code == 200
    d = mock_client.get(f"/api/incidents/{inc}").json()
    assert d["status"] == "closed" and d["timeline"][-1]["type"] == "incident.closed"
    after = {(s["part_id"], s["zone_id"]): s for s in mock_client.get("/api/stock").json()["stock"]}
    assert after[("PRT-04", "NORTE")]["on_hand"] == 0 and after[("PRT-04", "NORTE")]["reserved"] == 0
    assert mock_client.get("/api/machines/EX-017").json()["severity"] == "ok"


def test_planning_endpoints(mock_client):
    k = mock_client.get("/api/kpis").json()
    assert len(k["machines"]) == 100 and k["day"]
    by = {m["machine_id"]: m for m in k["machines"]}
    assert by["CF-042"]["quadrant"] == "inefficient" and by["CF-042"]["idle_ratio"] == 0.45
    assert by["VQ-031"]["quadrant"] == "underused" and by["MN-003"]["utilization"] > 0.6
    orders = mock_client.get("/api/orders").json()["orders"]
    assert orders and orders == sorted(orders, key=lambda o: o["due_at"])
    assert mock_client.get("/api/reorders").json()["reorders"]


def test_input_validation(mock_client):
    assert mock_client.get("/api/incidents/drop-table").status_code == 400
    assert mock_client.get("/api/machines/XX-1").status_code == 400
    assert mock_client.get("/api/incidents?status=weird").status_code == 400
    assert mock_client.get("/api/incidents/INC-EX-001-20261115T1430").status_code == 404


class Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_aws_proxy_sends_key_and_maps_errors(monkeypatch):
    monkeypatch.delenv("MOCK", raising=False)
    monkeypatch.setenv("STATIC_DIR", "/nonexistent")
    monkeypatch.setenv("AWS_REGISTRY_URL", "https://reg.example")
    monkeypatch.setenv("AWS_INCIDENTS_URL", "https://inc.example")
    import app.aws_api as aws_api
    import app.main as main
    importlib.reload(aws_api)
    importlib.reload(main)
    calls = []

    def opener(req, timeout):
        calls.append((req.get_method(), req.full_url, req.headers.get("X-api-key"), timeout))
        if req.full_url.endswith("/stock"):
            raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, io.BytesIO(b"{}"))
        return Resp(json.dumps({"machines": [], "incident_id": "x"}).encode())

    main.app.state.aws = aws_api.AwsApi(opener=opener, key_loader=lambda: "secret-key")
    c = TestClient(main.app)
    assert c.get("/api/machines").json() == {"machines": [], "incident_id": "x"}
    assert c.get("/api/stock").status_code == 502
    c.post("/api/incidents/INC-EX-017-20261115T1430/close", json={"closed_by": "TEC-01"})
    assert calls[0] == ("GET", "https://reg.example/machines", "secret-key", 6.0)
    assert calls[-1][:2] == ("POST", "https://inc.example/incidents/INC-EX-017-20261115T1430/close")


# ---------- T29: informe tecnico y plan del fabricante ----------

def test_oem_json_in_sync_and_endpoint(mock_client):
    yaml = pytest.importorskip("yaml")
    with open(os.path.join(ROOT, "contracts", "oem_specs.yaml"), encoding="utf-8") as f:
        expected = yaml.safe_load(f)
    with open(os.path.join(ROOT, "gcp", "console", "app", "oem.json"), encoding="utf-8") as f:
        assert json.load(f) == expected, "cp simulator/fpsim/oem.json gcp/console/app/oem.json"
    vq = mock_client.get("/api/oem/VQ").json()
    assert vq["illustrative"] is True and vq["service_schedule"] and vq["limits"]["coolant_temp_c"]["warning"] == 103
    assert mock_client.get("/api/oem/XX").status_code == 404


def test_mock_reports_match_the_incident_contract(mock_client):
    import jsonschema
    with open(os.path.join(ROOT, "contracts", "incident.schema.json"), encoding="utf-8") as f:
        schema = json.load(f)
    for mid in ("EX-017", "VQ-022"):
        inc_id = mock_client.get(f"/api/machines/{mid}").json()["open_incident_id"]
        inc = mock_client.get(f"/api/incidents/{inc_id}").json()
        assigned = next(t for t in inc["timeline"] if t["type"] == "incident.assigned")
        event = {"event_id": "3f2b8c1e-5d4a-4b7e-9a1c-2e6f8d0b4c11", "event_type": "incident.assigned",
                 "schema_version": 1, "source": "aws.incident-flow", "aggregate_type": "incident",
                 "aggregate_id": inc_id, "occurred_at": assigned["ts"], "incident_id": inc_id, "machine_id": mid,
                 "payload": assigned["payload"]}
        jsonschema.validate(event, schema)
        assert assigned["payload"]["explanation"]["report"]["findings"]
    vq = mock_client.get(f"/api/incidents/{mock_client.get('/api/machines/VQ-022').json()['open_incident_id']}").json()
    report = next(t for t in vq["timeline"] if t["type"] == "incident.assigned")["payload"]["explanation"]["report"]
    assert report["resonance"]["natural_frequency_hz"] == 62


# ---------- T34: consola por roles (modo de prueba con las reglas de aws/reports) ----------

def H(role):
    return {"authorization": f"Bearer mock.{role}"}


def test_auth_config_mock_lists_five_roles(mock_client):
    cfg = mock_client.get("/api/auth/config").json()
    assert cfg["mode"] == "mock" and {u["role"] for u in cfg["users"]} == {
        "operator", "technician", "supervisor", "management", "admin"}


def test_operator_reports_and_sees_checks(mock_client):
    assert mock_client.post("/api/reports", json={}).status_code == 401
    body = {"machine_id": "EX-017", "abnormality_type": "high_engine_temp",
            "description": "Sale vapor y el indicador de temperatura esta en rojo", "severity_perceived": "high"}
    r = mock_client.post("/api/reports", json=body, headers=H("operator"))
    assert r.status_code == 201
    rep = r.json()["report"]
    assert rep["recommendation"]["checks"] and rep["incident_id"] and rep["incident_link"] == "linked"
    assert rep["status"] == "in_review" and rep["assigned_tech_id"] == "TEC-01"
    assert mock_client.post("/api/reports", json=body, headers=H("technician")).status_code == 403
    mine = mock_client.get("/api/reports?mine=1", headers=H("operator")).json()["reports"]
    assert [x["report_id"] for x in mine] == [rep["report_id"]]
    linked = mock_client.get(f"/api/reports?incident_id={rep['incident_id']}", headers=H("supervisor")).json()["reports"]
    assert linked[0]["report_id"] == rep["report_id"]


def test_report_without_open_incident_starts_one_when_urgent(mock_client):
    body = {"machine_id": "CF-031", "abnormality_type": "smoke", "description": "Humo negro", "severity_perceived": "medium"}
    rep = mock_client.post("/api/reports", json=body, headers=H("operator")).json()["report"]
    assert rep["incident_link"] == "started"
    assert mock_client.get(f"/api/incidents/{rep['incident_id']}").json()["machine_id"] == "CF-031"


def test_technician_closes_assigned_report_once(mock_client):
    body = {"machine_id": "EX-017", "abnormality_type": "low_hydraulic_pressure", "description": "Pierde fuerza",
            "severity_perceived": "medium"}
    rid = mock_client.post("/api/reports", json=body, headers=H("operator")).json()["report"]["report_id"]
    close = {"findings": "Caudal bajo en la bomba", "confirmed_cause": "Bomba hidraulica gastada", "parts_used": []}
    assert mock_client.post(f"/api/reports/{rid}/close", json=close, headers=H("operator")).status_code == 403
    assigned = mock_client.get("/api/reports?mine=1", headers=H("technician")).json()["reports"]
    assert rid in [x["report_id"] for x in assigned]
    r = mock_client.post(f"/api/reports/{rid}/close", json=close, headers=H("technician"))
    assert r.status_code == 200 and r.json()["report"]["status"] == "closed"
    assert mock_client.post(f"/api/reports/{rid}/close", json=close, headers=H("technician")).status_code == 409


def test_technician_cannot_close_someone_elses_report(mock_client):
    body = {"machine_id": "VQ-022", "abnormality_type": "high_engine_temp", "description": "Temperatura alta",
            "severity_perceived": "high"}
    rep = mock_client.post("/api/reports", json=body, headers=H("operator")).json()["report"]
    assert rep["assigned_tech_id"] == "TEC-03"  # incidente de S7 asignado a TEC-03; el usuario de prueba es TEC-01
    close = {"findings": "x", "confirmed_cause": "y"}
    assert mock_client.post(f"/api/reports/{rep['report_id']}/close", json=close, headers=H("technician")).status_code == 403
    assert mock_client.post(f"/api/reports/{rep['report_id']}/close", json=close, headers=H("supervisor")).status_code == 200


def test_admin_changes_technician_and_types(mock_client):
    assert mock_client.put("/api/admin/technicians/TEC-01", json={"available": False}, headers=H("operator")).status_code == 403
    t = mock_client.put("/api/admin/technicians/TEC-01", json={"available": False}, headers=H("admin")).json()
    assert t["technician"]["available"] is False
    listing = mock_client.get("/api/admin/technicians", headers=H("admin")).json()
    assert len(listing["technicians"]) == 6 and "NORTE" in listing["allowed"]["zone_id"]
    assert mock_client.get("/api/admin/technicians", headers=H("operator")).status_code == 403
    assert mock_client.post("/api/admin/abnormality-types", json={"code": "brake_noise", "label_es": "Ruido de frenos"},
                            headers=H("admin")).status_code == 201
    codes = [x["code"] for x in mock_client.get("/api/abnormality-types", headers=H("operator")).json()["abnormality_types"]]
    assert "brake_noise" in codes and len(codes) == 11
    again = mock_client.post("/api/admin/abnormality-types", json={"code": "brake_noise", "label_es": "Chirrido de frenos"},
                             headers=H("admin"))
    assert again.status_code == 200  # reemplaza, como admin.py
    assert mock_client.delete("/api/admin/abnormality-types/brake_noise", headers=H("admin")).status_code == 200


def test_real_mode_forwards_the_cognito_token(monkeypatch):
    monkeypatch.delenv("MOCK", raising=False)
    monkeypatch.setenv("STATIC_DIR", "/nonexistent")
    monkeypatch.setenv("AWS_INCIDENTS_URL", "https://api.example")
    monkeypatch.setenv("AUTH_USER_POOL_ID", "us-east-1_Abc123")
    monkeypatch.setenv("AUTH_CLIENT_ID", "client123")
    monkeypatch.setenv("AUTH_DOMAIN", "fleetpulse-1.auth.us-east-1.amazoncognito.com")
    import app.aws_api as aws_api
    import app.main as main
    importlib.reload(aws_api)
    importlib.reload(main)
    seen = []

    def opener(req, timeout):
        seen.append((req.get_method(), req.full_url, req.headers.get("Authorization"), req.headers.get("X-api-key")))
        if req.get_method() == "POST":
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"error":"forbidden"}'))
        return Resp(json.dumps({"reports": []}).encode())

    main.app.state.aws = aws_api.AwsApi(opener=opener, key_loader=lambda: "k")
    c = TestClient(main.app)
    cfg = c.get("/api/auth/config").json()
    assert cfg["mode"] == "cognito" and cfg["authority"].endswith("/us-east-1_Abc123") and cfg["domain"].startswith("https://")
    assert c.get("/api/reports?mine=1", headers={"authorization": "Bearer eyJ.tok.en"}).json() == {"reports": []}
    assert seen[0] == ("GET", "https://api.example/reports?mine=1", "Bearer eyJ.tok.en", None)
    r = c.post("/api/reports", json={"machine_id": "EX-017"}, headers={"authorization": "Bearer eyJ.tok.en"})
    assert r.status_code == 403 and r.json() == {"error": "forbidden"}
    assert c.get("/api/reports").status_code == 401
