#!/usr/bin/env bash
# Carga las credenciales de sesion del AWS Learner Lab.
#
#   scripts/lab-creds.sh            # imprime exports para `eval "$(scripts/lab-creds.sh)"`
#   scripts/lab-creds.sh --gh       # ademas las sube como secrets del repo para deploy-aws.yml
#
# Pega antes el bloque "AWS CLI" del Learner Lab (AWS Details -> AWS CLI -> Show) en ~/.aws/credentials
# bajo el perfil [default]. Caducan cada sesion (~4 h): volver a correr este script.
set -euo pipefail
REPO="${REPO:-ArquiFusion-motosierra/fleetpulse}"
PROFILE="${AWS_PROFILE:-default}"

key=$(aws configure get aws_access_key_id --profile "$PROFILE")
secret=$(aws configure get aws_secret_access_key --profile "$PROFILE")
token=$(aws configure get aws_session_token --profile "$PROFILE")
if [ -z "$key" ] || [ -z "$token" ]; then
  echo "No hay credenciales de Learner Lab en el perfil '$PROFILE' de ~/.aws/credentials" >&2
  exit 1
fi

echo "export AWS_ACCESS_KEY_ID=$key"
echo "export AWS_SECRET_ACCESS_KEY=$secret"
echo "export AWS_SESSION_TOKEN=$token"
echo "export AWS_REGION=us-east-1"

if [ "${1:-}" = "--gh" ]; then
  gh secret set AWS_ACCESS_KEY_ID     --repo "$REPO" --body "$key"
  gh secret set AWS_SECRET_ACCESS_KEY --repo "$REPO" --body "$secret"
  gh secret set AWS_SESSION_TOKEN     --repo "$REPO" --body "$token"
  echo "# Secrets actualizados en $REPO" >&2
fi
