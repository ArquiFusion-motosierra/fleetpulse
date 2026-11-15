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

## AWS: `fp-registry`

Modelo fijado en `docs/superpowers/plans/2026-11-14-aws-core-pipeline.md` ("Data model locked here"). La tabla la crea `fp-core`.

| Ítem | PK / SK | Lo escribe |
|---|---|---|
| Incidente | `INCIDENT#<id>` / `META` (GSI1 `MACHINE#<id>` / `INC#<opened_at>`, GSI2 `STATUS#<open\|closed>` / `<opened_at>`) | `incident-flow` (abre, asigna) y `POST /incidents/{id}/close` |
| Evento del incidente | `INCIDENT#<id>` / `EV#<occurred_at>#<event_id>`: `event_type, source, payload` (sobre completo como JSON, igual que `fp-outbox`) | quien escribe el evento, en la misma transacción que el outbox |
| Stock | `STOCK#<part_id>#<zone_id>` / `META`: `on_hand, reserved, available (= on_hand - reserved), reorder_point, sku, name` | seed y Lambda `registry` |
| Reserva | `RSV#<incident_id>` / `<part_id>#<zone_id>`: `reservation_id (<event_id>#<línea>), qty, status (reserved/consumed/released)` | Lambda `registry` |
| Movimiento | `MOVE#<part_id>#<zone_id>` / `<closed_at>#<incident_id>` | Lambda `registry` |
| Pedido sugerido | `REORDER#<part_id>#<zone_id>` / `<created_at>` (GSI1 `REORDERS` / `<created_at>#…`) | Lambda `registry` |
| KPI diario | `KPI#<day>` / `<machine_id>` | Lambda `registry` |

Lambda `registry` (cola `registry-q`, filtrada a `part.reserve.requested`, `incident.closed` y `kpi.daily`):
- Reserva cada línea con `SET reserved += q, available -= q` y condición `available >= :q` (DynamoDB no admite aritmética en condiciones), probando primero la zona del incidente y luego la más cercana. Si no hay stock, escribe un `REORDER` y emite `part.shortage`.
- Escribe `part.reserved` / `part.shortage` (`source: aws.registry`) en `fp-outbox` junto con su `EV#`, en la misma transacción.
- Con `incident.closed` consume lo reservado, descuenta lo usado (`parts_used`) y libera lo que sobró.
- Idempotencia: `fp-inbox` con `message_id = registry:<event_id>`, en la misma transacción.

## AWS: API de lectura para la consola (`registry`, HTTP API propia, header `x-api-key` = console-key)

| Ruta | Respuesta |
|---|---|
| `GET /machines` | `{"machines": [{"machine_id", "model", "zone_id", "lat", "lon", "status" (motor, si ingest guarda `engine_status`), "severity" (= `status` de `fp-state`), "score", "hours_total", "fuel_remaining_ratio", "open_incident_id", "updated_at"}]}` |
| `GET /machines/{id}` | máquina + `incidents` (historial con `recurrence_flag`, repuestos usados) |
| `GET /incidents?status=open\|assigned\|closed&limit=50` (assigned = open con técnico) | `{"incidents": [...]}` más recientes primero |
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
