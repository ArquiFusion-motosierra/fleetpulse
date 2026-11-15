import gzip
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import jsonschema
import pytest

from fpsim import backfill, handler, live, physics
from fpsim.fleet import by_id, fleet
from fpsim.scenarios import parse_active

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
SCHEMA = lambda name: json.load(open(os.path.join(ROOT, "contracts", name), encoding="utf-8"))  # noqa: E731
NOW = datetime(2026, 11, 15, 19, 30, tzinfo=timezone.utc)  # 14:30 en Lima


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_ids_in_sync():
    yaml = pytest.importorskip("yaml")
    with open(os.path.join(ROOT, "contracts", "ids.yaml"), encoding="utf-8") as f:
        expected = yaml.safe_load(f)
    with open(os.path.join(ROOT, "simulator", "fpsim", "ids.json"), encoding="utf-8") as f:
        assert json.load(f) == expected, "corre: python simulator/fpsim/sync_ids.py"


def test_fleet_has_100_machines_and_scenario_ids():
    ids = by_id()
    assert len(fleet()) == 100
    for mid in ("EX-017", "EX-003", "VQ-011", "VQ-014", "TO-002", "CF-042", "MN-005", "EX-009", "VQ-031", "MN-003"):
        assert mid in ids
    assert {ids[m].zone_id for m in ("EX-017", "EX-003", "VQ-011", "VQ-014", "TO-002")} == {"NORTE"}


def test_live_batch_matches_contract_and_is_deterministic():
    b = live.build_batch(NOW, {})
    jsonschema.validate(b, SCHEMA("telemetry-batch.schema.json"))
    assert len(b["readings"]) == 1000
    assert b == live.build_batch(NOW + timedelta(seconds=20), {})  # mismo minuto -> mismo lote


def test_counters_are_monotonic():
    m = by_id()["VQ-011"]
    rs = [physics.reading(m, NOW + timedelta(minutes=15 * i)) for i in range(50)]
    for key in ("cumulative_operating_hours", "cumulative_idle_hours", "cumulative_fuel_used_l",
                "cumulative_load_count", "cumulative_payload_t", "distance_km"):
        assert all(a[key] <= b[key] for a, b in zip(rs, rs[1:])), key


def _mean(rows, key):
    rows = [r for r in rows if r["engine_status"] == "operating"]
    return sum(r[key] for r in rows) / len(rows)


def test_s1_drift_degrades_ex017_only():
    base = live.build_batch(NOW, {}, machine_ids={"EX-017", "EX-018"})["readings"]
    act = parse_active({"S1": {"since": iso(NOW - timedelta(minutes=15))}})
    drift = live.build_batch(NOW, act, machine_ids={"EX-017", "EX-018"})["readings"]
    b17 = [r for r in base if r["machine_id"] == "EX-017"]
    d17 = [r for r in drift if r["machine_id"] == "EX-017"]
    assert _mean(d17, "hydraulic_pressure_bar") < 0.92 * _mean(b17, "hydraulic_pressure_bar")
    assert _mean(d17, "hydraulic_oil_temp_c") > _mean(b17, "hydraulic_oil_temp_c") + 6
    assert _mean(d17, "vibration_mm_s") > 1.25 * _mean(b17, "vibration_mm_s")
    assert d17[-1]["fault_codes"][0]["severity"] == "high"
    assert [r for r in base if r["machine_id"] == "EX-018"] == [r for r in drift if r["machine_id"] == "EX-018"]


def test_s2_context_points_to_lot_l2311():
    act = parse_active({"S2": {"since": iso(NOW - timedelta(seconds=10))}})
    ctx = live.build_context(NOW, act)
    schema = SCHEMA("fuel.refill.schema.json")
    for c in ctx:
        jsonschema.validate(c, schema)
    lots = {c["machine_id"]: c["fuel_lot"] for c in ctx if c["type"] == "fuel.refill"}
    assert {m: lots.get(m) for m in ("EX-003", "VQ-011", "VQ-014", "TO-002")} == dict.fromkeys(
        ("EX-003", "VQ-011", "VQ-014", "TO-002"), "L-2311")


def _geohash6(lat: float, lon: float) -> str:
    alphabet, rng, bits, even = "0123456789bcdefghjkmnpqrstuvwxyz", {True: [-180.0, 180.0], False: [-90.0, 90.0]}, [], True
    while len(bits) < 30:
        lo, hi = rng[even]
        mid = (lo + hi) / 2
        value = lon if even else lat
        bits.append(int(value >= mid))
        rng[even] = [mid, hi] if value >= mid else [lo, mid]
        even = not even
    return "".join(alphabet[int("".join(map(str, bits[i:i + 5])), 2)] for i in range(0, 30, 5))


S2 = ("EX-003", "VQ-011", "VQ-014", "TO-002")


def test_s2_pins_the_four_machines_to_one_geohash6_cell():
    """La correlacion en vivo agrupa por celda geohash6: sin S2 las cuatro no comparten celda; con S2 si, en
    todos los bloques de 15 min (los volquetes dejan de rotar de zona) y en la zona NORTE."""
    since = NOW - timedelta(minutes=5)
    act = parse_active({"S2": {"since": iso(since)}})
    cells, zones_seen = set(), set()
    for minutes in range(0, 90, 7):
        batch = live.build_batch(NOW + timedelta(minutes=minutes), act, machine_ids=set(S2) | {"EX-018"})
        for r in batch["readings"]:
            if r["machine_id"] in S2:
                cells.add(_geohash6(r["lat"], r["lon"]))
                zones_seen.add(r["zone_id"])
    assert len(cells) == 1 and zones_seen == {"NORTE"}
    base = live.build_batch(NOW, {}, machine_ids=set(S2))["readings"]
    assert len({_geohash6(r["lat"], r["lon"]) for r in base}) > 1  # el problema que resuelve


def test_s2_pin_only_after_since_and_only_s2_machines():
    def where(batch):
        return [(r["lat"], r["lon"], r["zone_id"]) for r in batch["readings"]]

    act = parse_active({"S2": {"since": iso(NOW + timedelta(minutes=30))}})
    assert where(live.build_batch(NOW, act, machine_ids=set(S2))) == where(live.build_batch(NOW, {}, machine_ids=set(S2)))
    act = parse_active({"S2": {"since": iso(NOW - timedelta(minutes=5))}})
    other = {"EX-018", "VQ-022"}
    assert live.build_batch(NOW, act, machine_ids=other) == live.build_batch(NOW, {}, machine_ids=other)


def test_s3_idle_and_s6_usage():
    day = [NOW.replace(hour=0) + timedelta(minutes=15 * i) for i in range(96 * 3)]

    def idle_share(mid):
        st = [physics.engine_status(by_id()[mid], t) for t in day]
        on = [s for s in st if s != "off"]
        return st, sum(s == "idle" for s in on) / len(on)

    _, cf042 = idle_share("CF-042")
    _, cf031 = idle_share("CF-031")
    assert cf042 > 0.35 > cf031
    vq031, _ = idle_share("VQ-031")
    mn003, _ = idle_share("MN-003")
    assert sum(s != "off" for s in vq031) / 3 * 0.25 == pytest.approx(2.0)
    assert sum(s != "off" for s in mn003) / 3 * 0.25 == pytest.approx(20.0)


def test_backfill_small_run(tmp_path):
    end = datetime(2026, 11, 14, tzinfo=timezone.utc)
    s = backfill.run(days=2, step=timedelta(minutes=15), out=str(tmp_path), end=end,
                     machine_ids={"EX-009", "EX-017", "VQ-011"}, verbose=False)
    assert s["readings"] == 2 * 96 * 10 * 3
    with gzip.open(tmp_path / "telemetry" / "dt=2026-11-13" / "hr=15" / "backfill.ndjson.gz", "rt") as fh:
        first = json.loads(fh.readline())
    reading_schema = SCHEMA("telemetry-batch.schema.json")
    jsonschema.validate({"batch_id": first.pop("batch_id"), "sent_at": first["ts"], "source": "simulator.backfill",
                         "readings": [first]}, reading_schema)


def test_backfill_incident_events_match_contracts():
    end = datetime(2026, 11, 15, tzinfo=timezone.utc)
    faults = backfill.plan_faults(end - timedelta(days=30), end)
    assert 30 <= len(faults) <= 40
    s5 = [f for f in faults if f.machine_id == "EX-009"]
    assert s5 and s5[0].end.date() == (end - timedelta(days=10)).date()
    inc, part = SCHEMA("incident.schema.json"), SCHEMA("part.schema.json")
    events = backfill.incident_events(faults)
    for e in events:
        jsonschema.validate(e, part if e["event_type"].startswith("part.") else inc)
    assert len({e["event_id"] for e in events}) == len(events)


class _Resp(io.BytesIO):
    status = 202

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Ssm:
    def __init__(self, values):
        self.values = values

    def get_parameter(self, Name):
        return {"Parameter": {"Value": self.values[Name]}}


class _Secrets:
    def get_secret_value(self, SecretId):
        return {"SecretString": json.dumps({"api_key": "test-key"})}


def test_handler_posts_batch_with_api_key(monkeypatch):
    handler._cache.clear()
    sent = []

    def opener(req, timeout):
        sent.append((req.full_url, req.headers["X-api-key"], json.loads(req.data), timeout))
        return _Resp()

    ssm = _Ssm({"/fleetpulse/sim/scenarios": json.dumps({"S1": {"since": iso(NOW)}}),
                "/fleetpulse/api/url": "https://api.example/"})
    out = handler.handler({}, None, now=NOW, ssm=ssm, secrets=_Secrets(), opener=opener)
    assert sent[0][0] == "https://api.example/telemetry" and sent[0][1] == "test-key"
    assert sent[0][2]["batch_id"] == out["batch_id"] and out["readings"] == 1000
    assert sent[0][3] == handler.HTTP_TIMEOUT_S


def test_drift_script_apply():
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import drift

    st = drift.apply({}, "on", "S1", NOW)
    assert st == {"S1": {"since": "2026-11-15T19:30:00Z"}}
    assert drift.apply(st, "off", "all", NOW) == {"S1": False, "S2": False, "S5": False, "S7": False}
    with pytest.raises(SystemExit):
        drift.apply({}, "on", "S3", NOW)


# ---------- T26: ficha del fabricante ----------

def test_oem_specs_valid_and_in_sync():
    yaml = pytest.importorskip("yaml")
    with open(os.path.join(ROOT, "contracts", "oem_specs.yaml"), encoding="utf-8") as f:
        data = yaml.safe_load(f)
    jsonschema.validate(data, SCHEMA("oem_specs.schema.json"))
    with open(os.path.join(ROOT, "simulator", "fpsim", "oem.json"), encoding="utf-8") as f:
        assert json.load(f) == data, "corre: python simulator/fpsim/sync_ids.py"
    parts = {p["part_id"] for p in yaml.safe_load(open(os.path.join(ROOT, "contracts", "ids.yaml"), encoding="utf-8"))["parts"]}
    for m in data["models"].values():
        for s in m["service_schedule"]:
            assert {p["part_id"] for p in s["parts"]} <= parts


VAR_TO_LIMIT = {"coolant_temp_c": "coolant_temp_c", "hydraulic_oil_temp_c": "hydraulic_oil_temp_c",
                "hydraulic_pressure_bar": "hydraulic_pressure_bar", "vibration_mm_s": "vibration_mm_s",
                "fuel_rate_lph": "fuel_rate_lph"}


def test_normal_operation_stays_inside_oem_normal_ranges():
    """La ficha y el simulador no se contradicen: operando sin escenarios, >= 99 % dentro del rango normal."""
    from fpsim import oem
    inside = total = 0
    worst = {}
    for h in range(0, 24, 3):
        t = NOW.replace(hour=h)
        for r in live.build_batch(t, {})["readings"]:
            if r["engine_status"] != "operating":
                continue
            lim = oem.model(r["model"])["limits"]
            for var, key in VAR_TO_LIMIT.items():
                lo, hi = lim[key]["normal"]
                ok = lo <= r[var] <= hi
                inside += ok
                total += 1
                if not ok:
                    worst[(r["model"], var)] = worst.get((r["model"], var), 0) + 1
    assert inside / total >= 0.99, sorted(worst.items(), key=lambda x: -x[1])[:5]


def test_normal_harmonics_do_not_cross_natural_frequencies():
    from fpsim import oem
    for model_id in ("EX", "CF", "VQ", "MN", "TO"):
        rated = oem.model(model_id)["rated_rpm"]
        for rpm in range(int(rated * 0.93), int(rated * 1.03), 5):
            assert oem.resonance(model_id, rpm) is None, (model_id, rpm, oem.resonance(model_id, rpm))


def test_head_gasket_raises_rpm_into_vq_chassis_resonance():
    from fpsim import oem
    from fpsim.scenarios import signature
    eff = signature("head_gasket", 1.0)
    rpm = oem.model("VQ")["rated_rpm"] * eff.mult["rpm"]
    hit = oem.resonance("VQ", rpm)
    assert hit and hit["harmonic"] == "2x" and hit["natural_frequency_hz"] == 62
    assert eff.faults[0]["spn"] == 111


# ---------- T27: S7 junta de culata en vivo ----------

def _operating(rows):
    return [r for r in rows if r["engine_status"] == "operating"]


def test_s7_head_gasket_live_on_vq022_only():
    act = parse_active({"S7": {"since": iso(NOW - timedelta(minutes=22))}})
    base = live.build_batch(NOW, {}, machine_ids={"VQ-022", "VQ-023"})["readings"]
    s7 = live.build_batch(NOW, act, machine_ids={"VQ-022", "VQ-023"})["readings"]
    b22, d22 = [r for r in base if r["machine_id"] == "VQ-022"], [r for r in s7 if r["machine_id"] == "VQ-022"]
    assert _mean(d22, "coolant_temp_c") > _mean(b22, "coolant_temp_c") + 12
    assert _mean(d22, "engine_rpm") > 1.1 * _mean(b22, "engine_rpm")
    assert all(r["fault_codes"][0]["spn"] == 111 for r in _operating(d22))
    assert [r for r in base if r["machine_id"] == "VQ-023"] == [r for r in s7 if r["machine_id"] == "VQ-023"]


def test_s7_crosses_the_chassis_mode_and_vibration_jumps():
    from fpsim import oem
    act = parse_active({"S7": {"since": iso(NOW - timedelta(minutes=22))}})
    rows = []
    for m in range(0, 90, 3):
        rows += _operating(live.build_batch(NOW + timedelta(minutes=m), act, machine_ids={"VQ-022"})["readings"])
    hit = [r for r in rows if oem.resonance("VQ", r["engine_rpm"])]
    miss = [r for r in rows if not oem.resonance("VQ", r["engine_rpm"])]
    assert 0.2 < len(hit) / len(rows) < 0.8  # intermitente: depende de la carga del momento
    assert sum(r["vibration_mm_s"] for r in hit) / len(hit) > 1.3 * sum(r["vibration_mm_s"] for r in miss) / len(miss)


def test_s7_coolant_crosses_the_oem_warning():
    from fpsim import oem
    act = parse_active({"S7": {"since": iso(NOW - timedelta(minutes=25))}})
    d = _operating(live.build_batch(NOW, act, machine_ids={"VQ-022"})["readings"])
    assert _mean(d, "coolant_temp_c") > oem.model("VQ")["limits"]["coolant_temp_c"]["warning"]
