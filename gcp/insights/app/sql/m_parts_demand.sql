-- m_parts_demand (T20): pronostico de consumo de repuestos por repuesto y zona (ARIMA_PLUS, horizonte 14 dias).
-- Una serie por (part_id, zone_id) sobre v_parts_consumption_daily. Lo lee insights GET /forecast/parts.
CREATE OR REPLACE MODEL `fleetpulse.m_parts_demand`
OPTIONS(
  model_type = 'ARIMA_PLUS',
  time_series_timestamp_col = 'day',
  time_series_data_col = 'qty',
  time_series_id_col = ['part_id', 'zone_id'],
  data_frequency = 'DAILY',
  horizon = 14
) AS
SELECT day, part_id, zone_id, qty
FROM `fleetpulse.v_parts_consumption_daily`;
