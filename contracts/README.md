# Contratos (T04)

Fuente de verdad de lo que cruza entre nubes. Despues del tag `m1-contratos` cualquier cambio necesita aprobacion de los tres.

| Archivo | Contenido |
|---|---|
| `telemetry-batch.schema.json` | Lote de lecturas simulador -> API de ingesta |
| `incident.schema.json` | `incident.created`, `incident.assigned`, `incident.closed` |
| `part.schema.json` | `part.reserved`, `part.consumed` |
| `machine.snapshot.schema.json` | Estado por maquina para APEX |
| `kpi.daily.schema.json` | KPIs diarios desde BigQuery |
| `fuel.refill.schema.json` | Recargas de combustible |
| `ids.yaml` | Maquinas, zonas, tecnicos y SKUs fijos |
| `secrets.md` | Nombres de secretos y endpoints por nube (nunca valores) |

Todo evento lleva `event_id` (UUID, idempotencia), `event_type`, `occurred_at` (ISO 8601 UTC) y `payload`.
