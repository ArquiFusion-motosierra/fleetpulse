"""scripts/smoke_supply.py (T43) contra la consola en modo de prueba: la historia completa sin AWS."""
import importlib
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "gcp", "console"))

import smoke_supply  # noqa: E402

TOKENS = {"head": "mock.maintenance_head", "admin": "mock.admin", "admin_agro": "mock.admin_agro"}


@pytest.fixture
def console(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    monkeypatch.setenv("MOCK", "1")
    monkeypatch.setenv("STATIC_DIR", "/nonexistent")
    import app.main as main
    importlib.reload(main)
    c = TestClient(main.app)

    def call(method, path, token, body=None):
        r = c.request(method, path, json=body, headers={"authorization": f"Bearer {token}"})
        return r.status_code, r.json()
    return call


def test_full_story_passes_in_mock(console):
    lines = []
    story = smoke_supply.Story(console, TOKENS, out=lines.append, sleep=lambda s: None)
    assert story.run() == 0
    by = {name: (level, detail) for level, name, _, detail in story.steps}
    assert by["4 jefe aprueba"][0] == "OK" and by["6 admin de mina recibe"][0] == "OK"
    assert "reservada para INC-EX-017" in by["6 admin de mina recibe"][1]
    assert any("v_supply_orders" in x for x in lines)


def test_second_run_fails_cleanly_when_already_received(console):
    smoke_supply.Story(console, TOKENS, out=lambda x: None, sleep=lambda s: None).run()
    again = smoke_supply.Story(console, TOKENS, out=lambda x: None, sleep=lambda s: None)
    assert again.run() == 1  # PRT-04 ya no esta en quiebre en NORTE: hay que volver a sembrar
    assert again.steps[0][0] == "FAIL" or again.steps[1][0] == "FAIL"


def test_read_only_does_not_change_anything(console):
    s = smoke_supply.Story(console, TOKENS, read_only=True, out=lambda x: None, sleep=lambda s: None)
    assert s.run() == 0
    assert smoke_supply.Story(console, TOKENS, out=lambda x: None, sleep=lambda s: None).run() == 0
