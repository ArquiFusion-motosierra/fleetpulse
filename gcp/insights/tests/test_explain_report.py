import json
import os

import pytest
from fastapi.testclient import TestClient

from app import report
from app.explain import common_cause_for, fallback_response
from app.gemini import GeminiError
from app.main import app

client = TestClient(app)
CONTRACTS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "contracts")

S1 = {
    "incident_id": "INC-EX-017-20261115T1430", "machine_id": "EX-017", "model": "EX", "severity": "immediate",
    "failure_mode": "hydraulic_pump",
    "deviated_vars": [{"var": "hydraulic_pressure_bar", "value": 240.0, "z": -3.4},
                      {"var": "hydraulic_oil_temp_c", "value": 84.0, "z": 2.6}],
    "group_id": None, "context": {"fuel_lots": [], "weather": []}, "recent_incidents": [],
}
S7 = {
    "incident_id": "INC-VQ-022-20261115T1500", "machine_id": "VQ-022", "model": "VQ", "severity": "schedule",
    "failure_mode": "head_gasket",
    "deviated_vars": [{"var": "coolant_temp_c", "value": 104.2, "z": 3.1},
                      {"var": "engine_rpm", "value": 1868.0, "z": 2.4}],
    "group_id": None, "context": {}, "recent_incidents": [],
}
S2 = {
    "incident_id": "INC-VQ-011-20261115T1600", "machine_id": "VQ-011", "model": "VQ", "severity": "schedule",
    "failure_mode": "fuel_contamination",
    "deviated_vars": [{"var": "fuel_rate_lph", "value": 66.0, "z": 2.8}],
    "group_id": "GRP-6mc5h1-20261115T16", "context": {"fuel_lots": ["L-2311"], "weather": []},
    "recent_incidents": [],
}

GEMINI = {
    "hypotheses": ["Junta de culata"], "probable_component": "Junta de culata", "suggested_parts": ["PRT-13"],
    "confidence": 0.8, "common_cause": None,
    "role_messages": {"technician": "t", "supervisor": "s", "management": "m", "operator": "o"},
    "report": {"summary": "Resumen de Gemini", "probable_cause": "Causa de Gemini", "recommended_action": "Accion",
               "findings": [{"parameter": "coolant_temp_c", "value": 1, "unit": "C", "oem_limit": "x",
                             "status": "normal"}], "failure_mode": "brake_wear"},
}


class FakeGemini:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def explain(self, req):
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def report_validator():
    jsonschema = pytest.importorskip("jsonschema")
    with open(os.path.join(CONTRACTS, "incident.schema.json")) as f:
        schema = json.load(f)
    return jsonschema.Draft202012Validator({"$schema": schema["$schema"], "$defs": schema["$defs"],
                                            "$ref": "#/$defs/report"})


def setup_function(_fn):
    app.state.explain_cache.clear()


def test_specs_json_in_sync_with_contracts():
    pytest.importorskip("yaml")
    import importlib.util

    spec = importlib.util.spec_from_file_location("insights_sync_specs",
                                                  os.path.join(os.path.dirname(__file__), "..", "sync_specs.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert report.specs() == json.loads(json.dumps(mod.specs())), "correr python gcp/insights/sync_specs.py"


def test_s1_report_valid_with_oem_limits(report_validator):
    rep = report.build_report(S1)
    report_validator.validate(rep)
    by_var = {f["parameter"]: f for f in rep["findings"]}
    assert by_var["hydraulic_pressure_bar"]["status"] == "critical"  # <= 250 bar critico bajo
    assert by_var["hydraulic_pressure_bar"]["oem_limit"].startswith("normal 280-345 bar")
    assert by_var["hydraulic_oil_temp_c"]["status"] == "warning"  # >= 82 C
    assert rep["resonance"] is None
    assert rep["parts"] == [{"part_id": "PRT-04", "qty": 1}, {"part_id": "PRT-03", "qty": 2}]
    assert rep["failure_mode"] == "hydraulic_pump"
    assert "Bomba hidraulica gastada" in rep["probable_cause"]


def test_s7_report_identifies_resonance(report_validator):
    rep = report.build_report(S7)
    report_validator.validate(rep)
    assert rep["resonance"] == {"harmonic": "2x", "frequency_hz": 62.3, "natural_frequency_hz": 62,
                                "component": "Chasis y tolva (segundo modo)"}
    spectrum = [f for f in rep["findings"] if f["parameter"] == "vibration_spectrum"]
    assert spectrum and spectrum[0]["status"] == "critical"
    rpm = next(f for f in rep["findings"] if f["parameter"] == "engine_rpm")
    assert rpm["status"] == "warning" and "+13 %" in rpm["note"]
    assert rep["parts"] == [{"part_id": "PRT-13", "qty": 1}]
    assert "resonancia" in rep["summary"].lower()


def test_resonance_off_band_rpm_is_none():
    assert report.resonance("VQ", 1650) is None  # 2x = 55 Hz, lejos de 62 Hz
    assert report.resonance("ZZ", 1868) is None


def test_template_fills_report_on_gemini_failure(report_validator):
    app.state.gemini = FakeGemini(error=GeminiError("timeout"))
    body = client.post("/explain", json=S7).json()
    assert body["source"] == "template"
    report_validator.validate(body["report"])
    assert body["report"]["resonance"]["harmonic"] == "2x"


def test_gemini_only_rewrites_report_texts(report_validator):
    app.state.gemini = FakeGemini(result=GEMINI)
    body = client.post("/explain", json=S7).json()
    assert body["source"] == "gemini"
    rep = body["report"]
    report_validator.validate(rep)
    assert rep["summary"] == "Resumen de Gemini" and rep["recommended_action"] == "Accion"
    assert rep["failure_mode"] == "head_gasket"  # no el brake_wear de Gemini
    assert {f["parameter"] for f in rep["findings"]} == {"coolant_temp_c", "engine_rpm", "vibration_spectrum"}


def test_no_report_without_oem_spec():
    out = fallback_response(dict(S1, model="ZZ"))
    assert "report" not in out


def test_grouped_fallback_names_the_fuel_lot():
    out = fallback_response(S2)
    assert "L-2311" in out["common_cause"]
    assert len(out["common_cause"]) <= 300


@pytest.mark.parametrize("gemini_text", [None, "", "Condiciones de zona"])
def test_grouped_gemini_common_cause_must_name_the_lot(gemini_text):
    app.state.gemini = FakeGemini(result=dict(GEMINI, common_cause=gemini_text))
    body = client.post("/explain", json=S2).json()
    assert body["source"] == "gemini" and "L-2311" in body["common_cause"]


def test_grouped_gemini_common_cause_kept_when_it_names_the_lot():
    assert common_cause_for(S2, "El lote L-2311 esta contaminado") == "El lote L-2311 esta contaminado"


def test_common_cause_is_null_when_not_grouped():
    app.state.gemini = FakeGemini(result=dict(GEMINI, common_cause="algo"))
    assert client.post("/explain", json=S1).json()["common_cause"] is None
