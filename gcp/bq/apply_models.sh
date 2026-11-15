#!/usr/bin/env bash
# Entrena/actualiza los modelos BigQuery ML (T16). Requiere `bq` autenticado y las vistas ya aplicadas
# (corre antes gcp/bq/apply_views.sh). maximum_bytes_billed limita el gasto para quedar en free tier.
#   gcp/bq/apply_models.sh
set -euo pipefail
cd "$(dirname "$0")"

PROJECT="${GCP_PROJECT_ID:-fleetpulse-g19-2026}"
MAX_BYTES="${BQ_MAX_BYTES:-2000000000}"  # 2 GB tope por job

# m_parts_demand (ARIMA_PLUS) se OMITE: no hay aun una vista/fuente clara de demanda de repuestos
# (el consumo real sale de incident.closed -> fp-registry en AWS). Se agregara cuando exista esa vista.

for sql in m_peer_clusters.sql m_fuel_trend.sql; do
  echo "entrenando $sql"
  bq query --project_id "$PROJECT" --use_legacy_sql=false --nouse_cache \
    --maximum_bytes_billed "$MAX_BYTES" < "$sql"
done
echo "modelos listos"
