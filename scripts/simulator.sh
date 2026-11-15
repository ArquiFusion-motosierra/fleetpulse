#!/usr/bin/env bash
# Enciende, apaga o consulta el simulador: la regla de EventBridge (rate 1 minute) del stack fp-simulator-dev.
#   scripts/simulator.sh on       # habilita la regla: un lote por minuto a POST /telemetry
#   scripts/simulator.sh off      # la deshabilita (costo)
#   scripts/simulator.sh status   # nombre, estado y expresion de cada regla
# STACK=<otro-stack> para otra etapa. No toca la plantilla: el deploy deja `enabled: false`, y cuando
# CloudFormation actualiza la regla la vuelve a deshabilitar. Corre `status` despues de cualquier deploy.
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
STACK="${STACK:-fp-simulator-dev}"
action="${1:-}"
case "$action" in
  on|off|status) ;;
  *) echo "uso: $0 {on|off|status}" >&2; exit 2 ;;
esac

ids=$(aws cloudformation describe-stack-resources --region "$AWS_REGION" --stack-name "$STACK" \
      --query "StackResources[?ResourceType=='AWS::Events::Rule'].PhysicalResourceId" --output text)
if [ -z "$ids" ] || [ "$ids" = "None" ]; then
  echo "El stack $STACK no tiene reglas de EventBridge (desplegaste el simulador?)" >&2
  exit 1
fi

for id in $ids; do
  # Bus por defecto: el id fisico es el nombre de la regla; en un bus propio es "<bus>|<nombre>"
  bus=(); name="$id"
  if [ "${id#*|}" != "$id" ]; then bus=(--event-bus-name "${id%%|*}"); name="${id#*|}"; fi
  case "$action" in
    on)  aws events enable-rule  --region "$AWS_REGION" --name "$name" ${bus[@]+"${bus[@]}"} ;;
    off) aws events disable-rule --region "$AWS_REGION" --name "$name" ${bus[@]+"${bus[@]}"} ;;
  esac
  aws events describe-rule --region "$AWS_REGION" --name "$name" ${bus[@]+"${bus[@]}"} \
    --query '[Name, State, ScheduleExpression]' --output text
done
