#!/usr/bin/env bash
# Smoke test por etapas (T12, plan B: AWS + GCP, sin OCI). Cada etapa de un frente
# la completa su tarea; las de integracion (secrets, e2e) son de T12.
#   scripts/smoke.sh ingest   # T05: item en DynamoDB fp-state y NDJSON en S3 fp-raw
#   scripts/smoke.sh secrets  # T12: los secretos de AWS Secrets Manager existen
#   scripts/smoke.sh gcp      # T11: filas en fleetpulse.telemetry via load job
#   scripts/smoke.sh e2e      # T17: mismo incident_id en AWS y GCP
#   scripts/smoke.sh all      # T12: todo lo anterior en verde
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
ENV_FILE="${ENV_FILE:-.env.example}"
SKIP_MISSING="${SKIP_MISSING:-1}"  # 1 = saltar etapa cuyo frente aun no aterriza, en vez de fallar

ok()   { echo "  OK   $*"; }
skip() { echo "  SKIP $* (frente aun no listo; SKIP_MISSING=0 para exigirlo)"; }
fail() { echo "  FAIL $*" >&2; return 1; }

stage_ingest() {
  echo "[ingest] T05"
  echo "  TODO T05: POST telemetry-batch al API y verificar fp-state + fp-raw"
  [ "$SKIP_MISSING" = "1" ] && { skip "ingest"; return 0; }
  return 1
}

stage_secrets() {
  echo "[secrets] T12"
  if ! command -v aws >/dev/null 2>&1; then skip "secrets (sin aws cli)"; return 0; fi
  [ -f "$ENV_FILE" ] || { skip "secrets (falta $ENV_FILE)"; return 0; }
  # Nombres de secretos de AWS = lado derecho de cada linea SECRET_*= en .env.example
  local names
  names=$(grep -E '^SECRET_[A-Z0-9_]+=' "$ENV_FILE" | sed 's/^[^=]*=//')
  [ -z "$names" ] && { skip "secrets ($ENV_FILE sin lineas SECRET_*=)"; return 0; }
  local n missing=""
  for n in $names; do
    if aws secretsmanager describe-secret --secret-id "$n" --region "$AWS_REGION" >/dev/null 2>&1; then
      ok "secret $n"
    else
      missing="$missing $n"
    fi
  done
  [ -z "$missing" ] && return 0
  [ "$SKIP_MISSING" = "1" ] && { skip "secrets (faltan:$missing)"; return 0; }
  fail "faltan secretos:$missing (corre push-secrets.sh)"
}

stage_gcp() {
  echo "[gcp] T11"
  [ "$SKIP_MISSING" = "1" ] && { skip "gcp"; return 0; }
  echo "  TODO T11: verificar filas en fleetpulse.telemetry via load job (nunca streaming)"
  return 1
}

stage_e2e() {
  echo "[e2e] T17"
  [ "$SKIP_MISSING" = "1" ] && { skip "e2e"; return 0; }
  echo "  TODO T17: mismo incident_id en Step Functions/fp-registry (AWS) y BigQuery/consola (GCP)"
  return 1
}

run() {
  local rc=0
  for s in "$@"; do
    "stage_$s" || rc=1
  done
  [ "$rc" = "0" ] && echo "smoke: VERDE" || echo "smoke: ROJO" >&2
  return $rc
}

case "${1:-all}" in
  ingest)  run ingest ;;
  secrets) run secrets ;;
  gcp)     run gcp ;;
  e2e)     run e2e ;;
  all)     run ingest secrets gcp e2e ;;
  *)       echo "uso: $0 {ingest|secrets|gcp|e2e|all}" >&2 ; exit 2 ;;
esac
