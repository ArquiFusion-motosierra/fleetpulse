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
