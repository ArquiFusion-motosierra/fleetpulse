-- Consumo de combustible por maquina y dia (T11). Base para m_fuel_trend (ARIMA_PLUS, T16).
-- Los acumulados son monotonos por maquina, asi que el gasto del dia = max - min.
-- Filtra por ts (columna de particion) para cumplir require_partition_filter.
CREATE OR REPLACE VIEW `fleetpulse.v_fuel_daily` AS
SELECT
  machine_id,
  DATE(ts) AS day,
  ANY_VALUE(model) AS model,
  ANY_VALUE(zone_id) AS zone_id,
  MAX(cumulative_fuel_used_l) - MIN(cumulative_fuel_used_l) AS fuel_l,
  MAX(cumulative_operating_hours) - MIN(cumulative_operating_hours) AS operating_h,
  SAFE_DIVIDE(
    MAX(cumulative_fuel_used_l) - MIN(cumulative_fuel_used_l),
    NULLIF(MAX(cumulative_operating_hours) - MIN(cumulative_operating_hours), 0)
  ) AS fuel_lph,
  AVG(fuel_rate_lph) AS avg_fuel_rate_lph
FROM `fleetpulse.telemetry`
WHERE ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)
GROUP BY machine_id, day;
