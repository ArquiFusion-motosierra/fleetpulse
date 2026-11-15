# Secretos, recursos y endpoints (T04)

Solo nombres y formas. **Nunca valores.** Los valores los sube `scripts/push-secrets.sh` desde variables de entorno.

## AWS Secrets Manager (us-east-1, llave `aws/secretsmanager`)

| Secreto | JSON | Lo escribe | Lo leen |
|---|---|---|---|
| `fleetpulse/oci/ords` | `{"base_url": "https://<id>-fpdb.adb.us-ashburn-1.oraclecloudapps.com/ords/fleet/", "client_id": "...", "client_secret": "..."}` | Alex (`04_oauth.sql`) | `oci-writer`, `snapshot-publisher`, `replay-oci.py` |
| `fleetpulse/gcp/sa-exporter` | Llave JSON de la SA `fp-exporter` tal cual | Fabricio (Terraform) | `gcp-exporter`, `gcp-events` |
| `fleetpulse/gcp/sa-invoker` | `{"key": <llave JSON de fp-invoker>, "audience": "https://insights-<hash>-ue.a.run.app"}` | Fabricio (Terraform) | `incident-flow` ExplainCause, `daily-planning` |
| `fleetpulse/api/ingest-key` | `{"api_key": "..."}` | Benjamin | `simulator`, APEX Web Credential `fp_aws_api` |

Caché de 5 minutos en cada Lambda. Token OAuth de ORDS cacheado 50 minutos.

## AWS: nombres fijos

| Recurso | Nombre |
|---|---|
| Tablas DynamoDB | `fp-state`, `fp-outbox`, `fp-inbox` |
| Bucket S3 | `fp-raw-<accountId>` (prefijos `telemetry/dt=YYYY-MM-DD/hr=HH/`, `events/dt=YYYY-MM-DD/`, `backfill/`) |
| SNS | `fp-domain-events`, `fp-notify` (atributo `role`: technician, supervisor, management, operator), `fp-alerts` |
| SQS | `oci-writer-q` + `oci-writer-dlq`, `gcp-events-q` + `gcp-events-dlq` |
| Step Functions | `incident-flow`, `daily-planning` |
| SSM | `/fleetpulse/sim/scenarios` (JSON de escenarios activos), `/fleetpulse/api/url` (URL base del HTTP API) |

## AWS: HTTP API (API Gateway)

| Ruta | Auth | Llama | Contrato |
|---|---|---|---|
| `POST /telemetry` | header `x-api-key` | simulator | `telemetry-batch.schema.json` |
| `POST /context` | header `x-api-key` | simulator | `fuel.refill.schema.json` |
| `GET /approvals/{token}` y `POST /approvals/{token}` | task token de un solo uso | correo de aprobación, APEX | — |
| `POST /incidents/{incident_id}/close` | header `x-api-key` | APEX (botón Cerrar) | body `{"closed_by": "TEC-01", "parts_used": [...], "cause_summary": "..."}` → emite `incident.closed` |

## OCI: ORDS módulo `fleet/v1` (esquema `FLEET`, OAuth2 client credentials)

Token: `POST <base_url>oauth/token` con `grant_type=client_credentials` (Basic `client_id:client_secret`).

| Ruta | Body | Respuesta |
|---|---|---|
| `POST v1/events` | Un evento de dominio (`envelope` + payload) | `200 {"status": "applied" \| "duplicate", "event_id": "...", "result_event": <part.reserved \| part.shortage \| null>}` |
| `POST v1/machines/snapshot` | `machine.snapshot.schema.json` | `200 {"upserted": n}` |
| `POST v1/reorders` | `{"day": "YYYY-MM-DD", "forecast": [{"part_id", "zone_id", "forecast_14d"}]}` | `200 {"suggestions": [{"part_id", "zone_id", "qty"}]}` |
| `POST v1/kpis` | payload de `kpi.daily` | `200 {"upserted": n}` |
| `GET v1/incidents/:id` | — | Incidente con su línea de tiempo (para `smoke.sh`) |

`oci-writer` toma `result_event` y lo escribe en `fp-outbox` para que llegue también a BigQuery.

## GCP

| Recurso | Nombre |
|---|---|
| Proyecto / región | `fleetpulse-<sufijo>` / `us-east1` |
| Bucket | `fp-landing-<sufijo>` (lifecycle 3 días) |
| Dataset BigQuery (US) | `fleetpulse`: `telemetry`, `domain_events` |
| Service accounts | `fp-exporter`, `fp-invoker`, `fp-insights` |
| Secret Manager | `gemini-api-key` |
| Cloud Run `insights` | `GET /health`, `POST /explain`, `GET /peers/{machine_id}`, `POST /jobs/refresh`, `GET /jobs/status`, `POST /forecast/parts`, `POST /kpis/daily`, `POST /anomalies/fuel`, `GET /baselines` |
