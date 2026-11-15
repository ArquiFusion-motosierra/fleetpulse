-- fuel_daily (fix backlog#45): materializacion de v_fuel_daily como tabla chica (~124 maquinas x 90 dias).
-- Por que: m_fuel_trend (ARIMA_PLUS) re-escanea su entrada varias veces durante el ajuste. Sobre la VISTA
-- v_fuel_daily eso re-escanea toda `telemetry` (~3,5M filas) en cada pasada y supera el tope de bytes del
-- proyecto. Sobre esta tabla (~11k filas) el escaneo es minimo y el entrenamiento queda dentro del free tier.
-- No viola la regla del free tier: es un CREATE TABLE AS SELECT puntual, no streaming (CLAUDE.md prohibe
-- inserciones streaming, no la materializacion). La refresca apply_views.sh antes de entrenar los modelos.
CREATE OR REPLACE TABLE `fleetpulse.fuel_daily` AS
SELECT * FROM `fleetpulse.v_fuel_daily`;
