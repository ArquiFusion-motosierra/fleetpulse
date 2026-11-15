# Plan: reportes de anormalidad del operario, roles con Cognito y mantenimiento según fabricante

## Context

La entrevista con el ingeniero mecatrónico (mantenimiento preventivo aeronáutico) dejó cuatro insights: (1) el fabricante fija en su manual ciclos de uso, horas motor y anormalidades a vigilar; (2) hay que monitorear condiciones de operación críticas como el sello de culata (temperatura) y vibraciones; (3) debe existir un **sistema de reportes de anormalidades**: el operario que presencia algo levanta un informe, un LLM lo procesa y recomienda al técnico asignado los chequeos asociados (ej. temperatura alta de motor → sello/junta de culata); (4) todo esto debe ser visible en una web multiusuario por tipo de empleado, con un sitio de administración.

Estado real a las 00:50 del domingo: AWS desplegado (8 stacks), `smoke ingest` verde, consola React+MapLibre en Cloud Run (GCP) **sin login ni roles** y solo con lectura más el botón Cerrar; `insights` tiene `/explain` y `/peers` con Gemini; `fp-registry` tiene una API de solo lectura con API key. Alex ya se puso T26–T30 (ficha del fabricante `oem_specs.yaml`, escenario S7 junta de culata, informe técnico en `/explain`, consola con informe y plan del fabricante, espectro de vibración): cubren los insights 1 y 2 y la mitad del 3. **Falta** el flujo humano (operario → reporte → LLM → técnico → cierre) y la web por roles con administración.

Decisiones tomadas con Benjamin: la consola **se queda en GCP** (no S3+CloudFront); usuarios y roles con **Cognito** (un usuario de prueba por rol); el reporte **cruza telemetría reciente + ficha del fabricante**, **se vincula o abre un incidente** en `incident-flow`, **el técnico lo cierra** con hallazgos y repuestos; **admin** gestiona catálogo; todo **entra en M3 (dom 14:30)**.

Módulos de IA que cubren la rúbrica (confirmado): **ML de anomalías** = score estadístico en línea (z propio/pares, banda muerta 2,25) + BigQuery ML `m_peer_clusters` (KMEANS, pares reales) y `m_fuel_trend` (ARIMA_PLUS + `ML.DETECT_ANOMALIES`); **LLM** = Gemini Flash en `/explain` (causa, repuestos, mensajes por rol) y, con este plan, en `/recommend-checks` (observaciones del operario cruzadas con telemetría y manual del fabricante). En el pitch, el score se presenta como estadística y la IA como lo que una regla no decide.

## Alcance y reparto (para llegar a M3)

Ruta crítica propuesta: **T31 → T32 → T34**, con **T33** en paralelo. Para que Alex llegue, **T30 (espectro de vibración) pasa a incremento** y la gráfica de espectro de T29 se posterga; T26, T27, T28 y T29 (sin espectro) se mantienen.

| ID | Horario (Lima) | Dueño | Tarea | Depende de |
|---|---|---|---|---|
| T31 | Dom 01:00–03:00 | Benjamin | Cognito: User Pool, cliente PKCE, dominio Hosted UI, atributo `custom:role`, 5 usuarios de prueba, SSM outputs; autorizador JWT en la HTTP API compartida | — |
| T32 | Dom 09:00–12:00 | Benjamin | Servicio `aws/reports`: crear/listar/ver/cerrar reportes, recomendación vía `insights`, vínculo o apertura de incidente, eventos al outbox | T31, T33 (contrato), T26 (ficha) |
| T33 | Dom 09:00–11:00 | Fabricio | `insights POST /recommend-checks` con Gemini + plantilla de respaldo, usando `oem_specs.yaml` | T16 (merged), T26 |
| T34 | Dom 09:00–13:00 | Alex | Consola: login Cognito, enrutado por rol, formulario de reporte (operario), vista de chequeos y cierre (técnico), aprobaciones y reportes (supervisor), KPIs (dirección), catálogo (admin); BFF reenvía el JWT | T31, T32, T15 |
| T35 | Dom 12:00–13:00 | Benjamin | Rutas de administración: técnicos (zona, disponibilidad), tipos de anormalidad | T31, T32 |
| T17+ | Dom 13:15–14:30 | Todos | Punta a punta ampliada: operario reporta en EX-017 → técnico ve chequeos → incidente vinculado → cierre de reporte e incidente | T32–T35 |

## Diseño

### 1. Identidad y roles (T31, `aws/auth/`)
- Nuevo servicio Serverless `fp-auth` (solo CloudFormation): `AWS::Cognito::UserPool` `fleetpulse-users` (login por email, sin MFA, política de contraseña mínima), esquema con atributo custom `role` (string), `AWS::Cognito::UserPoolClient` sin secreto con flujo `code` + PKCE, callback/logout = URL de la consola (`https://console-…run.app/` y `http://localhost:5173/` para desarrollo), `AWS::Cognito::UserPoolDomain` `fleetpulse-<accountId>`. Outputs y SSM: `/fleetpulse/auth/user_pool_id`, `/fleetpulse/auth/client_id`, `/fleetpulse/auth/domain`, `/fleetpulse/auth/issuer`.
- `scripts/cognito-seed.sh`: crea 5 usuarios (`operador@`, `tecnico@`, `supervisor@`, `direccion@`, `admin@` + dominio del equipo), contraseña permanente desde variable de entorno, `custom:role` = `operator|technician|supervisor|management|admin`; para `tecnico@` también `custom:tech_id = TEC-01`.
- Autorizador JWT en la API compartida: en `aws/ingest/serverless.yml` añadir `provider.httpApi.authorizers.cognito: {type: jwt, identitySource: $request.header.Authorization, issuerUrl: ${cf:fp-auth-${sls:stage}.IssuerUrl, 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_offline'}, audience: [${cf:fp-auth-${sls:stage}.ClientId, 'offline'}]}`. Compose: `auth` antes de `core`. Las rutas existentes (telemetry, approvals, close, lectura con API key) **no cambian**.
- Roles en código: el claim `custom:role` llega en `event.requestContext.authorizer.jwt.claims`; helper `fplib.authz.role_of(event) -> str` y `require_role(event, *roles)` (lanza 403). Reutiliza el estilo de `fplib.auth`.

### 2. Reportes de anormalidad (T32, `aws/reports/`, Benjamin)
- Modelo en `fp-registry` (mismo single-table): `REPORT#<report_id>` / `META` con `report_id (RPT-<machine>-<YYYYMMDDTHHMMSS>)`, `machine_id`, `model`, `zone_id`, `abnormality_type` (catálogo), `description`, `severity_perceived (low|medium|high)`, `reported_by` (sub/email del JWT), `reported_at`, `status (open|in_review|closed)`, `telemetry_snapshot` (últimas lecturas resumidas), `recommendation` (salida de `/recommend-checks`), `incident_id` (vinculado o creado), `assigned_tech_id`, `closed_at`, `findings`, `parts_used`; `GSI1PK MACHINE#<id>` / `GSI1SK RPT#<reported_at>`; `GSI2PK RSTATUS#<status>` / `GSI2SK <reported_at>`. Línea de tiempo: `REPORT#<id>` / `EV#<ts>#<event_id>`.
- Rutas en la API compartida (todas con autorizador `cognito`): `POST /reports` (operator, supervisor, admin), `GET /reports?status=&machine_id=&mine=1` (todos los roles; `mine` filtra por `reported_by` o por `assigned_tech_id`), `GET /reports/{id}`, `POST /reports/{id}/close` (technician asignado, supervisor, admin) con `{findings, parts_used[], confirmed_cause}`.
- Lambda `reports` (`aws/reports/api/reports.py`, `lib/snapshot.py`, `lib/recommend.py`, `lib/link_incident.py`): 
  1. valida el body contra `contracts/report.schema.json` (nuevo);
  2. `snapshot.py`: lee `MACHINE#<id>/META` (hours_total, status, baseline) y las últimas 30 lecturas `R#` → medias de `coolant_temp_c, hydraulic_oil_temp_c, hydraulic_pressure_bar, vibration_mm_s, engine_rpm, fuel_rate_lph` y últimos `fault_codes`; añade la ficha del modelo desde `fplib.ids` + `oem_specs.yaml` (nuevo loader `fplib.ids.oem_specs(model)`; `build.sh` ya copia `ids.yaml`, se añade `oem_specs.yaml` a `fplib/data/`);
  3. `recommend.py`: `POST {audience}/recommend-checks` en `insights` con ID token (mover `aws/incidents/lib/gcp_auth.py` a `fplib/gcp_auth.py` para reutilizarlo; `aws/incidents` pasa a importarlo de ahí), timeout 8 s, fallback por `abnormality_type` (tabla local: temperatura alta → junta de culata, sello, termostato; vibración → rodamiento/mando final; presión baja → bomba/sellos; humo → inyectores/turbo; fuga → sellos/mangueras);
  4. `link_incident.py`: si hay incidente abierto de la máquina (`registry.open_incident_for`) lo vincula y agrega `EV#` al incidente con `report.linked`; si no y `recommendation.urgency ∈ {schedule, immediate}`, inicia `incident-flow` con nombre `INC-<machine>-<ventana>` y entrada equivalente a la del scoring (`score` derivado: schedule=2.1 / immediate=3.6, `deviated_vars` del snapshot con |z| ≥ 1 o el síntoma reportado, `failure_mode` de la recomendación, `source: "operator_report"`); el técnico asignado por el flujo se copia al reporte cuando `incident.assigned` llega (consumidor existente de `aws/registry` o lectura perezosa en `GET /reports/{id}`: elegir la lectura perezosa, sin tocar el consumidor de Alex);
  5. escribe `REPORT#` META + `EV#` + evento de dominio `report.created` al outbox en una transacción (reutiliza `fplib.outbox.transact_items`); el cierre escribe `report.closed` y, si el incidente vinculado sigue abierto, un `EV#` en el incidente.
- Contratos: `contracts/report.schema.json` (body de creación y de cierre; `abnormality_type` enum inicial: `high_engine_temp, high_hydraulic_temp, abnormal_vibration, low_hydraulic_pressure, smoke, leak, noise, warning_light, power_loss, other`), y añadir `report.created`, `report.closed` al enum de `envelope.schema.json` (aviso a los tres en T04; no rompe consumidores: filtran por tipo). Ejemplos en `contracts/examples/report/`.
- `serverless.yml`: servicio `fp-reports`, `provider.httpApi.id` de `fp-core`, capa de SSM, env `STATE_TABLE, REGISTRY_TABLE, OUTBOX_TABLE, INVOKER_SECRET, STATE_MACHINE_ARN`, memoria 256, timeout 15. Compose: `reports` dependsOn `[shared, core, auth, incidents]`.

### 3. Recomendación de chequeos (T33, `gcp/insights/`, Fabricio)
- `POST /recommend-checks` (privado, ID token como `/explain`). Entrada: `{report_id, machine_id, model, abnormality_type, description, severity_perceived, telemetry_snapshot{means, fault_codes, hours_total, hours_since_service}, oem_specs{ranges, service_intervals, typical_failures}, open_incident?{incident_id, failure_mode, severity}}`. Salida JSON estructurada (temperatura 0,2, mismo cliente `GeminiClient` con un segundo `build_prompt`): `{probable_causes[{cause, component, confidence}], checks[{order, step, tool, expected, if_fails}], urgency (observe|schedule|immediate), suggested_parts[PRT-##], safety_notes[], source, cached}`. Guardas: validación del JSON, repuestos filtrados al BOM del modelo, timeout 7 s, `fallback_recommendation(abnormality_type, model)` con el mismo catálogo que `FAILURE_CATALOG`. Caché por `report_id`.
- Usa `oem_specs.yaml` de T26 (Alex) si ya está; si no, los rangos de `ids.yaml` y una tabla mínima embebida. Tests con fakes (sin red).

### 4. Consola por roles (T34, `gcp/console/`, Alex)
- Login: `oidc-client-ts` contra Cognito Hosted UI (code + PKCE); el BFF lee `/fleetpulse/auth/*` de SSM por su API key actual (o se pasan como env al deploy) y expone `GET /api/auth/config`. El frontend guarda el `id_token` en memoria y lo envía en `Authorization` a `/api/reports*`; el BFF lo reenvía tal cual a AWS (`AWS_API_URL/reports…`) y mantiene la API key para las lecturas actuales.
- Enrutado por `custom:role`: **operator** → "Reportar anormalidad" (máquina, tipo, descripción, severidad percibida) y "Mis reportes" con la recomendación; **technician** → "Mis incidentes" y "Reportes asignados" con la lista de chequeos y botón "Cerrar reporte" (hallazgos, repuestos); **supervisor** → todo lo anterior en solo lectura + incidentes + aprobaciones pendientes (enlace del correo); **management** → pestaña Planificación/KPIs; **admin** → catálogo (técnicos: zona y disponibilidad; tipos de anormalidad).
- Panel del incidente: muestra los reportes vinculados y la recomendación del LLM junto al informe técnico de T29.

### 5. Administración (T35, `aws/reports/api/admin.py`, Benjamin)
- `GET/PUT /admin/technicians/{tech_id}` (zona, disponibilidad, especialidad) sobre `fp-state TECH#`; `GET/POST/DELETE /admin/abnormality-types` sobre `fp-registry CATALOG#abnormality_type/<code>` (semilla desde el enum del contrato); solo rol `admin`. Máquinas y repuestos quedan de solo lectura (vienen del catálogo versionado).

### 6. Cambios transversales
- `docs/architecture.md` y `docs/architecture.drawio`: añadir Cognito (AWS), `fp-reports`, `/recommend-checks` y el flujo operario → técnico; sección IA: enumerar explícitamente los dos módulos (ML anomalías / LLM) y dónde se ven en la demo.
- Guion del video: bloque nuevo de ~25 s (operario reporta "temperatura alta" en EX-017 desde el celular → el técnico ve 4 chequeos recomendados y el repuesto → se vincula al incidente de S1). Recortar 25 s de la lámina de arquitectura.
- Backlog: crear T31–T35 con dueños y horarios; mover T30 a `incremento`; comentar en T29 que el espectro se posterga.

## Archivos clave
- Nuevos: `aws/auth/serverless.yml`, `scripts/cognito-seed.sh`, `aws/reports/{serverless.yml,api/reports.py,api/admin.py,lib/snapshot.py,lib/recommend.py,lib/link_incident.py,tests/}`, `aws/shared/python/fplib/{authz.py,gcp_auth.py}`, `contracts/report.schema.json`, `contracts/examples/report/*.json`, `gcp/insights/app/recommend.py` (Fabricio), `gcp/console/web/src/{auth,pages/Report*,pages/Admin*}` (Alex).
- Modificados: `aws/ingest/serverless.yml` (autorizador JWT), `aws/serverless-compose.yml` (auth, reports), `aws/shared/build.sh` (copiar `oem_specs.yaml`), `aws/incidents/lib/gcp_auth.py` → import desde `fplib`, `contracts/envelope.schema.json` (enum), `gcp/insights/app/main.py` (ruta), `gcp/console/app/main.py` (reenvío de JWT), `docs/architecture.*`, `.env.example` (nada de secretos nuevos: la contraseña de los usuarios de prueba va por variable de entorno al seed).
- Reutilizar: `fplib.auth`, `fplib.outbox.transact_items`, `fplib.envelope.build`, `aws/incidents/lib/registry.py` (`open_incident_for`, `_transact`, `ConditionFailed`), `aws/incidents/lib/gcp_auth.py`, patrón de `aws/scoring/scoring/handler.py` para `start_execution`, `gcp/insights/app/{gemini,explain}.py` para el segundo prompt y el fallback.

## Riesgos y decisiones
- **Cognito en Learner Lab**: el User Pool no requiere roles IAM nuevos; si CloudFormation rechaza el dominio Hosted UI, usar el login directo con `InitiateAuth` (USER_PASSWORD_AUTH) desde el frontend. Verificar en la primera hora de T31.
- **Tiempo**: T34 es la tarea más larga; por eso T30 sale de M3. Si a las 12:00 la consola no tiene login, plan B: selector de rol sin contraseña (30 min) y Cognito queda como incremento.
- **Contrato del sobre**: añadir dos tipos al enum es un cambio de contrato post-`m1-contratos`; se avisa en T04 y se mergea con los tres al tanto.

## Verificación
1. `aws/auth` desplegado: `aws cognito-idp list-users --user-pool-id $(aws ssm get-parameter --name /fleetpulse/auth/user_pool_id --query Parameter.Value --output text)` lista 5 usuarios.
2. Token: `aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH --client-id … --auth-parameters USERNAME=operador@…,PASSWORD=…` devuelve un `IdToken` cuyo claim `custom:role` es `operator`.
3. `POST /reports` con ese token (curl) → 201 con `recommendation.checks` no vacío y `incident_id` vinculado o nuevo; `GET /reports/{id}` con token de técnico → 200; con token de operador ajeno → 403; `POST /reports/{id}/close` por el técnico → 200 y el incidente muestra el `EV#`.
4. Tests: `pytest -q aws/reports/tests aws/shared/tests` (moto; fakes para insights y Cognito claims), `pytest -q gcp/insights/tests`, consola `npm test` o smoke manual en `MOCK=1`.
5. `scripts/smoke.sh` gana la etapa `reports` (crear + leer + cerrar con tokens de prueba) y T17 la incluye en la prueba de punta a punta.
6. Deploy por compose en verde y, en la consola real, el recorrido operario → técnico → cierre grabable para el video.
