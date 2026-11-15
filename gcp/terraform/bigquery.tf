# Dataset de analitica en US (BigQuery). Las tablas se particionan por dia y exigen
# filtro de particion para no barrer todo el historico (control de costo, free tier).
resource "google_bigquery_dataset" "fleetpulse" {
  dataset_id = "fleetpulse"
  location   = "US"
  depends_on = [google_project_service.enabled]
}

resource "google_bigquery_table" "telemetry" {
  dataset_id          = google_bigquery_dataset.fleetpulse.dataset_id
  table_id            = "telemetry"
  schema              = file("${path.module}/schemas/telemetry.json")
  deletion_protection = false

  time_partitioning {
    type  = "DAY"
    field = "ts"
  }
  require_partition_filter = true
}

resource "google_bigquery_table" "domain_events" {
  dataset_id          = google_bigquery_dataset.fleetpulse.dataset_id
  table_id            = "domain_events"
  schema              = file("${path.module}/schemas/domain_events.json")
  deletion_protection = false

  time_partitioning {
    type  = "DAY"
    field = "occurred_at"
  }
  require_partition_filter = true
}
