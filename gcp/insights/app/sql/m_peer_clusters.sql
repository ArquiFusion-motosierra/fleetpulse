-- m_peer_clusters (T16): agrupa maquinas por su perfil de 30 dias (KMEANS).
-- El modelo de maquina entra como feature categorica, asi los clusters quedan dentro de cada tipo.
-- machine_id se excluye del entrenamiento (es identificador, no feature).
CREATE OR REPLACE MODEL `fleetpulse.m_peer_clusters`
OPTIONS(
  model_type = 'KMEANS',
  num_clusters = 5,
  standardize_features = TRUE,
  kmeans_init_method = 'KMEANS++'
) AS
SELECT
  model,
  operating_ratio,
  idle_ratio,
  avg_vibration_mm_s,
  avg_coolant_temp_c,
  avg_hydraulic_oil_temp_c,
  avg_hydraulic_pressure_bar,
  avg_engine_rpm,
  avg_fuel_rate_lph
FROM `fleetpulse.v_machine_profile_30d`;
