# Llave de Gemini para insights. El valor NO va en Terraform: se sube aparte
#   printf '<api-key>' | gcloud secrets versions add gemini-api-key --data-file=-
# El secreto aws-console-api-key lo maneja gcp/console (Alex), no este modulo.
resource "google_secret_manager_secret" "gemini" {
  secret_id = "gemini-api-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret_iam_member" "insights_gemini" {
  secret_id = google_secret_manager_secret.gemini.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.insights.email}"
}
