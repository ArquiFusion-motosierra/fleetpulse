-- Consumo diario de repuestos por zona (T20). Base para m_parts_demand (ARIMA_PLUS).
-- Sale de incident.closed (payload.parts_used) en domain_events; dia en hora de Lima. Los dias sin consumo van en 0
-- (ARIMA_PLUS interpola los huecos, y para una demanda eso inventaria consumo).
-- Filtra por occurred_at (columna de particion) para cumplir require_partition_filter.
CREATE OR REPLACE VIEW `fleetpulse.v_parts_consumption_daily` AS
WITH used AS (
  SELECT
    DATE(occurred_at, 'America/Lima') AS day,
    JSON_VALUE(p, '$.part_id') AS part_id,
    JSON_VALUE(p, '$.zone_id') AS zone_id,
    SAFE_CAST(JSON_VALUE(p, '$.qty') AS INT64) AS qty
  FROM `fleetpulse.domain_events`, UNNEST(JSON_QUERY_ARRAY(payload, '$.parts_used')) AS p
  WHERE event_type = 'incident.closed'
    AND occurred_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)
),
series AS (SELECT DISTINCT part_id, zone_id FROM used WHERE part_id IS NOT NULL AND zone_id IS NOT NULL),
days AS (
  SELECT day FROM UNNEST(GENERATE_DATE_ARRAY(
    DATE_SUB(CURRENT_DATE('America/Lima'), INTERVAL 89 DAY), CURRENT_DATE('America/Lima'))) AS day
)
SELECT d.day, s.part_id, s.zone_id, COALESCE(SUM(u.qty), 0) AS qty
FROM series s CROSS JOIN days d
LEFT JOIN used u ON u.part_id = s.part_id AND u.zone_id = s.zone_id AND u.day = d.day
GROUP BY d.day, s.part_id, s.zone_id;
