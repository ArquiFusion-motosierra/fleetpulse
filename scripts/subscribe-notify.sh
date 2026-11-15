#!/usr/bin/env bash
# Suscribe un correo al topic fp-notify filtrando por rol.
#   scripts/subscribe-notify.sh <technician|supervisor|management|operator> <email>
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
role="${1:-}"; email="${2:-}"
case "$role" in
  technician|supervisor|management|operator) ;;
  *) echo "uso: $0 {technician|supervisor|management|operator} <email>" >&2; exit 2 ;;
esac
[ -n "$email" ] || { echo "uso: $0 $role <email>" >&2; exit 2; }

acct=$(aws sts get-caller-identity --query Account --output text)
aws sns subscribe \
  --region "$AWS_REGION" \
  --topic-arn "arn:aws:sns:${AWS_REGION}:${acct}:fp-notify" \
  --protocol email \
  --notification-endpoint "$email" \
  --attributes "{\"FilterPolicy\":\"{\\\"role\\\":[\\\"${role}\\\"]}\"}" \
  --output text >/dev/null
echo "Suscripcion creada (rol=$role). Falta confirmar: abre el correo de AWS enviado a $email y pulsa 'Confirm subscription'."
