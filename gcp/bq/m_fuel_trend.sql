-- m_fuel_trend (T16): pronostico de consumo diario de combustible por maquina (ARIMA_PLUS).
-- Una serie por machine_id; dia = timestamp, fuel_l = valor. ARIMA_PLUS ajusta un modelo por serie.
-- ASUNCION: ignoramos dias sin operacion (fuel_l NULL) para no sesgar la serie.
CREATE OR REPLACE MODEL `fleetpulse.m_fuel_trend`
OPTIONS(
  model_type = 'ARIMA_PLUS',
  time_series_timestamp_col = 'day',
  time_series_data_col = 'fuel_l',
  time_series_id_col = 'machine_id',
  data_frequency = 'DAILY',
  clean_spikes_and_dips = TRUE,
  decompose_time_series = TRUE
) AS
SELECT
  machine_id,
  day,
  fuel_l
FROM `fleetpulse.v_fuel_daily`
WHERE fuel_l IS NOT NULL;
