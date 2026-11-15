"""scripts/seed-demo.py (T47): que restos detecta y las invariantes de stock, sin AWS."""
import importlib.util
import os
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture(scope="module")
def sd():
    pytest.importorskip("boto3")
    spec = importlib.util.spec_from_file_location("seed_demo", os.path.join(HERE, "..", "seed-demo.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _items():
    inc = lambda i, st: {"PK": f"INCIDENT#{i}", "SK": "META", "status": st, "tenant_id": "mineria"}  # noqa: E731
    return [
        inc("INC-EX-017-20261115T1300", "closed"), inc("INC-EX-017-20261115T2100", "open"),
        {"PK": "STOCK#PRT-04#NORTE", "SK": "META", "on_hand": 1, "reserved": 1, "available": 0, "in_transit": 1},
        {"PK": "STOCK#PRT-04#FUNDO-A", "SK": "META", "on_hand": 2, "reserved": 0, "available": 2, "in_transit": 0},
        {"PK": "RSV#INC-EX-017-20261115T1300", "SK": "PRT-04#NORTE", "part_id": "PRT-04", "zone_id": "NORTE", "qty": 1,
         "status": "reserved", "incident_id": "INC-EX-017-20261115T1300"},
        {"PK": "TRF#TRF-FUNDO-A-NORTE-PRT-04-20261115T1656", "SK": "META", "id": "TRF-FUNDO-A-NORTE-PRT-04-20261115T1656",
         "status": "in_transit", "part_id": "PRT-04", "zone_id": "NORTE", "from_zone": "FUNDO-A", "qty": 1},
        {"PK": "BACKORDER#PRT-04#NORTE", "SK": "INC-EX-017-20261115T1300", "qty": 1},
        {"PK": "BACKORDER#PRT-04#NORTE", "SK": "INC-EX-017-20261115T2100", "qty": 1},
        {"PK": "REORDER#PRT-04#NORTE", "SK": "2026-11-15T13:07:52Z", "source": "shortage",
         "incident_id": "INC-EX-017-20261115T1300"},
        {"PK": "REORDER#PRT-01#NORTE", "SK": "2026-11-15T19:33:36Z", "source": "planning"},
        {"PK": "REPORT#RPT-EX-017-20261115T180744", "SK": "META", "status": "in_review",
         "incident_id": "INC-EX-017-20261115T1300", "tenant_id": "mineria"},
    ]


def test_detecta_solo_los_restos_de_incidentes_cerrados(sd, monkeypatch):
    monkeypatch.setattr(sd, "execution_status", lambda lab, machine, name: "FAILED")
    acts = [(a, i["PK"], i["SK"]) for a, i in sd.plan_cleanup(SimpleNamespace(), sd.Snapshot(_items()))]
    assert acts == [
        ("delete_backorder", "BACKORDER#PRT-04#NORTE", "INC-EX-017-20261115T1300"),
        ("cancel_doc", "TRF#TRF-FUNDO-A-NORTE-PRT-04-20261115T1656", "META"),
        ("release_rsv", "RSV#INC-EX-017-20261115T1300", "PRT-04#NORTE"),
        ("delete_reorder", "REORDER#PRT-04#NORTE", "2026-11-15T13:07:52Z"),
        ("close_report", "REPORT#RPT-EX-017-20261115T180744", "META"),
    ]


def test_una_orden_con_ejecucion_viva_no_es_resto(sd, monkeypatch):
    monkeypatch.setattr(sd, "execution_status", lambda lab, machine, name: "RUNNING")
    acts = [a for a, _ in sd.plan_cleanup(SimpleNamespace(), sd.Snapshot(_items()))]
    assert "cancel_doc" not in acts


def test_invariantes_de_stock(sd):
    assert sd.invariants(sd.Snapshot(_items())) == []
    broken = _items()
    broken[2] = {**broken[2], "in_transit": 2, "available": 1}
    errs = sd.invariants(sd.Snapshot(broken))
    assert any("available 1" in e for e in errs) and any("in_transit=2" in e for e in errs)
