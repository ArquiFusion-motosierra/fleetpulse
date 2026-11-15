# Contratos (T04, plan B: AWS + GCP)

Fuente de verdad de lo que cruza entre nubes. Se congelan con el tag `m1-contratos`; despues solo cambian con revision de los tres. CI valida cada `*.schema.json` y cada ejemplo en `examples/<nombre>/`.

| Archivo | Contenido | Productor → consumidor |
|---|---|---|
| `telemetry-batch.schema.json` | Lote de 10 lecturas por maquina (AEMP / ISO 15143-3 + sensores) | simulator → `POST /telemetry` |
| `envelope.schema.json` | Sobre comun de todo evento de dominio | outbox → SNS `fp-domain-events` |
| `incident.schema.json` | `incident.opened`, `incident.escalated`, `incident.assigned`, `incident.closed` | incident-flow → registry (`fp-registry`), BigQuery |
| `part.schema.json` | `part.reserve.requested`, `part.reserved`, `part.shortage` | incident-flow → registry → outbox |
| `kpi.daily.schema.json` | KPIs de flota por dia y maquina | daily-planning → registry `KPI#<day>`, BigQuery |
| `maintenance.order.schema.json` | `maintenance.order.suggested`: orden preventiva o correctiva (propuesta para T18, la confirma Benjamin) | daily-planning → registry `ORDER#`, consola |
| `fuel.refill.schema.json` | Contexto por zona: recarga con lote de combustible y clima | simulator → `POST /context` → CorrelateZone |
| `report.schema.json` | Reportes de anormalidad del operario (T32): cuerpos de `POST /reports` (`$defs/create`) y `POST /reports/{id}/close` (`$defs/close`), eventos `report.created` y `report.closed` | `reports` (aws/reports) → outbox → BigQuery `domain_events`; consola (T34) |
| `ids.yaml` | Maquinas, zonas, estaciones de combustible, tecnicos, repuestos, BOM y stock inicial | `seed-registry.py` (fp-registry, fp-state) y simulador |
| `secrets.md` | Nombres de secretos, modelo de `fp-registry`, API de lectura de la consola y endpoints (nunca valores) | `push-secrets.sh`, consola |

## Convenciones fijadas

- `incident_id` = `INC-<machine_id>-<inicio de la ventana de 15 min UTC>` (ej. `INC-EX-017-20261115T1430`), igual al nombre de ejecución de `incident-flow`. Mismo valor en `fp-state`, `fp-registry`, BigQuery y la consola.
- `group_id` = `GRP-<geohash6>-<hora UTC>`; `batch_id` = `B-<sent_at>-<8 hex>`; solicitud de reserva = `RSV-<incident_id>`; cada línea reservada = `<event_id>#<línea>`.
- Severidad: `observe` (≥ 1,2), `schedule` (≥ 2,0), `immediate` (≥ 3,5). En el mapa se suma `ok`.
- IDs: máquinas `EX-001..020`, `CF-031..050` (para que exista CF-042 del escenario S3), `VQ-001..040`, `MN-001..012`, `TO-001..008`; zonas `NORTE`, `CENTRO`, `SUR`; técnicos `TEC-01..06`; repuestos `PRT-01..12`. Todo en `ids.yaml`.
- `report_id` = `RPT-<machine_id>-<reported_at UTC como YYYYMMDDTHHMMSS>` (ej. `RPT-EX-017-20261115T144012`). `report.created` / `report.closed` llevan `aggregate_type: report`, `source: aws.reports-api` e `incident_id` en el sobre cuando el reporte quedó vinculado (o inició) un incidente. `registry` no los consume (su cola filtra por tipo).
- `abnormality_type` es un código del catálogo `fp-registry` `CATALOG#abnormality_type` (lo edita el rol admin); el contrato fija la forma del código y la semilla (`$defs/abnormality_type_seed`). En la línea de tiempo del incidente, `report.linked` es solo un `EV#` (no sale por el outbox, por eso no está en el enum del sobre).
- `maintenance.order.suggested` tiene contrato propuesto (`maintenance.order.schema.json`); `fuel.anomaly` (incremento T18) sigue solo en el enum del sobre.
- Validar en local: `python -m check_jsonschema --schemafile contracts/incident.schema.json contracts/examples/incident/*.json`.

Todo evento de dominio lleva `event_id` (UUID v4, idempotencia), `event_type`, `aggregate_id`, `occurred_at` (ISO 8601 UTC), `incident_id` cuando aplica y `payload`.
