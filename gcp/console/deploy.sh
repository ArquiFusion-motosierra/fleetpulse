#!/usr/bin/env bash
# Despliega Cloud Run `console` (T15) en us-east1. Cloud Build construye el Dockerfile (nivel gratuito).
#   AWS_REGISTRY_URL=$(aws ssm get-parameter --name /fleetpulse/registry/url --query Parameter.Value --output text) \
#   AWS_INCIDENTS_URL=$(aws ssm get-parameter --name /fleetpulse/api/url --query Parameter.Value --output text) \
#   gcp/console/deploy.sh
# MOCK=1 gcp/console/deploy.sh   -> despliega con datos de prueba (respaldo para grabar si AWS no esta).
#
# Una vez por proyecto (hecho el 15/11: SA fp-console creada; consola en https://console-603826680958.us-east1.run.app):
#   gcloud iam service-accounts create fp-console
#   printf '{"api_key":"<valor de fleetpulse/api/console-key>"}' | gcloud secrets create aws-console-api-key --data-file=-
#   gcloud secrets add-iam-policy-binding aws-console-api-key \
#     --member serviceAccount:fp-console@${GCP_PROJECT_ID}.iam.gserviceaccount.com --role roles/secretmanager.secretAccessor
set -euo pipefail
cd "$(dirname "$0")"

PROJECT="${GCP_PROJECT_ID:-fleetpulse-g19-2026}"
REGION="${GCP_REGION:-us-east1}"
MOCK="${MOCK:-0}"
if [ "$MOCK" != "1" ]; then
  : "${AWS_REGISTRY_URL:?URL de la API de lectura (SSM /fleetpulse/registry/url)}"
  : "${AWS_INCIDENTS_URL:?URL del HTTP API de incidentes (SSM /fleetpulse/api/url)}"
fi

# min 0 / max 2 instancias, 512 MiB, CPU solo durante la solicitud: queda en la capa gratuita, sin load balancer.
# Publica (--allow-unauthenticated): es la consola web de la demo; la API key de AWS nunca sale del servidor.
gcloud run deploy console \
  --project "$PROJECT" --region "$REGION" --source . \
  --service-account "fp-console@${PROJECT}.iam.gserviceaccount.com" \
  --min-instances 0 --max-instances 2 --memory 512Mi --cpu 1 --concurrency 80 --timeout 30 \
  --cpu-throttling --allow-unauthenticated \
  --set-env-vars "MOCK=${MOCK},GCP_PROJECT_ID=${PROJECT},AWS_KEY_SECRET=aws-console-api-key,AWS_REGISTRY_URL=${AWS_REGISTRY_URL:-},AWS_INCIDENTS_URL=${AWS_INCIDENTS_URL:-}"

gcloud run services describe console --project "$PROJECT" --region "$REGION" --format 'value(status.url)'
