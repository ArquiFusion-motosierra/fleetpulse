#!/usr/bin/env bash
# Crea/actualiza las vistas base en BigQuery (T11). Requiere `bq` autenticado.
#   gcp/bq/apply_views.sh
set -euo pipefail
cd "$(dirname "$0")"

PROJECT="${GCP_PROJECT_ID:-fleetpulse-g19-2026}"

for sql in v_fuel_daily.sql v_machine_profile_30d.sql v_parts_consumption_daily.sql v_supply_orders.sql v_stockouts_daily.sql; do
  echo "aplicando $sql"
  bq query --project_id "$PROJECT" --use_legacy_sql=false --nouse_cache < "$sql"
done
echo "vistas listas"
