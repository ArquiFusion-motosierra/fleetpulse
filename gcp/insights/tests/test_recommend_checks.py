import os

import pytest
from fastapi.testclient import TestClient

from app import recommend
from app.gemini import GeminiClient, GeminiError
from app.main import app

client = TestClient(app)

CONTRACTS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "contracts")

# Cuerpo como lo arma aws/reports/lib/recommend.py.
REQ = {
    "report_id": "RPT-EX-001-20261115T101500",
    "machine_id": "EX-001",
    "model": "EX",
    "abnormality_type": "high_engine_temp",
    "description": "La aguja de temperatura sube en subida con carga y sale vapor del capo",
    "severity_perceived": "medium",
    "telemetry_snapshot": {
        "means": {"coolant_temp_c": 88.0, "hydraulic_oil_temp_c": 60.0, "hydraulic_pressure_bar": 300.0},
        "fault_codes": [],
        "hours_total": 5210.0,
        "hours_since_service": 120.0,
    },
    "oem_specs": {
        "ranges": {"coolant_temp_c": [65, 96], "hydraulic_oil_temp_c": [35, 75], "hydraulic_pressure_bar": [280, 345]},
        "service_intervals": [{"every_h": 250, "task": "Inspeccion general y filtro de aire", "parts": ["PRT-11"]}],
        "typical_failures": ["hydraulic_pump", "head_gasket"],
    },
}

GOOD = {
    "probable_causes": [{"cause": "Junta de culata con fuga", "component": "head_gasket", "confidence": 0.7},
                        {"cause": "Termostato pegado", "component": "thermostat", "confidence": 0.2}],
    "checks": [
        {"order": 2, "step": "Prueba de presion del sistema", "tool": "bomba manual", "expected": "1,2 bar 10 min",
         "if_fails": "junta de culata"},
        {"order": 1, "step": "Nivel y color del refrigerante", "tool": "visual", "expected": "sin aceite",
         "if_fails": "no operar"},
    ],
    "urgency": "schedule",
    "suggested_parts": ["PRT-13", "PRT-99", "PRT-12", "PRT-13"],
    "safety_notes": ["No abrir el radiador en caliente"],
}


class FakeGemini:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls, self.last = result, error, 0, None

    def recommend(self, req):
        self.calls += 1
        self.last = req
        if self.error:
            raise self.error
        return self.result


def setup_function(_fn):
    app.state.recommend_cache.clear()


def test_uses_gemini_when_valid_and_filters_parts_to_bom():
    app.state.gemini = FakeGemini(result=GOOD)
    r = client.post("/recommend-checks", json=REQ)
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "gemini" and body["cached"] is False
    assert [c["order"] for c in body["checks"]] == [1, 2]
    assert body["suggested_parts"] == ["PRT-13"]  # PRT-99 no existe, PRT-12 no es del BOM del EX
    assert body["urgency"] == "schedule"
    assert body["safety_notes"] == ["No abrir el radiador en caliente"]


def test_falls_back_when_gemini_fails():
    app.state.gemini = FakeGemini(error=GeminiError("TimeoutError"))
    body = client.post("/recommend-checks", json=REQ).json()
    assert body["source"] == "template"
    assert body["urgency"] == "immediate"
    assert body["probable_causes"][0]["component"] == "head_gasket"
    assert body["suggested_parts"] == ["PRT-13", "PRT-11"]
    assert "65-96" in body["checks"][0]["expected"]  # cita el rango del fabricante
    assert [c["order"] for c in body["checks"]] == list(range(1, len(body["checks"]) + 1))


@pytest.mark.parametrize("bad", [
    {"nope": 1},
    "texto",
    dict(GOOD, urgency="ya"),
    dict(GOOD, checks=[]),
    dict(GOOD, probable_causes=[{"cause": "  "}]),
])
def test_falls_back_on_invalid_shape(bad):
    app.state.gemini = FakeGemini(result=bad)
    assert client.post("/recommend-checks", json=REQ).json()["source"] == "template"


def test_caches_by_report_id():
    fake = FakeGemini(result=GOOD)
    app.state.gemini = fake
    first = client.post("/recommend-checks", json=REQ).json()
    second = client.post("/recommend-checks", json=REQ).json()
    assert (first["cached"], second["cached"]) == (False, True)
    assert fake.calls == 1


def test_urgency_floor_raises_gemini_urgency():
    app.state.gemini = FakeGemini(result=dict(GOOD, urgency="observe"))
    req = dict(REQ, open_incident={"incident_id": "INC-EX-001-20261115T1000", "failure_mode": "head_gasket",
                                   "severity": "immediate"})
    assert client.post("/recommend-checks", json=req).json()["urgency"] == "immediate"


def test_urgency_floor_rules():
    calm = dict(REQ, severity_perceived="low")
    assert recommend.urgency_floor(calm) == "observe"
    assert recommend.urgency_floor(dict(calm, severity_perceived="high")) == "schedule"
    hot = dict(calm, telemetry_snapshot=dict(REQ["telemetry_snapshot"], means={"coolant_temp_c": 104.0}))
    assert recommend.urgency_floor(hot) == "schedule"
    faults = dict(calm, telemetry_snapshot=dict(REQ["telemetry_snapshot"], fault_codes=[{"spn": 110, "fmi": 0}]))
    assert recommend.urgency_floor(faults) == "schedule"


def test_fallback_adds_check_for_other_out_of_range_vars():
    req = dict(REQ, abnormality_type="noise",
               telemetry_snapshot=dict(REQ["telemetry_snapshot"], means={"hydraulic_oil_temp_c": 81.0}))
    out = recommend.fallback_recommendation(req)
    assert out["urgency"] == "schedule"  # noise es observe, pero hay una media fuera de rango
    assert any("hydraulic_oil_temp_c" in c["step"] for c in out["checks"])


def test_unknown_type_uses_other_template():
    out = recommend.fallback_recommendation(dict(REQ, abnormality_type="tipo_del_admin", model="TO"))
    assert out["probable_causes"][0]["component"] == "general_inspection"
    assert out["suggested_parts"] == []


def test_minimal_request_is_accepted():
    app.state.gemini = FakeGemini(error=GeminiError("x"))
    r = client.post("/recommend-checks", json={"report_id": "RPT-TO-001-20261115T101500", "machine_id": "TO-001",
                                               "model": "TO", "abnormality_type": "leak"})
    assert r.status_code == 200 and r.json()["checks"]


def test_missing_report_id_is_422():
    r = client.post("/recommend-checks", json={k: v for k, v in REQ.items() if k != "report_id"})
    assert r.status_code == 422


def test_prompt_has_operator_text_oem_ranges_and_bom():
    prompt = recommend.build_prompt(REQ)
    assert REQ["description"] in prompt
    assert "[65, 96]" in prompt
    assert "PRT-13" in prompt and "PRT-12" not in prompt


def test_catalog_covers_abnormality_type_seed():
    import json

    with open(os.path.join(CONTRACTS, "report.schema.json")) as f:
        seed = json.load(f)["$defs"]["abnormality_type_seed"]["enum"]
    assert set(seed) == set(recommend.CATALOG)


def test_bom_matches_ids_yaml():
    yaml = pytest.importorskip("yaml")
    with open(os.path.join(CONTRACTS, "ids.yaml")) as f:
        bom = yaml.safe_load(f)["bom"]
    expected: dict = {}
    for row in bom:
        expected.setdefault(row["model_id"], {}).setdefault(row["failure_mode"], []).append(row["part_id"])
    assert {m: {fm: sorted(p) for fm, p in modes.items()} for m, modes in expected.items()} == \
        {m: {fm: sorted(p) for fm, p in modes.items()} for m, modes in recommend.BOM.items()}
    modes = set(yaml.safe_load(open(os.path.join(CONTRACTS, "ids.yaml")))["failure_modes"])
    assert all(set(t["modes"]) <= modes for t in recommend.CATALOG.values())


def test_gemini_client_key_failure_is_gemini_error():
    def broken_loader():
        raise OSError("metadata down")

    with pytest.raises(GeminiError):
        GeminiClient(key_loader=broken_loader).recommend(REQ)


def test_gemini_client_sends_recommend_prompt():
    import io
    import json

    seen = {}

    def opener(request, timeout):
        seen["body"] = json.loads(request.data)
        seen["timeout"] = timeout
        text = json.dumps(GOOD)
        return io.BytesIO(json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]}).encode())

    out = GeminiClient(opener=opener, key_loader=lambda: "k").recommend(REQ)
    assert out == GOOD
    assert REQ["description"] in seen["body"]["contents"][0]["parts"][0]["text"]
    assert seen["body"]["generationConfig"]["temperature"] == 0.2
    assert seen["timeout"] < 8
