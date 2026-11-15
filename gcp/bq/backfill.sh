#!/usr/bin/env bash
# Carga el backfill del simulador (~2,9 M lecturas) a BigQuery fleetpulse.telemetry (T11).
# El simulador genera NDJSON en simulator/out/telemetry/. Aqui se sube a GCS fp-landing
# y se carga con un load job (nunca streaming).
#
#   python simulator/backfill.py --days 30 --step 15m   # genera simulator/out/
#   gcp/bq/backfill.sh
set -euo pipefail

PROJECT="${GCP_PROJECT_ID:-fleetpulse-g19-2026}"
SUFFIX="${GCP_SUFFIX:-g19-2026}"
BUCKET="${LANDING_BUCKET:-fp-landing-${SUFFIX}}"
OUT="${SIM_OUT:-simulator/out/telemetry}"
SCHEMA="gcp/terraform/schemas/telemetry.json"

[ -d "$OUT" ] || { echo "No existe $OUT; corre primero simulator/backfill.py" >&2; exit 1; }

echo "subiendo $OUT a gs://$BUCKET/backfill/"
gcloud storage cp -r "$OUT"/* "gs://$BUCKET/backfill/"

echo "cargando a $PROJECT:fleetpulse.telemetry"
bq load \
  --project_id "$PROJECT" \
  --source_format=NEWLINE_DELIMITED_JSON \
  --ignore_unknown_values \
  --replace=false \
  "fleetpulse.telemetry" \
  "gs://$BUCKET/backfill/*" \
  "$SCHEMA"

echo "filas en telemetry:"
bq query --project_id "$PROJECT" --use_legacy_sql=false \
  'SELECT COUNT(*) AS rows FROM `fleetpulse.telemetry` WHERE ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)'
