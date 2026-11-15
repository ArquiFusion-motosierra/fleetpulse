-- Quiebres de stock por dia, parque y repuesto (T42): part.shortage en domain_events,
-- con el tiempo hasta resolverse (supply.received del mismo incident_id).
-- ASUNCION: part.shortage.payload.missing[] = [{part_id, qty}] (contracts/part.schema.json) y
--   payload.zone_id opcional (lo agrega el diseno de repuestos); tenant_id -> 'mineria' hasta T36.
-- Filtra por occurred_at (particion). Valida aunque aun no haya supply.received.
CREATE OR REPLACE VIEW `fleetpulse.v_stockouts_daily` AS
WITH shortages AS (
  SELECT
    incident_id,
    DATE(occurred_at, 'America/Lima') AS day,
    occurred_at AS shortage_at,
    COALESCE(JSON_VALUE(payload, '$.tenant_id'), 'mineria') AS tenant_id,
    JSON_VALUE(payload, '$.zone_id') AS zone_id,
    JSON_VALUE(m, '$.part_id') AS part_id
  FROM `fleetpulse.domain_events`, UNNEST(JSON_QUERY_ARRAY(payload, '$.missing')) AS m
  WHERE event_type = 'part.shortage'
    AND occurred_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)
),
received AS (
  SELECT incident_id, MIN(occurred_at) AS received_at
  FROM `fleetpulse.domain_events`
  WHERE event_type = 'supply.received'
    AND occurred_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)
    AND incident_id IS NOT NULL
  GROUP BY incident_id
)
SELECT
  s.day, s.tenant_id, s.part_id, s.zone_id,
  COUNT(*) AS shortages,
  COUNTIF(r.received_at IS NOT NULL) AS resolved,
  ROUND(AVG(TIMESTAMP_DIFF(r.received_at, s.shortage_at, HOUR)), 1) AS avg_resolution_hours
FROM shortages s
LEFT JOIN received r USING (incident_id)
WHERE s.part_id IS NOT NULL
GROUP BY s.day, s.tenant_id, s.part_id, s.zone_id;
