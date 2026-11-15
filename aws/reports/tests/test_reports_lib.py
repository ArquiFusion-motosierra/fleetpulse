"""Pruebas de lib/snapshot, lib/recommend y lib/link_incident (funciones puras y dobles) y de los ejemplos de
contracts/examples/report."""
import json
from pathlib import Path

import pytest
from reports_support import (
    AUDIENCE,
    BASELINE,
    T33_EXAMPLE,
    FakeHttp,
    log_lines,
    reading,
    seed_machine,
    seed_readings,
)

from fplib import ids, schema
from lib import link_incident, recommend, snapshot


# Alias locales de los fixtures de conftest.py (ver su docstring): `_env` local tambien evita el `_env` global de
# aws/incidents en la sesion unica de CI.
@pytest.fixture(autouse=True)
def _env(reports_env):
    return reports_env


@pytest.fixture
def ddb(reports_ddb):
    return reports_ddb


@pytest.fixture
def invoker(reports_invoker):
    return reports_invoker


@pytest.fixture
def svc(reports_svc):
    return reports_svc


REPO = Path(__file__).resolve().parents[3]
T26_EX = {  # forma de contracts/oem_specs.yaml (T26, rama contracts/t26-oem-specs)
    "name": "Excavadora hidraulica",
    "limits": {"coolant_temp_c": {"normal": [65, 96], "warning": 102, "critical": 108},
               "hydraulic_pressure_bar": {"normal": [280, 345], "low_warning": 270, "low_critical": 250}},
    "service_schedule": [{"every_h": 500, "task": "Filtros", "parts": [{"part_id": "PRT-01", "qty": 1}]}],
    "typical_failures": [{"failure_mode": "head_gasket", "name": "Junta de culata danada", "symptoms": []}],
}
REQUEST = {"report_id": "RPT-EX-017-20261115T144012", "machine_id": "EX-017", "model": "EX",
           "abnormality_type": "noise", "description": "ruido", "severity_perceived": "low",
           "telemetry_snapshot": {}, "oem_specs": {}}


# --- snapshot -------------------------------------------------------------------------------------------------------

def test_means_ignore_off_readings_and_fall_back_to_all():
    rows = [reading("2026-11-15T14:00:00Z", coolant_temp_c=100.0),
            reading("2026-11-15T14:00:30Z", coolant_temp_c=90.0),
            reading("2026-11-15T14:01:00Z", engine_status="off", coolant_temp_c=20.0),
            reading("2026-11-15T14:01:30Z", engine_status="idle", coolant_temp_c=80.0, vibration_mm_s=None)]
    m = snapshot.means(rows)
    assert m["coolant_temp_c"] == 90.0 and m["vibration_mm_s"] == 6.9 and set(m) == set(snapshot.VARS)
    off = [reading("2026-11-15T14:01:00Z", engine_status="off", coolant_temp_c=20.0)]
    assert snapshot.means(off)["coolant_temp_c"] == 20.0
    assert snapshot.means([]) == {}


def test_fault_codes_are_distinct_newest_first():
    rows = [reading("2026-11-15T14:00:00Z", fault_codes=[{"spn": 520193, "fmi": 1, "severity": "medium"}]),
            reading("2026-11-15T14:00:30Z", fault_codes=[{"spn": 110, "fmi": 0, "severity": "high"},
                                                          {"spn": 520193, "fmi": 1, "severity": "medium"}])]
    assert snapshot.fault_codes(rows) == [{"spn": 110, "fmi": 0, "severity": "high"},
                                          {"spn": 520193, "fmi": 1, "severity": "medium"}]


def test_build_reads_the_last_30_readings_and_meta(ddb):
    seed_machine(ddb.state, hours_total=2800.0)
    seed_readings(ddb.state, [reading(f"2026-11-15T13:{m:02d}:00Z", coolant_temp_c=200.0) for m in range(10)])
    seed_readings(ddb.state, [reading(f"2026-11-15T14:{m:02d}:00Z", coolant_temp_c=95.0) for m in range(30)])
    snap = snapshot.build(ddb.state, "EX-017")
    assert snap["n_readings"] == 30 and snap["means"]["coolant_temp_c"] == 95.0
    assert snap["last_ts"] == "2026-11-15T14:29:00Z" and snap["status"] == "schedule"
    # ids.yaml: hours_total 2762, hours_since_service 286 -> 286 + (2800 - 2762)
    assert snap["hours_total"] == 2800.0 and snap["hours_since_service"] == 324.0
    json.dumps(snap)  # sin Decimal


def test_oem_excerpt_maps_the_t26_sheet(monkeypatch):
    monkeypatch.setattr(ids, "oem_specs", lambda model: json.loads(json.dumps(T26_EX)) if model == "EX" else {})
    ex = snapshot.oem_excerpt("EX")
    assert ex == {"ranges": {"coolant_temp_c": [65, 96], "hydraulic_pressure_bar": [280, 345]},
                  "service_intervals": [{"every_h": 500, "task": "Filtros", "parts": ["PRT-01"]}],
                  "typical_failures": ["head_gasket"]}


def test_oem_excerpt_without_the_sheet_uses_ids_yaml(monkeypatch):
    monkeypatch.setattr(ids, "oem_specs", lambda model: {})
    ex = snapshot.oem_excerpt("VQ")
    assert ex["service_intervals"] == [{"every_h": 600, "task": "Servicio preventivo", "parts": []}]
    assert "final_drive" in ex["typical_failures"] and "ranges" not in ex


# --- recommend ------------------------------------------------------------------------------------------------------

def test_seed_catalog_matches_the_contract_seed():
    seed = json.loads((REPO / "contracts/report.schema.json").read_text())["$defs"]["abnormality_type_seed"]["enum"]
    entries = recommend.seed_catalog()
    assert [e["code"] for e in entries] == seed
    assert all(e["label_es"] and len(e["default_checks"]) >= 3 for e in entries)


@pytest.mark.parametrize("kind", list(recommend.TEMPLATES))
def test_every_template_is_usable_for_every_model(kind):
    for model in ("EX", "CF", "VQ", "MN", "TO"):
        rec = recommend.fallback(kind, model)
        assert rec["source"] == "template" and rec["checks"] and rec["probable_causes"]
        assert rec["urgency"] in recommend.URGENCIES
        assert set(rec["suggested_parts"]) <= recommend.model_parts(model)
        assert [c["order"] for c in rec["checks"]] == list(range(1, len(rec["checks"]) + 1))


def test_fallback_raises_urgency_when_the_operator_perceives_high_severity():
    assert recommend.fallback("noise", "EX")["urgency"] == "observe"
    assert recommend.fallback("noise", "EX", "high")["urgency"] == "schedule"
    assert recommend.fallback("smoke", "EX", "low")["urgency"] == "immediate"


def test_fallback_for_an_admin_added_type_uses_its_default_checks():
    rec = recommend.fallback("brake_smell", "CF", default_checks=["Oler los frenos", "Medir espesor de pastillas"])
    assert [c["step"] for c in rec["checks"]] == ["Oler los frenos", "Medir espesor de pastillas"]
    assert recommend.fallback("brake_smell", "CF")["checks"] == recommend.fallback("other", "CF")["checks"]


def test_normalize_filters_parts_and_trims():
    body = {**T33_EXAMPLE, "suggested_parts": ["PRT-01", "PRT-99", "PRT-12", "PRT-01", 7],
            "checks": [{"order": 2, "step": "b" * 400}, {"step": "a"}, {"order": 9, "step": "  "}, "x"],
            "probable_causes": [{"cause": "c", "confidence": 7}, {"cause": ""}], "source": "template"}
    out = recommend.normalize(body, "EX")
    assert out["suggested_parts"] == ["PRT-01"]  # PRT-12 no esta en el BOM de EX
    assert [c["order"] for c in out["checks"]] == [2, 2] and len(out["checks"][0]["step"]) == 300
    assert out["probable_causes"] == [{"cause": "c", "component": "", "confidence": 1.0}]
    assert out["source"] == "template"


@pytest.mark.parametrize("body,reason", [
    ({**T33_EXAMPLE, "checks": []}, "probable_causes y checks deben ser listas no vacias"),
    ({**T33_EXAMPLE, "urgency": "now"}, "urgency invalida"),
    ({**T33_EXAMPLE, "checks": [{"order": 1}]}, "sin causas o chequeos con texto"),
    ([], "respuesta no es un objeto"),
])
def test_bad_responses_fall_back(invoker, caplog, body, reason):
    http = FakeHttp((200, json.dumps(body).encode()))
    with caplog.at_level("INFO", logger="fplib"):
        rec = recommend.recommend(REQUEST, secrets=invoker.secrets, http=http)
    assert rec["source"] == "template" and log_lines(caplog, "recommend_fallback")[0]["reason"] == reason


@pytest.mark.parametrize("result,reason", [((500, b"boom"), "http 500"), ((200, b"{no"), "json invalido"),
                                           (ConnectionError("x"), "ConnectionError")])
def test_transport_errors_fall_back(invoker, caplog, result, reason):
    with caplog.at_level("INFO", logger="fplib"):
        rec = recommend.recommend(REQUEST, secrets=invoker.secrets, http=FakeHttp(result))
    assert rec == recommend.fallback("noise", "EX", "low")
    assert log_lines(caplog, "recommend_fallback")[0]["reason"] == reason


def test_missing_secret_falls_back_without_calling_insights(caplog):
    from reports_support import FakeSecrets
    http = FakeHttp((200, json.dumps(T33_EXAMPLE).encode()))
    with caplog.at_level("INFO", logger="fplib"):
        rec = recommend.recommend(REQUEST, secrets=FakeSecrets({}), http=http)
    assert rec["source"] == "template" and http.calls == []
    assert log_lines(caplog, "recommend_fallback")[0]["reason"] == "KeyError"


def test_recommend_posts_to_the_audience_without_trailing_slash(invoker):
    http = FakeHttp((200, json.dumps(T33_EXAMPLE).encode()))
    out = recommend.recommend(REQUEST, secrets=invoker.secrets, http=http)
    assert http.calls[0]["url"] == f"{AUDIENCE}/recommend-checks" and invoker.token_calls[0][1] == AUDIENCE
    assert out["urgency"] == "immediate" and out["safety_notes"] == ["No abrir el radiador en caliente"]


# --- link_incident --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("component,kind,mode", [
    ("thermostat", "high_engine_temp", "engine_overheat"),
    ("Termostato pegado", "other", "engine_overheat"),
    ("Rodamiento del mando final", "noise", "final_drive"),
    ("Bomba hidraulica", "leak", "hydraulic_pump"),
    ("oil_cooler", "high_hydraulic_temp", "hydraulic_overheat"),
    ("Inyector", "smoke", "fuel_contamination"),
    ("hydraulic_pump", "other", "hydraulic_pump"),
    ("algo raro", "abnormal_vibration", "final_drive"),
    ("algo raro", "brake_smell", "sensor_fault"),
    (None, "warning_light", "sensor_fault"),
])
def test_failure_mode_for_component(component, kind, mode):
    assert link_incident.failure_mode_for(component, kind) == mode


def test_head_gasket_only_when_the_catalog_knows_it(monkeypatch):
    real = ids.load()
    without = {**real, "failure_modes": [m for m in real["failure_modes"] if m != "head_gasket"]}
    monkeypatch.setattr(ids, "load", lambda: without)
    assert link_incident.failure_mode_for("head_gasket", "high_engine_temp") == "engine_overheat"
    assert link_incident.failure_mode_for("Junta de culata", "other") == "engine_overheat"
    with_t26 = {**real, "failure_modes": ["head_gasket", *without["failure_modes"]],
                "specialty_by_failure_mode": {**real["specialty_by_failure_mode"], "head_gasket": "engine"}}
    monkeypatch.setattr(ids, "load", lambda: with_t26)
    assert link_incident.failure_mode_for("head_gasket", "high_engine_temp") == "head_gasket"


def test_deviations_and_symptom_entry():
    z, dev = link_incident.deviations({"coolant_temp_c": 104.0, "vibration_mm_s": 6.9, "engine_rpm": 1810.0},
                                      BASELINE)
    assert dev == [{"var": "coolant_temp_c", "value": 104.0, "z": 5.333}]
    assert z["vibration_mm_s"] == {"self": 0.4, "peer": None} and set(z) == {"coolant_temp_c", "vibration_mm_s",
                                                                             "engine_rpm"}
    assert link_incident.deviations({"coolant_temp_c": 104.0}, None) == ({}, [])
    assert link_incident.symptom_entry("low_hydraulic_pressure", {"hydraulic_pressure_bar": 250.0}) == {
        "var": "hydraulic_pressure_bar", "value": 250.0, "z": -1.0}
    assert link_incident.symptom_entry("brake_smell", {}) == {"var": "vibration_mm_s", "value": 1.0, "z": 1.0}


# --- contratos ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", sorted((REPO / "contracts/examples/report").glob("*.json")), ids=lambda p: p.name)
def test_report_examples_validate(path):
    obj = json.loads(path.read_text())
    schema.validate("report", obj)
    if "event_type" in obj:
        schema.validate("envelope", obj)
    else:
        schema.validate("report", obj, ref="create" if "machine_id" in obj else "close")
