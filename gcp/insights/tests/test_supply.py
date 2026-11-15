from fastapi.testclient import TestClient

from app import supply
from app.main import app

client = TestClient(app)


class FakeBq:
    def __init__(self, orders=None, stockouts=None, error=False):
        self._orders = orders or []
        self._stockouts = stockouts or []
        self._error = error

    def rows(self, sql, params=None):
        if self._error:
            raise RuntimeError("boom")
        return self._orders if "v_supply_orders" in sql else self._stockouts


def test_orders_summary_pure():
    out = supply.orders_summary([
        {"status": "received", "kind": "transfer", "orders": 2, "downtime_avoided_usd": 68400, "cost_usd": 360},
        {"status": "in_transit", "kind": "transfer", "orders": 1, "downtime_avoided_usd": 0, "cost_usd": 180},
        {"status": "suggested", "kind": "purchase_order", "orders": 1, "downtime_avoided_usd": 0, "cost_usd": 0},
    ])
    assert out["total"] == 4
    assert out["received"] == 2
    assert out["in_flight"] == 1  # in_transit
    assert out["downtime_avoided_usd"] == 68400.0


def test_supply_analytics_ok():
    app.state.bq = FakeBq(
        orders=[{"status": "in_transit", "kind": "transfer", "orders": 1,
                 "downtime_avoided_usd": 180, "cost_usd": 180}],
        stockouts=[{"day": "2026-11-15", "part_id": "PRT-04", "zone_id": "NORTE",
                    "shortages": 1, "resolved": 0, "avg_resolution_hours": None}],
    )
    r = client.get("/supply/analytics?tenant=mineria&days=7")
    assert r.status_code == 200
    b = r.json()
    assert b["tenant"] == "mineria" and b["days"] == 7
    assert b["orders"]["in_flight"] == 1
    assert b["stockouts_daily"][0]["part_id"] == "PRT-04"


def test_supply_analytics_all_tenants_default():
    app.state.bq = FakeBq()
    r = client.get("/supply/analytics")
    assert r.status_code == 200 and r.json()["tenant"] == "all"


def test_supply_analytics_bad_tenant():
    app.state.bq = FakeBq()
    assert client.get("/supply/analytics?tenant=nope").status_code == 400


def test_supply_analytics_bq_error_503():
    app.state.bq = FakeBq(error=True)
    assert client.get("/supply/analytics?tenant=mineria").status_code == 503
