#!/usr/bin/env bash
# daily-planning (T18): corrida manual y regla diaria (05:00 Lima) del stack fp-planning-dev.
#   scripts/planning.sh run            # inicia una ejecucion, espera y muestra estado y resumen
#   scripts/planning.sh run --dry-run  # solo calcula: no escribe ordenes ni pedidos
#   scripts/planning.sh on|off|status  # habilita, deshabilita o consulta la regla diaria (via simulator.sh)
# El deploy deja la regla DISABLED y CloudFormation la vuelve a deshabilitar al actualizar el stack.
# Las ordenes llegan a fp-registry por outbox -> registry (segundos); se ven en GET /orders y GET /reorders.
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
STACK="${STACK:-fp-planning-dev}"
action="${1:-}"

case "$action" in
  on|off|status) exec env STACK="$STACK" AWS_REGION="$AWS_REGION" "$(dirname "$0")/simulator.sh" "$action" ;;
  run) ;;
  *) echo "uso: $0 {run [--dry-run]|on|off|status}" >&2; exit 2 ;;
esac

input='{}'
[ "${2:-}" = "--dry-run" ] && input='{"dry_run": true}'
account=$(aws sts get-caller-identity --query Account --output text)
sm="arn:aws:states:${AWS_REGION}:${account}:stateMachine:daily-planning"
name="manual-$(date -u +%Y%m%dT%H%M%S)"
arn=$(aws stepfunctions start-execution --region "$AWS_REGION" --state-machine-arn "$sm" --name "$name" \
      --input "$input" --query executionArn --output text)
echo "ejecucion: $arn"
for _ in $(seq 1 60); do
  status=$(aws stepfunctions describe-execution --region "$AWS_REGION" --execution-arn "$arn" \
           --query status --output text)
  [ "$status" != "RUNNING" ] && break
  sleep 2
done
echo "estado: $status"
aws stepfunctions describe-execution --region "$AWS_REGION" --execution-arn "$arn" --query output --output text
[ "$status" = "SUCCEEDED" ]
