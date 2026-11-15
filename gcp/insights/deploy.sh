#!/usr/bin/env bash
# Construye la imagen de insights en Artifact Registry (Cloud Build, free tier).
# Imprime el nombre de la imagen para pasarselo a Terraform:
#   gcp/insights/deploy.sh
#   terraform -chdir=gcp/terraform apply -var insights_image=$(gcp/insights/deploy.sh | tail -1)
set -euo pipefail
cd "$(dirname "$0")"

PROJECT="${GCP_PROJECT_ID:-fleetpulse-g19-2026}"
REGION="${GCP_REGION:-us-east1}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/fleetpulse/insights:latest"

gcloud builds submit --project "$PROJECT" --tag "$IMAGE" . >&2
echo "$IMAGE"
