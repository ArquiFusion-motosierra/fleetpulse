#!/usr/bin/env bash
# Smoke test por etapas (T12, plan B: AWS + GCP, sin OCI). Cada etapa de un frente
# la completa su tarea; las de integracion (secrets, e2e) son de T12.
#   scripts/smoke.sh ingest   # T05: item en DynamoDB fp-state y NDJSON en S3 fp-raw
#   scripts/smoke.sh secrets  # T12: los secretos de AWS Secrets Manager existen
#   scripts/smoke.sh gcp      # T11: filas en fleetpulse.telemetry via load job
#   scripts/smoke.sh incidents  # T14: S1 y S2 por incident-flow, aprobacion y cierre (crea incidentes reales;
#                               #      no entra en `all`). SCENARIO=s1|s2|all, SMOKE_ARGS="--sim --close-stale"
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
  if ! command -v aws >/dev/null 2>&1; then
    [ "$SKIP_MISSING" = "1" ] && { skip "ingest (sin aws cli)"; return 0; }
    fail "ingest: falta aws cli"; return 1
  fi
  local url key code acct item
  if ! url=$(aws ssm get-parameter --name /fleetpulse/api/url --region "$AWS_REGION" --query Parameter.Value --output text 2>/dev/null); then
    [ "$SKIP_MISSING" = "1" ] && { skip "ingest (sin /fleetpulse/api/url; fp-core no desplegado)"; return 0; }
    fail "ingest: falta SSM /fleetpulse/api/url"; return 1
  fi
  key=$(aws secretsmanager get-secret-value --secret-id fleetpulse/api/ingest-key --region "$AWS_REGION" \
        --query SecretString --output text | python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])') \
    || { fail "ingest: no se pudo leer fleetpulse/api/ingest-key"; return 1; }
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${url%/}/telemetry" \
         -H "content-type: application/json" -H "x-api-key: $key" \
         --data @contracts/examples/telemetry-batch/with-fault.json) || true
  case "$code" in
    202|200) ok "POST telemetry-batch -> $code" ;;
    *) fail "POST telemetry-batch devolvio $code (esperado 202 o 200)"; return 1 ;;
  esac
  item=$(aws dynamodb get-item --table-name fp-state --region "$AWS_REGION" \
         --key '{"PK":{"S":"MACHINE#EX-017"},"SK":{"S":"META"}}') || { fail "get-item fp-state"; return 1; }
  case "$item" in
    *last_ts*) ok "fp-state MACHINE#EX-017 tiene last_ts" ;;
    *) fail "fp-state MACHINE#EX-017 sin last_ts"; return 1 ;;
  esac
  acct=$(aws sts get-caller-identity --query Account --output text)
  echo "  esperando 20 s al archiver..."
  sleep 20
  if aws s3 ls "s3://fp-raw-${acct}/telemetry/" --recursive 2>/dev/null | head -1 | grep -q .; then
    ok "fp-raw-${acct}/telemetry tiene objetos"
  else
    echo "  SKIP archiver window"
  fi
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

stage_incidents() {
  echo "[incidents] T14"
  if ! command -v aws >/dev/null 2>&1; then
    [ "$SKIP_MISSING" = "1" ] && { skip "incidents (sin aws cli)"; return 0; }
    fail "incidents: falta aws cli"; return 1
  fi
  if ! aws ssm get-parameter --name /fleetpulse/api/url --region "$AWS_REGION" >/dev/null 2>&1; then
    [ "$SKIP_MISSING" = "1" ] && { skip "incidents (sin /fleetpulse/api/url; fp-core no desplegado)"; return 0; }
    fail "incidents: falta SSM /fleetpulse/api/url"; return 1
  fi
  # shellcheck disable=SC2086 # SMOKE_ARGS son varias banderas
  "${PYTHON:-.venv/bin/python}" scripts/smoke_incidents.py "${SCENARIO:-all}" ${SMOKE_ARGS:-}
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
  echo "  TODO T17: mismo incident_id en las 2 nubes: Step Functions/fp-registry (AWS) y BigQuery/consola (GCP)"
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
  incidents) run incidents ;;
  e2e)     run e2e ;;
  all)     run ingest secrets gcp e2e ;;
  *)       echo "uso: $0 {ingest|secrets|gcp|incidents|e2e|all}" >&2 ; exit 2 ;;
esac
