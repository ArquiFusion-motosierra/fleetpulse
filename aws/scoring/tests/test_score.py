"""Pruebas puras de score, failure_mode y baselines.from_readings."""
import pytest

from scoring import baselines, failure_mode, score

BASE = {v: {"mu": 100.0, "sigma": 10.0} for v in score.VARS}


def test_zscores_and_weights_with_dead_band():
    base = {"hydraulic_pressure_bar": {"mu": 300.0, "sigma": 10.0}}
    z = score.zscores({"hydraulic_pressure_bar": 274.0}, base, None)
    assert z["hydraulic_pressure_bar"]["self"] == pytest.approx(-2.6)
    assert z["hydraulic_pressure_bar"]["peer"] is None
    out = score.compute({"hydraulic_pressure_bar": 274.0}, base, None, [])
    assert score.DEADBAND == 2.25
    assert out["score"] == pytest.approx(0.35)  # peso 1.0 * (|z| - 2.25)
    assert out["deviated_vars"] == [{"var": "hydraulic_pressure_bar", "value": 274.0, "z": pytest.approx(-2.6)}]


def test_healthy_noise_inside_the_dead_band_scores_zero():
    """Seis variables a |z| 1.9 sumaban 9.8 con la formula sin banda; dentro de la banda no aportan."""
    means = {v: 100.0 + 19.0 for v in score.VARS}
    out = score.compute(means, BASE, None, [])
    assert out["score"] == 0.0 and out["severity"] == "ok"
    assert {d["var"] for d in out["deviated_vars"]} == set(score.VARS)  # |z| >= 1.0 se informa igual


def test_score_is_weighted_excess_plus_trend_only_outside_the_band():
    base = {"hydraulic_pressure_bar": {"mu": 300.0, "sigma": 10.0}, "hydraulic_oil_temp_c": {"mu": 80.0, "sigma": 4.0},
            "vibration_mm_s": {"mu": 5.0, "sigma": 1.0}}
    means = {"hydraulic_pressure_bar": 250.0, "hydraulic_oil_temp_c": 92.0, "vibration_mm_s": 6.5}
    assert score.compute(means, base, None, [])["score"] == pytest.approx(1.0 * 2.75 + 0.9 * 0.75)  # 3.425
    # oil (|z| 3) y vibracion (|z| 1.5) suben 3 lotes seguidos: solo cuenta la que ya salio de la banda
    prev = [{"hydraulic_oil_temp_c": 84.0, "vibration_mm_s": 5.5}, {"hydraulic_oil_temp_c": 88.0, "vibration_mm_s": 6.0}]
    assert score.compute(means, base, None, prev)["score"] == pytest.approx(3.425 + 0.5)


def test_zscores_relative_sigma_floor_and_missing_var():
    # sigma 0 ya no satura en 1e6: el piso es el 1 % de |mu| (4.0 -> 0.04)
    z = score.zscores({"engine_rpm": 5.0, "vibration_mm_s": 1.0}, {"engine_rpm": {"mu": 4.0, "sigma": 0.0}}, None)
    assert z["engine_rpm"]["self"] == pytest.approx(25.0)
    assert "vibration_mm_s" not in z
    # con mu 0 queda el piso absoluto
    assert score.zscores({"engine_rpm": 1e-6}, {"engine_rpm": {"mu": 0.0, "sigma": 0.0}}, None)[
        "engine_rpm"]["self"] == pytest.approx(1.0)


def test_batch_means_uses_only_operating_readings():
    rs = [{"engine_status": "operating", "engine_rpm": 1000}, {"engine_status": "operating", "engine_rpm": 2000},
          {"engine_status": "idle", "engine_rpm": 750}, {"engine_status": "off", "engine_rpm": 0}]
    assert score.batch_means(rs)["engine_rpm"] == 1500.0
    assert score.batch_means([{"engine_status": "idle", "engine_rpm": 750},
                              {"engine_status": "off", "engine_rpm": 0}]) == {}


def test_trend_bonus_counts_three_rising():
    prev = [{"vibration_mm_s": 1.0, "engine_rpm": 5.0}, {"vibration_mm_s": 2.0, "engine_rpm": 4.0}]
    assert score.trend_bonus({"vibration_mm_s": 3.0, "engine_rpm": 6.0}, prev) == 1
    assert score.trend_bonus({"vibration_mm_s": 3.0}, prev[:1]) == 0
    assert score.trend_bonus({"vibration_mm_s": 3.0, "engine_rpm": 6.0}, prev, only={"engine_rpm"}) == 0


@pytest.mark.parametrize("z,sev", [(3.44, "ok"), (3.45, "observe"), (4.25, "schedule"), (5.75, "immediate")])
def test_severity_thresholds_unchanged_on_the_excess(z, sev):
    base = {"vibration_mm_s": {"mu": 0.0, "sigma": 1.0}}
    assert score.compute({"vibration_mm_s": z}, base, None, [])["severity"] == sev


def test_peer_z_can_drive_score():
    self_b = {"vibration_mm_s": {"mu": 5.0, "sigma": 1.0}}
    peer_b = {"vibration_mm_s": {"mu": 1.0, "sigma": 1.0}}
    out = score.compute({"vibration_mm_s": 5.0}, self_b, peer_b, [])
    assert out["z"]["vibration_mm_s"] == {"self": 0.0, "peer": 4.0}
    assert out["score"] == pytest.approx(1.75)


def test_deviated_vars_never_empty_when_an_execution_starts(monkeypatch):
    base = {"vibration_mm_s": {"mu": 0.0, "sigma": 1.0}, "engine_rpm": {"mu": 0.0, "sigma": 1.0}}
    monkeypatch.setattr(score, "DEVIATED_Z", 10.0)  # fuerza la lista vacia por el corte de |z|
    out = score.compute({"vibration_mm_s": 4.5, "engine_rpm": 3.0}, base, None, [])
    assert out["score"] >= score.THRESHOLDS["schedule"]
    assert out["deviated_vars"] == [{"var": "vibration_mm_s", "value": 4.5, "z": pytest.approx(4.5)}]
    # por debajo de schedule no se inventa nada
    assert score.compute({"vibration_mm_s": 3.0}, base, None, [])["deviated_vars"] == []


def dv(**zs):
    return [{"var": k, "value": 0.0, "z": z} for k, z in zs.items()]


def test_failure_mode_hydraulic_pump():
    assert failure_mode.infer(dv(hydraulic_pressure_bar=-3.0, hydraulic_oil_temp_c=2.5)) == "hydraulic_pump"
    assert failure_mode.infer(dv(hydraulic_pressure_bar=-3.0, vibration_mm_s=1.5)) == "hydraulic_pump"


def test_failure_mode_s1_early_crossing_is_hydraulic_pump_not_fuel():
    """Primer cruce real de S1 en un bloque de carga alta (replay del simulador): la presion aun no cae bajo la
    media, pero aceite hidraulico y vibracion suben juntos; el combustible tambien sube por la carga."""
    early = dv(hydraulic_oil_temp_c=2.9, vibration_mm_s=2.6, fuel_rate_lph=2.5, engine_rpm=2.0)
    assert failure_mode.infer(early) == "hydraulic_pump"


def test_failure_mode_fuel_contamination():
    assert failure_mode.infer(dv(fuel_rate_lph=2.0, engine_rpm=-1.5)) == "fuel_contamination"
    assert failure_mode.infer(dv(fuel_rate_lph=2.0, coolant_temp_c=1.5)) == "fuel_contamination"
    assert failure_mode.infer(dv(fuel_rate_lph=2.4, coolant_temp_c=2.1, vibration_mm_s=1.1)) == "fuel_contamination"


def test_failure_mode_single_var_rules_and_fallback():
    assert failure_mode.infer(dv(coolant_temp_c=2.0)) == "engine_overheat"
    assert failure_mode.infer(dv(hydraulic_oil_temp_c=2.0)) == "hydraulic_overheat"
    assert failure_mode.infer(dv(vibration_mm_s=2.0)) == "final_drive"
    assert failure_mode.infer(dv(vibration_mm_s=-2.0)) == "sensor_fault"
    assert failure_mode.infer([]) == "sensor_fault"


def _op(i, status="operating"):
    return {"engine_status": status, **{v: float(i % 5) for v in score.VARS}}


def test_baseline_needs_200_operating_readings():
    assert baselines.MIN_READINGS == 200
    assert baselines.from_readings([_op(i) for i in range(199)]) is None
    b = baselines.from_readings([_op(i) for i in range(200)])
    assert b["engine_rpm"]["mu"] == pytest.approx(2.0)
    assert b["engine_rpm"]["sigma"] > 0
    # idle y off no cuentan ni para el minimo ni para mu/sigma
    others = [{"engine_status": "idle", **{v: 50.0 for v in score.VARS}}] * 30 + \
             [{"engine_status": "off", **{v: 0.0 for v in score.VARS}}] * 30
    assert baselines.from_readings([_op(i) for i in range(150)] + others) is None
    mixed = baselines.from_readings([_op(i) for i in range(200)] + others)
    assert mixed["engine_rpm"]["mu"] == pytest.approx(2.0)


def test_baseline_relative_floor_for_the_live_bootstrap():
    flat = [{"engine_status": "operating", **{v: 100.0 for v in score.VARS}}] * 200
    assert baselines.from_readings(flat)["engine_rpm"]["sigma"] == 0.0
    assert baselines.from_readings(flat, rel_floor=0.05)["engine_rpm"]["sigma"] == pytest.approx(5.0)
