#!/usr/bin/env bash
# T12: publica los secretos cruzados entre nubes en AWS Secrets Manager.
# Los NOMBRES salen de contracts/secrets.md (T04); los VALORES de variables de entorno.
# Nunca lee valores de archivos versionados ni los imprime.
#
# Convencion de entorno: el secreto `fp/oci/ords-oauth` se toma de la variable
#   SECRET_fp_oci_ords_oauth  (prefijo SECRET_ + nombre con / y - vueltos _).
#
# Uso:
#   scripts/push-secrets.sh --dry-run   # lista que secretos tocaria, sin escribir
#   scripts/push-secrets.sh             # crea o actualiza cada secreto
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
NAMES_FILE="${NAMES_FILE:-contracts/secrets.md}"
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1

if [ ! -f "$NAMES_FILE" ]; then
  echo "Falta $NAMES_FILE (lo congela T04 con el tag m1-contratos). Nada que publicar." >&2
  exit 1
fi

# Nombres de secretos = tokens `fp/...` entre backticks en secrets.md
mapfile -t NAMES < <(grep -oE '`fp/[A-Za-z0-9/_-]+`' "$NAMES_FILE" | tr -d '`' | sort -u)
if [ "${#NAMES[@]}" -eq 0 ]; then
  echo "secrets.md no define nombres fp/... todavia." >&2
  exit 1
fi

env_var_for() { printf 'SECRET_%s' "$(echo "$1" | tr '/-' '__')"; }

missing=0
for name in "${NAMES[@]}"; do
  var="$(env_var_for "$name")"
  if [ -z "${!var:-}" ]; then
    echo "  FALTA valor: exporta $var para el secreto $name" >&2
    missing=1
  fi
done
[ "$missing" = "1" ] && { echo "Faltan valores en el entorno; aborto sin escribir." >&2; exit 1; }

for name in "${NAMES[@]}"; do
  var="$(env_var_for "$name")"
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
echo "push-secrets: OK (${#NAMES[@]} secretos)"
