from fastapi.testclient import TestClient

from app.bq import build_peers_response
from app.main import app

client = TestClient(app)

ROW = {
    "machine_id": "EX-001",
    "model": "EX",
    "zone_id": "NORTE",
    "avg_fuel_rate_lph": 41.2,
    "avg_vibration_mm_s": 6.1,
    "avg_coolant_temp_c": 88.0,
    "avg_hydraulic_pressure_bar": 210.0,
    "operating_ratio": 0.72,
    "hours_total": 1508.0,
    "fuel_pctile": 0.85,
    "vibration_pctile": 0.9,
    "peer_count": 20,
    "peer_avg_fuel_rate_lph": 38.5,
    "peer_avg_vibration_mm_s": 4.8,
}


class FakeBq:
    def __init__(self, rows=None, error=None):
        self.rows = rows if rows is not None else []
        self.error = error

    def peers(self, machine_id):
        if self.error:
            raise self.error
        return self.rows


def test_peers_returns_position_vs_cohort():
    app.state.bq = FakeBq(rows=[ROW])
    r = client.get("/peers/EX-001")
    assert r.status_code == 200
    body = r.json()
    assert body["peer_count"] == 20
    assert body["percentiles"]["fuel_rate_lph"] == 0.85
    assert body["peer_avg"]["vibration_mm_s"] == 4.8


def test_peers_bad_machine_id():
    app.state.bq = FakeBq(rows=[ROW])
    assert client.get("/peers/NOPE").status_code == 400


def test_peers_not_found_when_empty():
    app.state.bq = FakeBq(rows=[])
    assert client.get("/peers/EX-099").status_code == 404


def test_peers_bq_error_is_503():
    app.state.bq = FakeBq(error=RuntimeError("boom"))
    assert client.get("/peers/EX-001").status_code == 503


def test_build_peers_response_none_on_empty():
    assert build_peers_response([], "EX-001") is None
