# Landing de telemetria: el exporter deja NDJSON aqui y un load job lo sube a BigQuery.
resource "google_storage_bucket" "landing" {
  name                        = "fp-landing-${var.name_suffix}"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true

  lifecycle_rule {
    condition { age = 3 }
    action { type = "Delete" }
  }

  depends_on = [google_project_service.enabled]
}
