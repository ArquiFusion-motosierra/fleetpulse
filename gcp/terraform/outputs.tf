output "landing_bucket" {
  value = google_storage_bucket.landing.name
}

output "dataset" {
  value = google_bigquery_dataset.fleetpulse.dataset_id
}

output "insights_url" {
  value = google_cloud_run_v2_service.insights.uri
}

output "artifact_registry" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.fleetpulse.repository_id}"
}

output "service_accounts" {
  value = {
    exporter = google_service_account.exporter.email
    invoker  = google_service_account.invoker.email
    insights = google_service_account.insights.email
  }
}

output "tenant_datasets" {
  value = { for t, d in google_bigquery_dataset.tenant : t => d.dataset_id }
}
