-- Estado actual de cada orden de compra / transferencia (T42), desde domain_events.
-- Hay un evento supply.* por etapa; se agrega por aggregate_id (el id de la PO/TRF).
-- ASUNCION (diseno repuestos 4.4): payload lleva tenant_id, zone_id, from_zone, part_id, qty,
--   cost_usd, downtime_avoided_usd; aggregate_type in ('purchase_order','transfer'). Se ajusta si T37
--   congela otra forma. tenant_id -> 'mineria' mientras T36 no agregue la columna top-level.
-- Filtra por occurred_at (particion). Queda valida aunque los supply.* aun no fluyan (devuelve 0 filas).
CREATE OR REPLACE VIEW `fleetpulse.v_supply_orders` AS
WITH ev AS (
  SELECT
    aggregate_id AS order_id,
    aggregate_type AS kind,
    event_type,
    occurred_at,
    incident_id,
    COALESCE(JSON_VALUE(payload, '$.tenant_id'), 'mineria') AS tenant_id,
    JSON_VALUE(payload, '$.zone_id') AS zone_id,
    JSON_VALUE(payload, '$.from_zone') AS from_zone,
    JSON_VALUE(payload, '$.part_id') AS part_id,
    SAFE_CAST(JSON_VALUE(payload, '$.qty') AS INT64) AS qty,
    SAFE_CAST(JSON_VALUE(payload, '$.cost_usd') AS FLOAT64) AS cost_usd,
    SAFE_CAST(JSON_VALUE(payload, '$.downtime_avoided_usd') AS FLOAT64) AS downtime_avoided_usd
  FROM `fleetpulse.domain_events`
  WHERE occurred_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)
    AND aggregate_type IN ('purchase_order', 'transfer')
),
agg AS (
  SELECT
    order_id,
    ANY_VALUE(kind) AS kind,
    ANY_VALUE(tenant_id) AS tenant_id,
    MAX(part_id) AS part_id,
    MAX(zone_id) AS zone_id,
    MAX(from_zone) AS from_zone,
    MAX(qty) AS qty,
    MAX(incident_id) AS incident_id,
    MAX(cost_usd) AS cost_usd,
    MAX(downtime_avoided_usd) AS downtime_avoided_usd,
    MIN(IF(event_type = 'supply.suggested', occurred_at, NULL)) AS suggested_at,
    MIN(IF(event_type = 'supply.approved', occurred_at, NULL)) AS approved_at,
    MIN(IF(event_type IN ('po.ordered', 'transfer.shipped'), occurred_at, NULL)) AS dispatched_at,
    MIN(IF(event_type = 'supply.received', occurred_at, NULL)) AS received_at,
    MIN(IF(event_type = 'supply.cancelled', occurred_at, NULL)) AS cancelled_at
  FROM ev
  GROUP BY order_id
)
SELECT
  order_id, kind, tenant_id, part_id, zone_id, from_zone, qty, incident_id,
  cost_usd, downtime_avoided_usd,
  suggested_at, approved_at, dispatched_at, received_at, cancelled_at,
  CASE
    WHEN received_at IS NOT NULL THEN 'received'
    WHEN cancelled_at IS NOT NULL THEN 'cancelled'
    WHEN dispatched_at IS NOT NULL THEN IF(kind = 'transfer', 'in_transit', 'ordered')
    WHEN approved_at IS NOT NULL THEN 'approved'
    ELSE 'suggested'
  END AS status,
  TIMESTAMP_DIFF(received_at, suggested_at, HOUR) AS lead_hours
FROM agg;
