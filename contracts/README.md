# Contratos (T04)

Fuente de verdad de lo que cruza entre nubes. Se congelan con el tag `m1-contratos`; despues solo cambian con revision de los tres. CI valida cada `*.schema.json` y cada ejemplo en `examples/<nombre>/`.

| Archivo | Contenido | Productor → consumidor |
|---|---|---|
| `telemetry-batch.schema.json` | Lote de 10 lecturas por maquina (AEMP / ISO 15143-3 + sensores) | simulator → `POST /telemetry` |
| `envelope.schema.json` | Sobre comun de todo evento de dominio | outbox → SNS `fp-domain-events` |
| `incident.schema.json` | `incident.opened`, `incident.assigned`, `incident.closed` | incident-flow → OCI, BigQuery |
| `part.schema.json` | `part.reserve.requested`, `part.reserved`, `part.shortage` | incident-flow ↔ OCI |
| `machine.snapshot.schema.json` | Estado y posicion de maquinas que cambiaron | snapshot-publisher → ORDS `POST /machines/snapshot` |
| `kpi.daily.schema.json` | KPIs de flota por dia y maquina | daily-planning → OCI `FLEET_KPIS_DAILY` |
| `fuel.refill.schema.json` | Recarga con lote de combustible por zona | simulator → contexto de CorrelateZone |
| `ids.yaml` | Maquinas, zonas, tecnicos, repuestos y BOM fijos | seed de OCI, DynamoDB y simulador |
| `secrets.md` | Nombres de secretos y endpoints por nube (nunca valores) | `push-secrets.sh` |

Todo evento de dominio lleva `event_id` (UUID v4, idempotencia), `event_type`, `aggregate_id`, `occurred_at` (ISO 8601 UTC), `incident_id` cuando aplica y `payload`.
