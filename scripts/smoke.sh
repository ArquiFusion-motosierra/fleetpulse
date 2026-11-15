#!/usr/bin/env bash
# Smoke test por etapas (T12). Cada etapa de un frente la completa su tarea;
# las etapas de integracion (secrets, oci, gcp, e2e) son de T12.
#   scripts/smoke.sh ingest   # T05: item en DynamoDB fp-state y NDJSON en S3 fp-raw
#   scripts/smoke.sh secrets  # T12: los secretos cruzados existen en Secrets Manager
#   scripts/smoke.sh oci      # T10: un evento llega a OCI y no se duplica
#   scripts/smoke.sh gcp      # T11: filas en BigQuery via load job
#   scripts/smoke.sh e2e      # T17: mismo incident_id en las 3 nubes
#   scripts/smoke.sh all      # T12: todo lo anterior en verde
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
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
  local names_file="contracts/secrets.md"
  if [ ! -f "$names_file" ]; then
    skip "secrets (falta $names_file, T04)"; return 0
  fi
  # Nombres de secretos = lineas con backticks `fp/...` en secrets.md
  local names
  names=$(grep -oE '`fp/[A-Za-z0-9/_-]+`' "$names_file" | tr -d '`' | sort -u)
  [ -z "$names" ] && { skip "secrets (secrets.md sin nombres fp/...)"; return 0; }
  local rc=0 n
  for n in $names; do
    if aws secretsmanager describe-secret --secret-id "$n" --region "$AWS_REGION" >/dev/null 2>&1; then
      ok "secret $n"
    else
      fail "secret $n no existe (corre push-secrets.sh)" || rc=1
    fi
  done
  return $rc
}

stage_oci() {
  echo "[oci] T10"
  [ "$SKIP_MISSING" = "1" ] && { skip "oci"; return 0; }
  echo "  TODO T10: enviar un evento por oci-writer y verificar idempotencia en PROCESSED_EVENTS"
  return 1
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
  echo "  TODO T17: mismo incident_id visible en Step Functions, BigQuery y APEX"
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
  oci)     run oci ;;
  gcp)     run gcp ;;
  e2e)     run e2e ;;
  all)     run ingest secrets oci gcp e2e ;;
  *)       echo "uso: $0 {ingest|secrets|oci|gcp|e2e|all}" >&2 ; exit 2 ;;
esac
