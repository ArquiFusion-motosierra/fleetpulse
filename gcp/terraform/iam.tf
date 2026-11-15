# Una service account por rol. fp-console (consola, T15) la maneja gcp/console (Alex), no este modulo.
resource "google_service_account" "exporter" {
  account_id   = "fp-exporter"
  display_name = "Carga telemetria a BigQuery (load jobs)"
}

resource "google_service_account" "invoker" {
  account_id   = "fp-invoker"
  display_name = "Invoca Cloud Run insights con ID token (desde AWS)"
}

resource "google_service_account" "insights" {
  account_id   = "fp-insights"
  display_name = "Identidad del servicio Cloud Run insights"
}

# fp-exporter: deja NDJSON en el bucket y corre load jobs hacia el dataset.
resource "google_storage_bucket_iam_member" "exporter_bucket" {
  bucket = google_storage_bucket.landing.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.exporter.email}"
}

resource "google_project_iam_member" "exporter_job" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.exporter.email}"
}

resource "google_bigquery_dataset_iam_member" "exporter_data" {
  dataset_id = google_bigquery_dataset.fleetpulse.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.exporter.email}"
}

# fp-insights: consulta BigQuery (lectura) y corre jobs de consulta.
resource "google_project_iam_member" "insights_job" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.insights.email}"
}

resource "google_bigquery_dataset_iam_member" "insights_data" {
  dataset_id = google_bigquery_dataset.fleetpulse.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.insights.email}"
}
