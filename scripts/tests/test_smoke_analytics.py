"""scripts/smoke_analytics.py (T20/T42) contra un insights falso: sin red, sin gcloud, sin BigQuery."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import smoke_analytics  # noqa: E402

ORDERS = {"total": 2, "by_status": {"in_transit": 1, "received": 1}, "in_flight": 1, "received": 1,
          "downtime_avoided_usd": 136800.0, "cost_usd": 360.0}
STOCK = [{"day": "2026-11-15", "part_id": "PRT-04", "zone_id": "NORTE", "shortages": 4, "resolved": 1,
          "avg_resolution_hours": 2.0}]
KPI = {"machine_id": "EX-001", "operating_h": 14.0, "idle_ratio": 0.125, "utilization": 0.583, "fuel_lph": 35.0,
       "cost_per_h": 58.25, "quadrant": "workhorse", "fuel_vs_peers_pct": 7.7, "cluster_id": 2}


def fake(overrides=None, slow=0.1):
    states = {"n": 0}

    def call(method, path, auth=True):
        if overrides and path in overrides:
            return overrides[path]
        if not auth:
            return 403, {}, 0.1
        p = path.split("?")[0]
        if p == "/health":
            return 200, {"status": "ok", "project": "fleetpulse-g19-2026"}, 0.1
        if p == "/supply/analytics":
            if "tenant=marte" in path:
                return 400, {"detail": "BAD_TENANT"}, 0.1
            if "days=91" in path:
                return 422, {}, 0.1
            return 200, {"tenant": "mineria" if "mineria" in path else "all", "days": 14, "orders": ORDERS,
                         "stockouts_daily": STOCK}, slow
        if p == "/anomalies/fuel":
            return 200, {"day": "2026-11-14", "days": 7, "threshold": 0.95, "anomalies": [
                {"machine_id": "CF-042", "day": "2026-11-13", "fuel_l": 412.3, "expected": [250.0, 330.0],
                 "anomaly_probability": 0.991}]}, 0.2
        if p == "/forecast/parts":
            return 200, {"horizon_days": 14, "parts": [{"part_id": "PRT-02", "zone_id": "NORTE", "demand_14d": 3,
                                                        "demand_14d_upper": 4}]}, 0.2
        if p == "/kpis/daily":
            return 200, {"day": "2026-11-14", "machines": [KPI]}, 1.0
        if p == "/jobs/refresh":
            return 200, {"jobs": [{"model": m, "job_id": f"j{i}"} for i, m in
                                  enumerate(("m_peer_clusters", "m_fuel_trend", "m_parts_demand"))], "errors": []}, 0.5
        if p == "/jobs/status":
            if "bad" in path:
                return 400, {"detail": "BAD_JOB_IDS"}, 0.1
            ids = path.split("ids=")[1].split(",")
            states["n"] += 1
            st = "DONE" if states["n"] > 1 else "RUNNING"
            return 200, {"jobs": [{"job_id": j, "state": st, "error": None} for j in ids], "done": st == "DONE",
                         "failed": []}, 0.1
        return 404, {}, 0.1
    return call


def run(call, **kw):
    lines = []
    rc = smoke_analytics.Smoke(call, out=lines.append, sleep=lambda s: None, **kw).run()
    return rc, lines


def test_all_green_with_every_option():
    rc, lines = run(fake(), job_ids=["bqjob_1"], kpis=True, refresh=True)
    assert rc == 0, lines
    assert not [x for x in lines if x.startswith("[FAIL") or x.startswith("[WARN")], lines
    assert any("9 /jobs/refresh" in x for x in lines)


def test_supply_over_slo_fails():
    rc, lines = run(fake(slow=3.2))
    assert rc == 1 and any("[FAIL" in x and "/supply/analytics?tenant=mineria" in x for x in lines)


def test_public_service_fails():
    rc, lines = run(fake({"/health": (200, {"status": "ok"}, 0.1)}))
    assert rc == 1 and "[FAIL" in lines[0]


def test_forecast_503_fails_and_empty_warns():
    rc, lines = run(fake({"/forecast/parts": (503, {"detail": "BQ_UNAVAILABLE"}, 0.3)}))
    assert rc == 1 and any("BQ_UNAVAILABLE" in x for x in lines)
    empty = {"/forecast/parts": (200, {"horizon_days": 14, "parts": []}, 0.3)}
    assert run(fake(empty))[0] == 0
    assert run(fake(empty), strict_forecast=True)[0] == 1


def test_malformed_body_is_a_fail_not_a_crash():
    rc, lines = run(fake({"/anomalies/fuel?days=7": (200, {"day": "x"}, 0.1)}))
    assert rc == 1 and any("KeyError" in x or "threshold" in x for x in lines)
    assert any("6 /forecast/parts" in x for x in lines)  # siguio con el resto


def test_kpis_out_of_contract_fails():
    bad = {**KPI, "machine_id": "EX-1001"}
    rc, lines = run(fake({"/kpis/daily": (200, {"day": "2026-11-14", "machines": [bad]}, 1.0)}), kpis=True)
    assert rc == 1 and any("fuera de contrato" in x for x in lines)
