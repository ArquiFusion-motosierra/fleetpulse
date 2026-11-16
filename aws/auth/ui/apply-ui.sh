#!/usr/bin/env bash
# Aplica el branding de FleetPulse a la Hosted UI clasica de Cognito (stack fp-auth).
# Lee el pool y el client de SSM (los publica aws/auth/serverless.yml). Requiere credenciales AWS
# del Learner Lab activas (scripts/lab-creds.sh) y que LabRole permita cognito-idp:SetUICustomization.
#   aws/auth/ui/apply-ui.sh
set -euo pipefail
cd "$(dirname "$0")"
REGION="${AWS_REGION:-us-east-1}"

POOL=$(aws ssm get-parameter --name /fleetpulse/auth/user_pool_id --query Parameter.Value --output text --region "$REGION")
CLIENT=$(aws ssm get-parameter --name /fleetpulse/auth/client_id --query Parameter.Value --output text --region "$REGION")
echo "Pool=$POOL Client=$CLIENT" >&2

aws cognito-idp set-ui-customization \
  --user-pool-id "$POOL" \
  --client-id "$CLIENT" \
  --css "$(cat login.css)" \
  --image-file "fileb://logo.png" \
  --region "$REGION" >&2
echo "Listo. Abre la Hosted UI (https://<domain>/login?client_id=$CLIENT&response_type=code&scope=openid&redirect_uri=<callback>) para verlo." >&2
