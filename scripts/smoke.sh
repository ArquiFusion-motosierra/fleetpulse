#!/usr/bin/env bash
# Smoke test por etapas. Cada subcomando se completa en la tarea que lo exige.
#   scripts/smoke.sh ingest     # T05: item en DynamoDB fp-state y NDJSON en S3 fp-raw
#   scripts/smoke.sh all        # T12: todo lo anterior en verde
set -euo pipefail
case "${1:-all}" in
  ingest) echo "TODO T05: POST telemetry-batch al API y verificar fp-state + fp-raw" ; exit 1 ;;
  all)    "$0" ingest ;;
  *)      echo "uso: $0 {ingest|all}" >&2 ; exit 2 ;;
esac
