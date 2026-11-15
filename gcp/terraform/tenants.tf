# Multi-tenancy en BigQuery (docs/superpowers/plans/2026-11-15-multi-tenancy.md).
# Las tablas base (fleetpulse.telemetry, fleetpulse.domain_events) son compartidas y llevan la columna tenant_id. Cada
# parque tiene su propio dataset fp_<tenant> con vistas filtradas por parque, autorizadas sobre el dataset base: quien
# lee fp_agro ve solo filas de agro y no necesita (ni recibe) acceso a fleetpulse. Las vistas no cuestan en reposo y
# siguen el filtro de particion de la tabla base (require_partition_filter), asi que no cambian el costo por consulta.
locals {
  tenant_tables = {
    telemetry     = google_bigquery_table.telemetry.table_id
    domain_events = google_bigquery_table.domain_events.table_id
  }
  tenant_views = {
    for pair in setproduct(var.tenants, keys(local.tenant_tables)) :
    "${pair[0]}.${pair[1]}" => { tenant = pair[0], table = pair[1] }
  }
}

resource "google_bigquery_dataset" "tenant" {
  for_each    = toset(var.tenants)
  dataset_id  = "fp_${each.key}"
  location    = "US"
  description = "FleetPulse parque ${each.key}: vistas autorizadas sobre fleetpulse filtradas por tenant_id"
  labels = {
    tenant = each.key
  }
  depends_on = [google_project_service.enabled]
}

resource "google_bigquery_table" "tenant_view" {
  for_each            = local.tenant_views
  dataset_id          = google_bigquery_dataset.tenant[each.value.tenant].dataset_id
  table_id            = each.value.table
  deletion_protection = false
  labels = {
    tenant = each.value.tenant
  }

  view {
    use_legacy_sql = false
    query          = <<-SQL
      SELECT * FROM `${var.project_id}.${google_bigquery_dataset.fleetpulse.dataset_id}.${local.tenant_tables[each.value.table]}`
      WHERE COALESCE(tenant_id, '${var.default_tenant}') = '${each.value.tenant}'
    SQL
  }
}

# Vista autorizada: la vista lee la tabla base con permisos propios, sin dar acceso a fleetpulse a quien la consulta.
resource "google_bigquery_dataset_access" "tenant_view" {
  for_each   = local.tenant_views
  dataset_id = google_bigquery_dataset.fleetpulse.dataset_id
  view {
    project_id = var.project_id
    dataset_id = google_bigquery_table.tenant_view[each.key].dataset_id
    table_id   = google_bigquery_table.tenant_view[each.key].table_id
  }
}

# Lectores de cada parque: dataViewer solo en fp_<tenant>.
resource "google_bigquery_dataset_iam_member" "tenant_reader" {
  for_each = {
    for pair in flatten([for t, members in var.tenant_readers : [for m in members : { tenant = t, member = m }]]) :
    "${pair.tenant}/${pair.member}" => pair
  }
  dataset_id = google_bigquery_dataset.tenant[each.value.tenant].dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = each.value.member
}

# insights (Cloud Run) puede leer cualquier parque para los modelos de flota y las consultas por parque.
resource "google_bigquery_dataset_iam_member" "insights_tenant" {
  for_each   = toset(var.tenants)
  dataset_id = google_bigquery_dataset.tenant[each.key].dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.insights.email}"
}
