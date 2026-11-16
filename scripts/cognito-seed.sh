#!/usr/bin/env bash
# Crea (o reutiliza) los usuarios de prueba por parque (tenant) y rol en el user pool de Cognito. Idempotente.
#   FP_TEST_PASSWORD='...' scripts/cognito-seed.sh        # FP_TEST_DOMAIN opcional (default example.com)
# Lo corre una persona del equipo con credenciales del Learner Lab, nunca CI.
#
# Multi-tenancy: el parque va en el grupo de Cognito
# `tenant-<id>` (los crea el stack fp-auth); el jefe de mantenimiento va en `maintenance-head` y ve todos los parques.
# Los 5 usuarios originales (operador@, tecnico@, ...) quedan en tenant-mineria: siguen viendo lo mismo que antes.
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
# pero admin-create-user y admin-update-user-attributes (IAM) no pasan por ese filtro. Lo mismo para los grupos.
# usuario:rol:grupo[:tech_id]
users=(
  "operador:operator:tenant-mineria"
  "tecnico:technician:tenant-mineria:TEC-01"
  "supervisor:supervisor:tenant-mineria"
  "direccion:management:tenant-mineria"
  "admin:admin:tenant-mineria"
  "admin.mineria:admin:tenant-mineria"
  "operador.agro:operator:tenant-agro"
  "tecnico.agro:technician:tenant-agro:TEC-11"
  "supervisor.agro:supervisor:tenant-agro"
  "direccion.agro:management:tenant-agro"
  "admin.agro:admin:tenant-agro"
  "operador.industria:operator:tenant-industria"
  "tecnico.industria:technician:tenant-industria:TEC-21"
  "supervisor.industria:supervisor:tenant-industria"
  "direccion.industria:management:tenant-industria"
  "admin.industria:admin:tenant-industria"
  "jefe.mantenimiento:maintenance_head:maintenance-head"
)

printf '%-40s %-18s %s\n' "USUARIO" "GRUPO" "ROL"
for spec in "${users[@]}"; do
  IFS=: read -r name role group tech <<<"$spec"
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
  # un usuario de parque esta en un solo grupo de parque: se lo saca de los otros (idempotente)
  for other in tenant-mineria tenant-agro tenant-industria maintenance-head; do
    [ "$other" = "$group" ] && continue
    aws cognito-idp admin-remove-user-from-group --region "$AWS_REGION" --user-pool-id "$pool" \
      --username "$email" --group-name "$other" >/dev/null 2>&1 || true
  done
  aws cognito-idp admin-add-user-to-group --region "$AWS_REGION" --user-pool-id "$pool" \
    --username "$email" --group-name "$group"
  printf '%-40s %-18s %s\n' "$email" "$group" "$role${tech:+ ($tech)}"
  tech=""
done
echo "Los grupos entran en el ID token al iniciar sesion de nuevo (cerrar sesion en la consola y volver a entrar)."
