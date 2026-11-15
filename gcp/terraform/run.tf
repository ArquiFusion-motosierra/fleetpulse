# Cloud Run insights (FastAPI). min 0 / max 2 / 512 MiB: dentro del free tier, sin load balancer.
# La imagen es un placeholder hasta correr gcp/insights/deploy.sh; luego:
#   terraform apply -var insights_image=<REGION>-docker.pkg.dev/<PROJECT>/fleetpulse/insights:latest
resource "google_cloud_run_v2_service" "insights" {
  name                = "insights"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account = google_service_account.insights.email

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    containers {
      image = var.insights_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          memory = "512Mi"
          cpu    = "1"
        }
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "BQ_DATASET"
        value = google_bigquery_dataset.fleetpulse.dataset_id
      }
      env {
        name  = "GEMINI_SECRET"
        value = google_secret_manager_secret.gemini.secret_id
      }
      env {
        name  = "GEMINI_MODEL"
        value = "gemini-flash-lite-latest"
      }
      # 15 s (no los 7 por defecto): gemini-flash-lite a veces tarda mas y /explain caia en fallback.
      env {
        name  = "GEMINI_TIMEOUT_S"
        value = "15"
      }
    }
  }

  depends_on = [google_project_service.enabled]
}

# Solo fp-invoker (AWS con ID token) puede llamar al servicio; no es publico.
resource "google_cloud_run_v2_service_iam_member" "invoker" {
  name     = google_cloud_run_v2_service.insights.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.invoker.email}"
}
