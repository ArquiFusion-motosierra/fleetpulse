import json
import os
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import analytics
from app.main import app

client = TestClient(app)
ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")

KPI = [
    {"machine_id": "EX-001", "model": "EX", "operating_h": 14.0, "idle_h": 2.0, "fuel_l": 560.0},
    {"machine_id": "EX-002", "model": "EX", "operating_h": 12.0, "idle_h": 2.0, "fuel_l": 420.0},
    {"machine_id": "EX-003", "model": "EX", "operating_h": 3.0, "idle_h": 3.0, "fuel_l": 260.0},
    {"machine_id": "VQ-011", "model": "VQ", "operating_h": 40.0, "idle_h": 0.0, "fuel_l": 1000.0},  # TIME_WARP
    {"machine_id": "MN-003", "model": "MN", "operating_h": 0.0, "idle_h": 0.0, "fuel_l": 0.0},
]


class FakeBq:
    def __init__(self, results=None, error=None):
        self.results = results or {}
        self.error = error
        self.calls, self.submitted = [], []

    def rows(self, sql, params=None):
        self.calls.append((sql, params))
        if self.error:
            raise self.error
        for key, value in self.results.items():
            if key in sql:
                if isinstance(value, Exception):
                    raise value
                return value
        return []

    def submit(self, sql, max_bytes=None):
        if "m_parts_demand" in sql and self.error:
            raise self.error
        self.submitted.append((sql, max_bytes))
        return f"job_{len(self.submitted)}"

    def job(self, job_id):
        return {"job_id": job_id, "state": "DONE" if job_id != "job_2" else "RUNNING",
                "error": "boom" if job_id == "job_3" else None}


def test_kpis_daily_matches_the_contract():
    jsonschema = pytest.importorskip("jsonschema")
    app.state.bq = FakeBq({"telemetry": KPI, "m_peer_clusters": [{"machine_id": "EX-001", "cluster_id": 2},
                                                                 {"machine_id": "EX-002", "cluster_id": 2}]})
    r = client.get("/kpis/daily?day=2026-11-14")
    assert r.status_code == 200
    body = r.json()
    with open(os.path.join(ROOT, "contracts", "kpi.daily.schema.json")) as f:
        schema = json.load(f)
    event = {"event_id": "3f2b8c1e-5d4a-4b7e-9a1c-2e6f8d0b4c11", "event_type": "kpi.daily", "schema_version": 1,
             "source": "aws.daily-planning", "aggregate_type": "fleet", "aggregate_id": "KPI-2026-11-14",
             "occurred_at": "2026-11-15T10:00:00Z", "payload": body}
    jsonschema.validate(event, schema, format_checker=jsonschema.FormatChecker())
    by = {m["machine_id"]: m for m in body["machines"]}
    assert by["EX-001"]["fuel_lph"] == 35.0 and by["EX-001"]["cluster_id"] == 2
    assert by["EX-001"]["fuel_vs_peers_pct"] == pytest.approx(7.7)  # mediana del cluster 2: (35 + 30) / 2 = 32,5
    assert by["EX-001"]["quadrant"] == "workhorse" and by["EX-002"]["quadrant"] == "star"
    assert by["EX-003"]["quadrant"] == "underused"  # sin cluster: pares del mismo modelo
    assert by["VQ-011"]["operating_h"] == 24 and by["VQ-011"]["utilization"] == 1
    assert by["MN-003"]["cost_per_h"] == 0 and by["MN-003"]["idle_ratio"] == 0
    sql, params = app.state.bq.calls[0]
    assert params == {"day": date(2026, 11, 14)} and "TIMESTAMP(@day, 'America/Lima')" in sql


def test_kpis_without_cluster_model_still_answers():
    app.state.bq = FakeBq({"telemetry": KPI, "m_peer_clusters": RuntimeError("Not found: Model")})
    body = client.get("/kpis/daily?day=2026-11-14").json()
    assert all("cluster_id" not in m for m in body["machines"])


def test_kpis_errors():
    app.state.bq = FakeBq({"telemetry": []})
    assert client.get("/kpis/daily?day=2026-11-14").status_code == 404
    assert client.get("/kpis/daily?day=ayer").status_code == 400
    app.state.bq = FakeBq(error=RuntimeError("no creds"))
    assert client.get("/kpis/daily?day=2026-11-14").status_code == 503


def test_kpis_default_day_is_yesterday_in_lima():
    app.state.bq = FakeBq({"telemetry": KPI})
    r = client.get("/kpis/daily")
    assert r.status_code == 200 and app.state.bq.calls[0][1]["day"] < date.today()


def test_anomalies_fuel():
    rows = [{"machine_id": "CF-042", "day": date(2026, 11, 13), "fuel_l": 412.33, "lower_bound": 250.0,
             "upper_bound": 330.0, "anomaly_probability": 0.991}]
    app.state.bq = FakeBq({"m_fuel_trend": rows})
    body = client.get("/anomalies/fuel?day=2026-11-14&days=3").json()
    assert body["anomalies"] == [{"machine_id": "CF-042", "day": "2026-11-13", "fuel_l": 412.3,
                                  "expected": [250.0, 330.0], "anomaly_probability": 0.991}]
    assert app.state.bq.calls[0][1] == {"day": date(2026, 11, 14), "days": 3}
    assert client.get("/anomalies/fuel?days=99").status_code == 422


def test_forecast_parts_rounds_up_and_503_without_model():
    app.state.bq = FakeBq({"m_parts_demand": [{"part_id": "PRT-02", "zone_id": "NORTE", "demand": 2.2,
                                               "demand_upper": 4.0}]})
    body = client.get("/forecast/parts").json()
    assert body == {"horizon_days": 14, "parts": [{"part_id": "PRT-02", "zone_id": "NORTE", "demand_14d": 3,
                                                   "demand_14d_upper": 4}]}
    app.state.bq = FakeBq({"m_parts_demand": RuntimeError("Not found: Model fleetpulse.m_parts_demand")})
    assert client.get("/forecast/parts").status_code == 503


def test_jobs_refresh_and_status():
    fake = FakeBq()
    app.state.bq = fake
    body = client.post("/jobs/refresh").json()
    assert [j["model"] for j in body["jobs"]] == list(analytics.MODELS) and body["errors"] == []
    assert all(sql.startswith("--") and "CREATE OR REPLACE MODEL" in sql and cap == 2_000_000_000
               for sql, cap in fake.submitted)
    st = client.get("/jobs/status?ids=job_1,job_2,job_3").json()
    assert st["done"] is False and st["failed"] == ["job_3"]
    assert client.get("/jobs/status?ids=job_1").json()["done"] is True
    assert client.get("/jobs/status?ids=bad id").status_code == 400


def test_jobs_refresh_reports_partial_errors():
    app.state.bq = FakeBq(error=RuntimeError("denied"))
    body = client.post("/jobs/refresh").json()
    assert len(body["jobs"]) == 2 and body["errors"] == [{"model": "m_parts_demand", "error": "RuntimeError"}]


@pytest.mark.parametrize("model", analytics.MODELS)
def test_vendored_model_sql_matches_gcp_bq(model):
    with open(os.path.join(ROOT, "gcp", "bq", f"{model}.sql"), encoding="utf-8") as f:
        assert analytics.model_sql(model) == f.read(), "cp gcp/bq/m_*.sql gcp/insights/app/sql/"
