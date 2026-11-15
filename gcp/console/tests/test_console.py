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
    pytest.importorskip("yaml")
    with open(os.path.join(ROOT, "simulator", "fpsim", "ids.json"), encoding="utf-8") as f:
        expected = json.load(f)  # union de los parques (simulator/fpsim/sync_ids.py; test_ids_in_sync la compara)
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
        if "/stock" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, io.BytesIO(b"{}"))
        return Resp(json.dumps({"machines": [], "incident_id": "x"}).encode())

    main.app.state.aws = aws_api.AwsApi(opener=opener, key_loader=lambda: "secret-key")
    c = TestClient(main.app)
    assert c.get("/api/machines").json() == {"machines": [], "incident_id": "x"}
    assert c.get("/api/stock").status_code == 502
    c.post("/api/incidents/INC-EX-017-20261115T1430/close", json={"closed_by": "TEC-01"})
    assert calls[0] == ("GET", "https://reg.example/machines?tenant=mineria", "secret-key", 6.0)  # sin token: mineria
    assert calls[-2][:2] == ("GET", "https://reg.example/incidents/INC-EX-017-20261115T1430?tenant=mineria")
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


def test_auth_config_mock_lists_the_roles(mock_client):
    cfg = mock_client.get("/api/auth/config").json()
    assert cfg["mode"] == "mock" and {u["role"] for u in cfg["users"]} == {
        "operator", "technician", "supervisor", "management", "admin", "maintenance_head"}


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


# ---------- multi-tenancy: el parque sale del token ----------

def _as(role):
    return {"authorization": f"Bearer mock.{role}"}


def test_park_users_only_see_their_park(mock_client):
    mine = mock_client.get("/api/machines", headers=_as("supervisor")).json()["machines"]
    assert len(mine) == 100 and {m["tenant_id"] for m in mine} == {"mineria"}
    agro = mock_client.get("/api/machines", headers=_as("admin_agro")).json()["machines"]
    assert len(agro) == 12 and {m["tenant_id"] for m in agro} == {"agro"}
    incs = mock_client.get("/api/incidents", headers=_as("admin_agro")).json()["incidents"]
    assert [i["machine_id"] for i in incs] == ["CS-003"]
    stock = mock_client.get("/api/stock", headers=_as("admin_agro")).json()["stock"]
    assert stock and {s["zone_id"] for s in stock} == {"FUNDO-A", "FUNDO-B", "PACKING"}


def test_park_user_cannot_pick_another_park_or_open_its_resources(mock_client):
    assert mock_client.get("/api/machines?tenant=mineria", headers=_as("admin_agro")).status_code == 403
    inc = mock_client.get("/api/incidents", headers=_as("supervisor")).json()["incidents"][0]["incident_id"]
    assert mock_client.get(f"/api/incidents/{inc}", headers=_as("admin_agro")).status_code == 404
    assert mock_client.post(f"/api/incidents/{inc}/close", json={}, headers=_as("admin_agro")).status_code == 404
    assert mock_client.get("/api/machines/EX-017", headers=_as("admin_agro")).status_code == 404


def test_maintenance_head_sees_every_park_and_can_filter(mock_client):
    head = _as("maintenance_head")
    assert len(mock_client.get("/api/machines", headers=head).json()["machines"]) == 124
    ind = mock_client.get("/api/incidents?tenant=industria", headers=head).json()["incidents"]
    assert [i["machine_id"] for i in ind] == ["CP-002"]
    t = mock_client.get("/api/tenants", headers=head).json()
    assert t["global"] is True and t["scope"] == ["mineria", "agro", "industria"] and len(t["tenants"]) == 3
    park = mock_client.get("/api/tenants", headers=_as("admin_agro")).json()
    assert park["global"] is False and park["scope"] == ["agro"] and park["tenant_id"] == "agro"
    assert mock_client.get("/api/machines?tenant=luna", headers=head).status_code == 400


def test_real_mode_resolves_the_park_with_whoami_and_caches_it(monkeypatch):
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
        calls.append((req.get_method(), req.full_url, req.headers.get("Authorization")))
        if req.full_url.endswith("/whoami"):
            return Resp(json.dumps({"role": "admin", "tenant_id": "agro", "tenants": ["agro"], "global": False}).encode())
        return Resp(json.dumps({"machines": []}).encode())

    main.app.state.aws = aws_api.AwsApi(opener=opener, key_loader=lambda: "secret-key")
    c = TestClient(main.app)
    h = {"authorization": "Bearer tok-agro"}
    assert c.get("/api/machines", headers=h).status_code == 200
    assert c.get("/api/stock", headers=h).status_code == 200
    assert c.get("/api/machines?tenant=mineria", headers=h).status_code == 403
    whoami = [x for x in calls if x[1].endswith("/whoami")]
    assert len(whoami) == 1 and whoami[0] == ("GET", "https://inc.example/whoami", "Bearer tok-agro")  # cache 5 min
    assert ("GET", "https://reg.example/machines?tenant=agro", None) in calls


# ---------- T34: cierre de incidentes con login (POST /me/incidents/{id}/close) ----------

def _open_inc(client, mid):
    return client.get(f"/api/machines/{mid}").json()["open_incident_id"]


def test_mock_close_with_login_follows_aws_roles(mock_client):
    own, other = _open_inc(mock_client, "EX-017"), _open_inc(mock_client, "VQ-022")  # TEC-01 y TEC-03
    assert mock_client.post(f"/api/incidents/{own}/close", json={}, headers=H("operator")).status_code == 403
    assert mock_client.post(f"/api/incidents/{other}/close", json={}, headers=H("technician")).status_code == 403
    r = mock_client.post(f"/api/incidents/{own}/close", json={"closed_by": "otro", "cause_summary": "Bomba"},
                         headers=H("technician"))
    assert r.status_code == 200
    closed = mock_client.get(f"/api/incidents/{own}").json()["timeline"][-1]
    assert closed["type"] == "incident.closed" and closed["payload"]["closed_by"] == "tecnico@fleetpulse.test"
    assert mock_client.post(f"/api/incidents/{other}/close", json={}, headers=H("supervisor")).status_code == 200
    assert mock_client.post(f"/api/incidents/{own}/close", json={}, headers={"authorization": "Bearer x"}).status_code == 401


def _aws_client(monkeypatch, auth=False):
    monkeypatch.delenv("MOCK", raising=False)
    monkeypatch.setenv("STATIC_DIR", "/nonexistent")
    monkeypatch.setenv("AWS_REGISTRY_URL", "https://reg.example")
    monkeypatch.setenv("AWS_INCIDENTS_URL", "https://inc.example")
    for k, v in (("AUTH_USER_POOL_ID", "us-east-1_abc"), ("AUTH_CLIENT_ID", "cid"), ("AUTH_DOMAIN", "fp.auth.example")):
        if auth:
            monkeypatch.setenv(k, v)
        else:
            monkeypatch.delenv(k, raising=False)
    import app.aws_api as aws_api
    import app.main as main
    importlib.reload(aws_api)
    importlib.reload(main)
    calls = []

    def opener(req, timeout):
        calls.append({"method": req.get_method(), "url": req.full_url, "key": req.headers.get("X-api-key"),
                      "auth": req.headers.get("Authorization"), "body": json.loads(req.data or b"{}")})
        if "TEC-99" in json.dumps(calls[-1]["body"]):
            raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, io.BytesIO(b'{"error": "forbidden"}'))
        return Resp(json.dumps({"incident_id": "INC-EX-017-20261115T1430", "downtime_h": 1.5}).encode())

    main.app.state.aws = aws_api.AwsApi(opener=opener, key_loader=lambda: "secret-key")
    return TestClient(main.app), calls


def test_aws_close_with_login_forwards_the_id_token_to_the_user_route(monkeypatch):
    c, calls = _aws_client(monkeypatch, auth=True)
    r = c.post("/api/incidents/INC-EX-017-20261115T1430/close", headers={"authorization": "Bearer id.tok.en"},
               json={"closed_by": "suplantado", "cause_summary": "Bomba cambiada",
                     "parts_used": [{"part_id": "PRT-04", "zone_id": "NORTE", "qty": 1}]})
    assert r.status_code == 200 and r.json()["downtime_h"] == 1.5
    (call,) = calls
    assert call["method"] == "POST" and call["url"] == "https://inc.example/me/incidents/INC-EX-017-20261115T1430/close"
    assert call["auth"] == "Bearer id.tok.en" and call["key"] is None
    assert call["body"] == {"cause_summary": "Bomba cambiada",
                            "parts_used": [{"part_id": "PRT-04", "zone_id": "NORTE", "qty": 1}]}
    r = c.post("/api/incidents/INC-EX-017-20261115T1430/close", headers={"authorization": "Bearer id.tok.en"},
               json={"cause_summary": "TEC-99"})
    assert r.status_code == 403  # el rol lo decide AWS y la consola devuelve su respuesta


def test_aws_close_without_token_is_401_once_login_is_configured(monkeypatch):
    c, calls = _aws_client(monkeypatch, auth=True)
    assert c.post("/api/incidents/INC-EX-017-20261115T1430/close", json={"closed_by": "TEC-01"}).status_code == 401
    assert calls == []
    c, calls = _aws_client(monkeypatch, auth=False)  # sin login (T15): API key como antes
    assert c.post("/api/incidents/INC-EX-017-20261115T1430/close", json={"closed_by": "TEC-01"}).status_code == 200
    check, close = calls  # primero confirma que el incidente es del parque, luego cierra
    assert check["url"] == "https://reg.example/incidents/INC-EX-017-20261115T1430?tenant=mineria"
    assert close["url"] == "https://inc.example/incidents/INC-EX-017-20261115T1430/close"
    assert close["key"] == "secret-key" and close["body"]["closed_by"] == "TEC-01"


# ---------- T41: abastecimiento (mock con la historia de la demo y reenvio a /supply/*) ----------

def test_supply_json_in_sync_with_contracts():
    yaml = pytest.importorskip("yaml")
    path = os.path.join(ROOT, "contracts", "supply.yaml")
    if not os.path.isfile(path):
        pytest.skip("contracts/supply.yaml llega con T37 (PR #31)")
    with open(path, encoding="utf-8") as f:
        expected = yaml.safe_load(f)
    with open(os.path.join(ROOT, "gcp", "console", "app", "supply.json"), encoding="utf-8") as f:
        assert json.load(f) == expected, "regenerar gcp/console/app/supply.json desde contracts/supply.yaml"


def _stock(c, role, part, zone, tenant=""):
    q = f"?part={part}" + (f"&tenant={tenant}" if tenant else "")
    return next(r for r in c.get(f"/api/supply/stock{q}", headers=H(role)).json()["stock"] if r["zone_id"] == zone)


def test_supply_demo_story_transfer_from_agro(mock_client):
    c = mock_client
    assert _stock(c, "maintenance_head", "PRT-04", "NORTE")["status"] == "stockout"
    assert _stock(c, "maintenance_head", "PRT-04", "FUNDO-A")["status"] == "ok"
    sug = c.get("/api/supply/suggestions", headers=H("maintenance_head")).json()["suggestions"]
    trf = next(s for s in sug if s["kind"] == "transfer")
    assert (trf["from_zone"], trf["eta_h"], trf["cost_usd"], trf["downtime_avoided_h"], trf["downtime_avoided_usd"]) == (
        "FUNDO-A", 16, 180, 152, 68400)
    assert [o["source_kind"] for o in trf["options"]] == ["transfer", "purchase"]
    # El admin de agro ve su stock y la transferencia saliente, pero no la aprueba (solo el jefe)
    agro = c.get("/api/supply/stock", headers=H("admin_agro")).json()["stock"]
    assert agro and {r["tenant_id"] for r in agro} == {"agro"}
    assert [t["id"] for t in c.get("/api/supply/transfers", headers=H("admin_agro")).json()["transfers"]] == [trf["id"]]
    assert c.get("/api/supply/stock?tenant=mineria", headers=H("admin_agro")).status_code == 403
    assert c.post(f"/api/supply/transfers/{trf['id']}/approve", headers=H("admin_agro")).status_code == 403
    r = c.post(f"/api/supply/transfers/{trf['id']}/approve", headers=H("maintenance_head"))
    assert r.status_code == 200 and r.json()["status"] == "in_transit"
    assert _stock(c, "maintenance_head", "PRT-04", "FUNDO-A")["on_hand"] == 2
    assert _stock(c, "maintenance_head", "PRT-04", "NORTE")["in_transit"] == 1
    # El admin de mineria confirma la recepcion: queda reservada para EX-017
    assert c.post(f"/api/supply/transfers/{trf['id']}/receive", headers=H("admin_agro")).status_code == 403
    r = c.post(f"/api/supply/transfers/{trf['id']}/receive", headers=H("admin"))
    assert r.status_code == 200 and r.json()["status"] == "received"
    assert r.json()["timeline"][-1]["type"] == "part.reserved"
    norte = _stock(c, "admin", "PRT-04", "NORTE")
    assert (norte["on_hand"], norte["reserved"], norte["in_transit"]) == (1, 1, 0)
    assert c.post(f"/api/supply/transfers/{trf['id']}/receive", headers=H("admin")).status_code == 409
    k = c.get("/api/supply/kpis", headers=H("maintenance_head")).json()
    assert k["downtime_avoided_usd"] == 68400 and set(k["inventory_value_usd"]) == {"mineria", "agro", "industria"}


def test_supply_roles_and_small_purchase_order(mock_client):
    c = mock_client
    assert c.get("/api/supply/stock", headers=H("operator")).status_code == 403
    assert c.get("/api/supply/stock").status_code == 401
    po = next(s for s in c.get("/api/supply/suggestions", headers=H("supervisor")).json()["suggestions"]
              if s["kind"] == "purchase_order")
    assert c.get(f"/api/supply/purchase-orders/{po['id']}", headers=H("admin_agro")).status_code == 404
    r = c.post(f"/api/supply/purchase-orders/{po['id']}/approve", headers=H("supervisor"))
    assert r.status_code == 200 and r.json()["status"] == "ordered"
    assert _stock(c, "supervisor", po["part_id"], po["zone_id"])["on_order"] == po["qty"]
    assert c.post(f"/api/supply/purchase-orders/{po['id']}/reject", headers=H("supervisor")).status_code == 409
    sups = c.get("/api/supply/suppliers", headers=H("admin")).json()["suppliers"]
    assert {s["supplier_id"] for s in sups} == {"SUP-HIDRA", "SUP-MOTOR", "SUP-FILTR"}
    assert c.get("/api/supply/purchase-orders/BAD", headers=H("admin")).status_code == 400


def test_supply_real_mode_forwards_token_and_tenant(monkeypatch):
    monkeypatch.delenv("MOCK", raising=False)
    monkeypatch.delenv("SUPPLY_MOCK", raising=False)
    monkeypatch.setenv("STATIC_DIR", "/nonexistent")
    monkeypatch.setenv("AWS_INCIDENTS_URL", "https://api.example")
    import app.aws_api as aws_api
    import app.main as main
    importlib.reload(aws_api)
    importlib.reload(main)
    seen = []

    def opener(req, timeout):
        seen.append((req.get_method(), req.full_url, req.headers.get("Authorization"), req.headers.get("X-api-key")))
        return Resp(json.dumps({"ok": True}).encode())

    main.app.state.aws = aws_api.AwsApi(opener=opener, key_loader=lambda: "k")
    c = TestClient(main.app)
    auth = {"authorization": "Bearer eyJ.tok.en"}
    assert c.get("/api/health").json()["supply_mode"] == "aws"
    assert c.get("/api/supply/stock?tenant=agro", headers=auth).status_code == 200
    assert seen[-1] == ("GET", "https://api.example/supply/stock?tenant=agro", "Bearer eyJ.tok.en", None)
    trf = "TRF-FUNDO-A-NORTE-PRT-04-20261115T1430"
    assert c.post(f"/api/supply/transfers/{trf}/approve", headers=auth).status_code == 200
    assert seen[-1][:2] == ("POST", f"https://api.example/supply/transfers/{trf}/approve")
    assert c.get("/api/supply/kpis").status_code == 401


def test_user_writes_wait_longer_than_reads_and_timeouts_are_504(monkeypatch):
    """backlog #49: POST /api/reports espera a Gemini; con 6 s el BFF cortaba y respondia 502."""
    monkeypatch.delenv("MOCK", raising=False)
    monkeypatch.setenv("STATIC_DIR", "/nonexistent")
    monkeypatch.setenv("AWS_INCIDENTS_URL", "https://api.example")
    import app.aws_api as aws_api
    import app.main as main
    importlib.reload(aws_api)
    importlib.reload(main)
    seen = []

    def opener(req, timeout):
        seen.append((req.get_method(), timeout))
        if req.get_method() == "POST":
            raise TimeoutError()
        return Resp(json.dumps({"reports": []}).encode())

    main.app.state.aws = aws_api.AwsApi(opener=opener, key_loader=lambda: "k")
    c = TestClient(main.app)
    auth = {"authorization": "Bearer eyJ.tok.en"}
    assert c.get("/api/reports", headers=auth).status_code == 200
    r = c.post("/api/reports", json={"machine_id": "EX-017"}, headers=auth)
    assert r.status_code == 504 and r.json() == {"error": "AWS_TIMEOUT"}
    assert seen == [("GET", aws_api.TIMEOUT_S), ("POST", aws_api.WRITE_TIMEOUT_S)]
    assert aws_api.WRITE_TIMEOUT_S == 25 and aws_api.TIMEOUT_S == 6
