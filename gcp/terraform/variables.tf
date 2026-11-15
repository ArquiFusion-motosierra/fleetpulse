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

variable "tenants" {
  type        = list(string)
  default     = ["mineria", "agro", "industria"]
  description = "Parques (tenants) de FleetPulse (contracts/tenants.yaml). Cada uno tiene su dataset fp_<tenant> con vistas autorizadas sobre fleetpulse."
}

variable "default_tenant" {
  type        = string
  default     = "mineria"
  description = "Parque de las filas sin tenant_id (cargas anteriores al multi-tenancy)."
}

variable "tenant_readers" {
  type        = map(list(string))
  default     = {}
  description = "Lectores por parque: {\"agro\" = [\"user:ana@ejemplo.com\", \"group:agro@ejemplo.com\"]}. Reciben dataViewer solo en fp_<tenant>, nunca en fleetpulse."
}
