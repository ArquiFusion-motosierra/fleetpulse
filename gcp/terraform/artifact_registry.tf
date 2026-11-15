# Repo de imagenes Docker. Politica de limpieza: conserva solo las 2 mas recientes.
resource "google_artifact_registry_repository" "fleetpulse" {
  repository_id = "fleetpulse"
  location      = var.region
  format        = "DOCKER"
  description   = "Imagenes de Cloud Run (insights, console)"

  cleanup_policies {
    id     = "keep-2-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 2
    }
  }

  depends_on = [google_project_service.enabled]
}
