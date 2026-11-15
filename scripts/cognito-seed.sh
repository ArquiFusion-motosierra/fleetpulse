#!/usr/bin/env bash
# Crea (o reutiliza) un usuario de prueba por rol en el user pool de Cognito. Idempotente.
#   FP_TEST_PASSWORD='...' scripts/cognito-seed.sh        # FP_TEST_DOMAIN opcional (default example.com)
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
domain="${FP_TEST_DOMAIN:-example.com}"
if [ -z "${FP_TEST_PASSWORD:-}" ]; then
  echo "FP_TEST_PASSWORD vacia: exportala solo en tu entorno antes de correr este script" >&2
  exit 2
fi

pool=$(aws ssm get-parameter --region "$AWS_REGION" --name /fleetpulse/auth/user_pool_id \
  --query Parameter.Value --output text)

# Los roles los asigna solo un administrador: el app client no puede escribir custom:role/custom:tech_id,
# pero admin-create-user y admin-update-user-attributes (IAM) no pasan por ese filtro.
# usuario:rol[:tech_id]
users=(
  "operador:operator"
  "tecnico:technician:TEC-01"
  "supervisor:supervisor"
  "direccion:management"
  "admin:admin"
)

printf '%-34s %s\n' "USUARIO" "ROL"
for spec in "${users[@]}"; do
  IFS=: read -r name role tech <<<"$spec"
  email="${name}@${domain}"
  attrs=("Name=email,Value=${email}" "Name=email_verified,Value=true" "Name=custom:role,Value=${role}")
  [ -n "${tech:-}" ] && attrs+=("Name=custom:tech_id,Value=${tech}")

  if ! out=$(aws cognito-idp admin-create-user --region "$AWS_REGION" --user-pool-id "$pool" \
      --username "$email" --message-action SUPPRESS --user-attributes "${attrs[@]}" 2>&1); then
    case "$out" in
      *UsernameExistsException*)
        # ya existe: se reafirman los atributos para que el rol quede al dia
        aws cognito-idp admin-update-user-attributes --region "$AWS_REGION" --user-pool-id "$pool" \
          --username "$email" --user-attributes "${attrs[@]}" >/dev/null ;;
      *) echo "$out" >&2; exit 1 ;;
    esac
  fi
  aws cognito-idp admin-set-user-password --region "$AWS_REGION" --user-pool-id "$pool" \
    --username "$email" --password "$FP_TEST_PASSWORD" --permanent
  printf '%-34s %s\n' "$email" "$role${tech:+ ($tech)}"
done
