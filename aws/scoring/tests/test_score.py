"""Pruebas puras de score, failure_mode y baselines.from_readings."""
import pytest

from scoring import baselines, failure_mode, score

BASE = {v: {"mu": 100.0, "sigma": 10.0} for v in score.VARS}


def test_zscores_and_weights():
    base = {"hydraulic_pressure_bar": {"mu": 300.0, "sigma": 10.0}}
    z = score.zscores({"hydraulic_pressure_bar": 274.0}, base, None)
    assert z["hydraulic_pressure_bar"]["self"] == pytest.approx(-2.6)
    assert z["hydraulic_pressure_bar"]["peer"] is None
    out = score.compute({"hydraulic_pressure_bar": 274.0}, base, None, [])
    assert out["score"] == pytest.approx(2.6)  # peso 1.0 * |z|
    assert out["deviated_vars"] == [{"var": "hydraulic_pressure_bar", "value": 274.0, "z": pytest.approx(-2.6)}]


def test_score_is_weighted_sum_plus_trend():
    base = {"hydraulic_pressure_bar": {"mu": 300.0, "sigma": 10.0}, "hydraulic_oil_temp_c": {"mu": 80.0, "sigma": 4.0}}
    means = {"hydraulic_pressure_bar": 274.0, "hydraulic_oil_temp_c": 88.0}
    assert score.compute(means, base, None, [])["score"] == pytest.approx(1.0 * 2.6 + 0.9 * 2.0)  # 4.4
    prev = [{"hydraulic_oil_temp_c": 84.0}, {"hydraulic_oil_temp_c": 86.0}]
    assert score.compute(means, base, None, prev)["score"] == pytest.approx(4.9)


def test_zscores_sigma_floor_and_missing_var():
    z = score.zscores({"engine_rpm": 5.0, "vibration_mm_s": 1.0}, {"engine_rpm": {"mu": 4.0, "sigma": 0.0}}, None)
    assert z["engine_rpm"]["self"] == pytest.approx(1e6)
    assert "vibration_mm_s" not in z


def test_batch_means_skips_off():
    rs = [{"engine_status": "on", "engine_rpm": 1000}, {"engine_status": "on", "engine_rpm": 2000},
          {"engine_status": "off", "engine_rpm": 0}]
    assert score.batch_means(rs)["engine_rpm"] == 1500.0
    assert score.batch_means([{"engine_status": "off", "engine_rpm": 0}]) == {}


def test_trend_bonus_counts_three_rising():
    prev = [{"vibration_mm_s": 1.0, "engine_rpm": 5.0}, {"vibration_mm_s": 2.0, "engine_rpm": 4.0}]
    assert score.trend_bonus({"vibration_mm_s": 3.0, "engine_rpm": 6.0}, prev) == 1
    assert score.trend_bonus({"vibration_mm_s": 3.0}, prev[:1]) == 0


@pytest.mark.parametrize("value,sev", [(1.19, "ok"), (1.2, "observe"), (2.0, "schedule"), (3.5, "immediate")])
def test_severity_thresholds(value, sev):
    base = {"vibration_mm_s": {"mu": 0.0, "sigma": 1.0}}
    assert score.compute({"vibration_mm_s": value}, base, None, [])["severity"] == sev


def test_peer_z_can_drive_score():
    self_b = {"vibration_mm_s": {"mu": 5.0, "sigma": 1.0}}
    peer_b = {"vibration_mm_s": {"mu": 1.0, "sigma": 1.0}}
    out = score.compute({"vibration_mm_s": 5.0}, self_b, peer_b, [])
    assert out["z"]["vibration_mm_s"] == {"self": 0.0, "peer": 4.0}
    assert out["score"] == pytest.approx(4.0)


def dv(**zs):
    return [{"var": k, "value": 0.0, "z": z} for k, z in zs.items()]


def test_failure_mode_hydraulic_pump():
    assert failure_mode.infer(dv(hydraulic_pressure_bar=-3.0, hydraulic_oil_temp_c=2.5)) == "hydraulic_pump"
    assert failure_mode.infer(dv(hydraulic_pressure_bar=-3.0, vibration_mm_s=1.5)) == "hydraulic_pump"


def test_failure_mode_fuel_contamination():
    assert failure_mode.infer(dv(fuel_rate_lph=2.0, engine_rpm=-1.5)) == "fuel_contamination"
    assert failure_mode.infer(dv(fuel_rate_lph=2.0, coolant_temp_c=1.5)) == "fuel_contamination"


def test_failure_mode_single_var_rules_and_fallback():
    assert failure_mode.infer(dv(coolant_temp_c=2.0)) == "engine_overheat"
    assert failure_mode.infer(dv(hydraulic_oil_temp_c=2.0)) == "hydraulic_overheat"
    assert failure_mode.infer(dv(vibration_mm_s=2.0)) == "final_drive"
    assert failure_mode.infer(dv(vibration_mm_s=-2.0)) == "sensor_fault"
    assert failure_mode.infer([]) == "sensor_fault"


def test_baseline_needs_50_readings():
    mk = lambda i: {"engine_status": "on", **{v: float(i % 5) for v in score.VARS}}  # noqa: E731
    assert baselines.from_readings([mk(i) for i in range(49)]) is None
    b = baselines.from_readings([mk(i) for i in range(50)])
    assert b["engine_rpm"]["mu"] == pytest.approx(2.0)
    assert b["engine_rpm"]["sigma"] > 0
    off = [{"engine_status": "off", **{v: 0.0 for v in score.VARS}}] * 30
    assert baselines.from_readings([mk(i) for i in range(30)] + off) is None
