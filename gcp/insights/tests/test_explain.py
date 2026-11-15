from fastapi.testclient import TestClient

from app.explain import fallback_response, validate_explain
from app.gemini import GeminiError
from app.main import app

client = TestClient(app)

REQ = {
    "incident_id": "INC-EX-001-20261114T1200",
    "machine_id": "EX-001",
    "model": "EX",
    "severity": "immediate",
    "failure_mode": "hydraulic_pump",
    "deviated_vars": [{"var": "hydraulic_pressure_bar", "value": 120.0, "z": 3.4}],
    "group_id": None,
    "context": {"zone_id": "NORTE"},
    "recent_incidents": [],
}

GOOD_GEMINI = {
    "hypotheses": ["Desgaste de la bomba hidraulica", "Perdida de presion por sellos"],
    "probable_component": "Bomba hidraulica",
    "suggested_parts": ["PRT-04"],
    "confidence": 0.8,
    "common_cause": None,
    "role_messages": {
        "technician": "Revisar bomba y sellos.",
        "supervisor": "EX-001 critico, coordinar.",
        "management": "Impacto en produccion.",
        "operator": "Detener la maquina.",
    },
}


class FakeGemini:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def explain(self, req):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def setup_function(_fn):
    app.state.explain_cache.clear()


def test_explain_uses_gemini_when_valid():
    app.state.gemini = FakeGemini(result=GOOD_GEMINI)
    r = client.post("/explain", json=REQ)
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "gemini"
    assert body["probable_component"] == "Bomba hidraulica"
    assert set(body["role_messages"]) == {"technician", "supervisor", "management", "operator"}


def test_explain_falls_back_when_gemini_fails():
    app.state.gemini = FakeGemini(error=GeminiError("timeout"))
    r = client.post("/explain", json=REQ)
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "template"
    assert body["suggested_parts"] == ["PRT-04", "PRT-03"]
    assert all(body["role_messages"][role] for role in ("technician", "supervisor", "management", "operator"))


def test_explain_falls_back_on_invalid_json():
    app.state.gemini = FakeGemini(result={"nope": 1})
    r = client.post("/explain", json=REQ)
    assert r.json()["source"] == "template"


def test_explain_caches_by_incident_id():
    fake = FakeGemini(result=GOOD_GEMINI)
    app.state.gemini = fake
    first = client.post("/explain", json=REQ)
    second = client.post("/explain", json=REQ)
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert fake.calls == 1  # no se vuelve a llamar a Gemini


def test_validate_rejects_missing_roles():
    bad = dict(GOOD_GEMINI)
    bad["role_messages"] = {"technician": "x"}
    assert validate_explain(bad) is None


def test_fallback_grouped_sets_common_cause():
    grouped = dict(REQ, group_id="GRP-abc123-20261114T12")
    out = fallback_response(grouped)
    assert out["common_cause"] is not None
