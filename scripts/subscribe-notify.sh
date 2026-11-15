#!/usr/bin/env bash
# Suscribe un correo al topic fp-notify filtrando por rol y por parque (tenant).
#   scripts/subscribe-notify.sh <technician|supervisor|management|operator> <email> [mineria|agro|industria|all]
# Sin parque: mineria (compatible con las suscripciones creadas antes del multi-tenancy). `all` = jefe de
# mantenimiento: recibe los avisos de ese rol de todos los parques.
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
role="${1:-}"; email="${2:-}"; tenant="${3:-mineria}"
case "$role" in
  technician|supervisor|management|operator) ;;
  *) echo "uso: $0 {technician|supervisor|management|operator} <email> [mineria|agro|industria|all]" >&2; exit 2 ;;
esac
[ -n "$email" ] || { echo "uso: $0 $role <email> [tenant]" >&2; exit 2; }
case "$tenant" in
  mineria|agro|industria) policy="{\"role\":[\"${role}\"],\"tenant_id\":[\"${tenant}\"]}" ;;
  all) policy="{\"role\":[\"${role}\"]}" ;;
  *) echo "parque desconocido: $tenant (mineria|agro|industria|all)" >&2; exit 2 ;;
esac

acct=$(aws sts get-caller-identity --query Account --output text)
attrs=$(python3 -c 'import json,sys; print(json.dumps({"FilterPolicy": sys.argv[1]}))' "$policy")
aws sns subscribe \
  --region "$AWS_REGION" \
  --topic-arn "arn:aws:sns:${AWS_REGION}:${acct}:fp-notify" \
  --protocol email \
  --notification-endpoint "$email" \
  --attributes "$attrs" \
  --output text >/dev/null
echo "Suscripcion creada (rol=$role, parque=$tenant). Falta confirmar: abre el correo de AWS enviado a $email y pulsa 'Confirm subscription'."
