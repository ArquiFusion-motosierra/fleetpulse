#!/usr/bin/env bash
# T12 (plan B, sin OCI): publica en AWS Secrets Manager los secretos que listan
# las lineas SECRET_*= de .env.example. La izquierda es la variable de entorno con
# el VALOR; la derecha es el nombre del secreto. Nunca lee valores de archivos
# versionados ni los imprime. Los secretos GCP_SECRET_*/SSM_* no son de aqui.
#
# Uso:
#   scripts/push-secrets.sh --dry-run   # lista que secretos tocaria, sin escribir
#   scripts/push-secrets.sh             # crea o actualiza cada secreto
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
ENV_FILE="${ENV_FILE:-.env.example}"
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1

[ -f "$ENV_FILE" ] || { echo "Falta $ENV_FILE." >&2; exit 1; }

# Pares "VARIABLE nombre_secreto" de cada linea SECRET_*=fleetpulse/...
mapfile -t PAIRS < <(grep -E '^SECRET_[A-Z0-9_]+=' "$ENV_FILE" | sed 's/=/ /')
[ "${#PAIRS[@]}" -gt 0 ] || { echo "$ENV_FILE no define lineas SECRET_*=" >&2; exit 1; }

missing=0
for pair in "${PAIRS[@]}"; do
  var="${pair%% *}"
  if [ -z "${!var:-}" ]; then
    echo "  FALTA valor: exporta $var (secreto ${pair#* })" >&2
    missing=1
  fi
done
[ "$missing" = "1" ] && { echo "Faltan valores en el entorno; aborto sin escribir." >&2; exit 1; }

for pair in "${PAIRS[@]}"; do
  var="${pair%% *}"; name="${pair#* }"
  if [ "$DRY" = "1" ]; then
    echo "  DRY put $name  (desde \$$var)"
    continue
  fi
  if aws secretsmanager describe-secret --secret-id "$name" --region "$AWS_REGION" >/dev/null 2>&1; then
    aws secretsmanager put-secret-value --secret-id "$name" \
      --secret-string "${!var}" --region "$AWS_REGION" >/dev/null
    echo "  actualizado $name"
  else
    aws secretsmanager create-secret --name "$name" \
      --secret-string "${!var}" --region "$AWS_REGION" >/dev/null
    echo "  creado $name"
  fi
done
echo "push-secrets: OK (${#PAIRS[@]} secretos)"
