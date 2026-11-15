-- Perfil de 30 dias por maquina (T11). Base para m_peer_clusters (KMEANS, T16) y la vista de pares.
CREATE OR REPLACE VIEW `fleetpulse.v_machine_profile_30d` AS
SELECT
  machine_id,
  ANY_VALUE(model) AS model,
  ANY_VALUE(zone_id) AS zone_id,
  COUNT(*) AS readings,
  AVG(IF(engine_status = 'operating', 1, 0)) AS operating_ratio,
  AVG(IF(engine_status = 'idle', 1, 0)) AS idle_ratio,
  AVG(vibration_mm_s) AS avg_vibration_mm_s,
  AVG(coolant_temp_c) AS avg_coolant_temp_c,
  AVG(hydraulic_oil_temp_c) AS avg_hydraulic_oil_temp_c,
  AVG(hydraulic_pressure_bar) AS avg_hydraulic_pressure_bar,
  AVG(engine_rpm) AS avg_engine_rpm,
  AVG(fuel_rate_lph) AS avg_fuel_rate_lph,
  MAX(cumulative_operating_hours) AS hours_total
FROM `fleetpulse.telemetry`
WHERE ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY machine_id;
