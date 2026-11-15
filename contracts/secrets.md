# Secretos, recursos y endpoints (T04, plan B sin OCI)

Solo nombres y formas. **Nunca valores.** Los valores los sube `scripts/push-secrets.sh` desde variables de entorno.

## AWS Secrets Manager (us-east-1, llave `aws/secretsmanager`)

| Secreto | JSON | Lo escribe | Lo leen |
|---|---|---|---|
| `fleetpulse/gcp/sa-exporter` | Llave JSON de la SA `fp-exporter` tal cual | Fabricio (Terraform) | `gcp-exporter`, `gcp-events` |
| `fleetpulse/gcp/sa-invoker` | `{"key": <llave JSON de fp-invoker>, "audience": "https://insights-<hash>-ue.a.run.app"}` | Fabricio (Terraform) | `incident-flow` ExplainCause, `daily-planning` |
| `fleetpulse/api/ingest-key` | `{"api_key": "..."}` | Benjamin | `simulator` (`POST /telemetry`, `POST /context`) |
| `fleetpulse/api/console-key` | `{"api_key": "..."}` | Alex | API de lectura de `registry` y `POST /incidents/{id}/close`; la consola la guarda en GCP Secret Manager `aws-console-api-key` |

Caché de 5 minutos en cada Lambda.

## AWS: nombres fijos

| Recurso | Nombre |
|---|---|
| Tablas DynamoDB | `fp-state`, `fp-outbox`, `fp-inbox`, `fp-registry` |
| Bucket S3 | `fp-raw-<accountId>` (prefijos `telemetry/dt=YYYY-MM-DD/hr=HH/`, `events/dt=YYYY-MM-DD/`, `backfill/`) |
| SNS | `fp-domain-events`, `fp-notify` (atributo `role`: technician, supervisor, management, operator), `fp-alerts` |
| SQS | `registry-q` + `registry-dlq` (pila `aws/registry`), `gcp-events-q` + `gcp-events-dlq` |
| Step Functions | `incident-flow`, `daily-planning` |
| SSM | `/fleetpulse/sim/scenarios` (escenarios activos), `/fleetpulse/api/url` (HTTP API de ingest), `/fleetpulse/registry/url` (API de lectura) |

## AWS: HTTP API de ingest e incidentes (Benjamin)

| Ruta | Auth | Llama | Contrato |
|---|---|---|---|
| `POST /telemetry` | `x-api-key` (ingest-key) | simulator | `telemetry-batch.schema.json` |
| `POST /context` | `x-api-key` (ingest-key) | simulator | `fuel.refill.schema.json` (objeto o lista) |
| `GET /approvals/{token}` y `POST /approvals/{token}` | task token de un solo uso | correo de aprobación | — |
| `POST /incidents/{incident_id}/close` | `x-api-key` (console-key) | consola (botón Cerrar, vía BFF) | body `{"closed_by": "TEC-01", "parts_used": [...], "cause_summary": "..."}` → emite `incident.closed` |

## AWS: `fp-registry` (Alex, `aws/registry/`)

On-demand, PITR y stream. Lo llena la Lambda `registry` desde `registry-q` (todos los eventos de dominio) y el seed `scripts/seed-registry.py`.

| Entidad | PK | SK | Atributos | GSI1 (PK / SK) |
|---|---|---|---|---|
| Máquina | `MACHINE#<id>` | `META` | model, serial, zone_id, commissioned_at, severity (ok/observe/schedule/immediate), open_incident_id | `MACHINES` / `<zone_id>#<id>` |
| Incidente | `INCIDENT#<id>` | `META` | machine_id, zone_id, group_id, severity, status (open/assigned/closed), score, failure_mode, opened_at, assigned_at, closed_at, downtime_h, tech_id, approval, explanation, explanation_src, cause_summary, recurrence_flag | `INCIDENTS` / `<opened_at>` |
| Evento del incidente | `INCIDENT#<id>` | `EVT#<occurred_at>#<event_id>` | type, source, payload (JSON) | — |
| Reserva | `INCIDENT#<id>` | `RSV#<event_id>#<línea>` | part_id, zone_id, qty, status (reserved/consumed/released) | — |
| Historial por máquina | — | — | (consulta `GSI2`) | GSI2: `MACHINE#<id>` / `INC#<opened_at>` en el ítem del incidente |
| Stock | `STOCK` | `<part_id>#<zone_id>` | on_hand, reserved, available (= on_hand - reserved), reorder_point; reserva = `SET reserved += q, available -= q` con condición `available >= :qty` (DynamoDB no admite aritmética en condiciones) | — |
| Repuesto / técnico / zona | `PART#<id>` / `TECH#<id>` / `ZONE#<id>` | `META` | catálogo de `ids.yaml`; técnico con available | — |
| BOM | `BOM#<model_id>` | `<failure_mode>#<part_id>` | qty, replace_every_h | — |
| Movimiento | `MOVE#<part_id>#<zone_id>` | `<ts>#<incident_id>` | qty, kind (consumption/restock) | — |
| Pedido sugerido | `REORDER` | `<created_at>#<part_id>#<zone_id>` | qty, source (shortage/forecast), incident_id, forecast_14d | — |
| KPI diario | `KPI#<day>` | `<machine_id>` | campos de `kpi.daily` | — |

Idempotencia: cada evento se aplica en una `TransactWriteItems` que incluye `fp-inbox` (`message_id = event_id`, condición `attribute_not_exists`). La reserva de cada línea es su propia transacción con la reserva `RSV#<event_id>#<línea>` como condición, así que un reintento no reserva dos veces. Con el resultado, `registry` escribe `part.reserved` y/o `part.shortage` (`source: aws.registry`) en `fp-outbox`.

## AWS: API de lectura para la consola (`registry`, HTTP API propia, header `x-api-key` = console-key)

| Ruta | Respuesta |
|---|---|
| `GET /machines` | `{"machines": [{"machine_id", "model", "zone_id", "lat", "lon", "status", "severity", "score", "hours_total", "fuel_remaining_ratio", "open_incident_id", "updated_at"}]}` (posición y estado de `fp-state`, severidad de `fp-registry`) |
| `GET /machines/{id}` | máquina + `incidents` (historial con `recurrence_flag`, repuestos usados) |
| `GET /incidents?status=open\|assigned\|closed&limit=50` | `{"incidents": [...]}` más recientes primero |
| `GET /incidents/{id}` | incidente + `timeline` + `reservations` |
| `GET /stock` | `{"stock": [{"part_id", "sku", "name", "zone_id", "on_hand", "reserved", "available", "reorder_point", "below_reorder"}]}` |
| `GET /reorders` | `{"reorders": [...]}` (T19) |
| `GET /kpis/{day}` | `{"day", "machines": [...]}` (T19) |

## GCP

| Recurso | Nombre |
|---|---|
| Proyecto / región | `fleetpulse-<sufijo>` / `us-east1` |
| Bucket | `fp-landing-<sufijo>` (lifecycle 3 días) |
| Dataset BigQuery (US) | `fleetpulse`: `telemetry`, `domain_events` |
| Service accounts | `fp-exporter`, `fp-invoker`, `fp-insights`, `fp-console` |
| Secret Manager | `gemini-api-key`, `aws-console-api-key` |
| Cloud Run `insights` | `GET /health`, `POST /explain`, `GET /peers/{machine_id}`, `POST /jobs/refresh`, `GET /jobs/status`, `POST /forecast/parts`, `POST /kpis/daily`, `POST /anomalies/fuel`, `GET /baselines` |
| Cloud Run `console` | estáticos de React + BFF `GET /api/machines`, `/api/machines/{id}`, `/api/incidents`, `/api/incidents/{id}`, `/api/stock`, `POST /api/incidents/{id}/close` |
