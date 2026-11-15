variable "project_id" {
  type        = string
  description = "ID del proyecto GCP (Free Trial, sin upgrade)."
}

variable "region" {
  type        = string
  default     = "us-east1"
  description = "Region de Cloud Run, Storage y Artifact Registry."
}

variable "name_suffix" {
  type        = string
  default     = "g19-2026"
  description = "Sufijo para nombres globales (bucket)."
}

variable "insights_image" {
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
  description = "Imagen del servicio insights. Placeholder hasta que gcp/insights/deploy.sh construya la real en Artifact Registry."
}
