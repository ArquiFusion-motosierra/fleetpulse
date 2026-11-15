#!/usr/bin/env bash
# Entrena/actualiza los modelos BigQuery ML (T16). Requiere `bq` autenticado y las vistas ya aplicadas
# (corre antes gcp/bq/apply_views.sh). maximum_bytes_billed limita el gasto para quedar en free tier.
#   gcp/bq/apply_models.sh
set -euo pipefail
cd "$(dirname "$0")"

PROJECT="${GCP_PROJECT_ID:-fleetpulse-g19-2026}"
MAX_BYTES="${BQ_MAX_BYTES:-2000000000}"  # 2 GB tope por job: los modelos entrenan sobre tablas/vistas chicas (free tier)

# m_parts_demand (T20) sale de incident.closed en domain_events (v_parts_consumption_daily). Lo mismo, sin `bq`:
# insights POST /jobs/refresh (copias en gcp/insights/app/sql/).

for sql in m_peer_clusters.sql m_fuel_trend.sql m_parts_demand.sql; do
  echo "entrenando $sql"
  bq query --project_id "$PROJECT" --use_legacy_sql=false --nouse_cache \
    --maximum_bytes_billed "$MAX_BYTES" < "$sql"
done
echo "modelos listos"
