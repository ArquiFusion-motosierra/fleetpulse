# T18 daily-planning (incremento 1): órdenes preventivas y pedidos sugeridos

**Listo cuando:** ejecución manual verde que escribe órdenes preventivas y pedidos sugeridos en `fp-registry`.
Es un incremento: se mergea solo con M3 verde y la regla diaria queda **deshabilitada** hasta que el equipo la encienda.

## Alcance

Del flujo completo del documento de diseño (`ExportPending → RefreshModels → Analyze → ComputeReorders →
UpdateBaselines → Publish`) este incremento implementa la parte que no depende de GCP: preventivo por uso real
(problema 2) y reorden contra el stock de `fp-registry` (problema 3, sin pronóstico ARIMA). Los pasos de GCP
(`/forecast/parts`, `/kpis/daily`, `/baselines`, T20) se agregan después como estados nuevos de la misma máquina.

Servicio `aws/planning` (stack `fp-planning`):

- Step Functions **Standard** `daily-planning`: `PlanPreventive` (Lambda `plan`, reintento 3× base 2 s) y `Catch`
  global → SNS `fp-alerts` → `Fail`, igual que `incident-flow`.
- Regla de EventBridge `cron(0 10 * * ? *)` (05:00 Lima) → la máquina de estados con `LabRole`. Queda
  `DISABLED` por defecto (`params.default.schedule: DISABLED`); `scripts/planning.sh on|off|status` la cambia.
- Sin servicios nuevos ni GCP: Lambda, Step Functions, EventBridge, DynamoDB y el outbox que ya existen.

## Entradas

| Dato | De dónde |
|---|---|
| Flota y horómetro | `fp-state` `MACHINE#<id>/META` por zona (GSI1 `ZONE#<zona>`): `model, zone_id, hours_total, status` |
| Último servicio | `META.last_service_h` si existe; si no, `ids.yaml`: `hours_total - hours_since_service` del catálogo |
| Intervalo del fabricante | `ids.yaml` `models[].service_interval_h` |
| Kit de repuestos | `ids.yaml` `bom`: cada línea con `replace_every_h` cuyo múltiplo cae entre el último servicio y el próximo |
| Uso diario | lectura `R#` más antigua de los últimos 3 días (TTL de las lecturas) contra `META`; si no hay, 12 h/día; acotado a [1, 24] |
| Riesgo | `META.status` como factor de desgaste: ok 1,0 · observe 1,1 · schedule 1,25 · immediate 1,5 |
| Stock | `fp-registry` `STOCK#<part>#<zone>/META`: `available, reorder_point` |
| Órdenes abiertas | `fp-registry` GSI1 `ORDERS` con `status = suggested` |
| Pedidos en curso | `fp-registry` GSI1 `REORDERS` creados en los últimos 14 días (cualquier origen) |

Solo se planifican las máquinas que existen en `fp-state` (lo que sembró `seed-registry.py` o escribió ingest).

## Cálculo

Por máquina, con `f` = factor de desgaste y `r` = horas por día:

```
desde      = hours_total - last_service_h
restante   = service_interval_h - desde × f
dias       = max(restante, 0) / (r × f)
vence      = dia_lima + floor(dias)        (08:00 Lima = 13:00Z; contrato maintenance.order)
```

Si `dias <= horizonte` (14 por defecto) la máquina entra al plan; el motivo dice si vence en 7 o en 14 días.
Esto es la fórmula del diseño (horas desde el servicio × factor × (1 + score)) con el score de 7 días reemplazado
por la severidad actual: los `S#` duran 3 días y son demasiados para promediarlos en cada corrida.

Demanda de repuestos a 14 días por (repuesto, zona de la máquina) = suma de los kits de las máquinas del plan,
tengan o no una orden abierta. Para cada fila de stock y cada (repuesto, zona) con demanda:

```
proyectado = available + en_pedido_14d - demanda_14d
si proyectado < reorder_point: pedido = reorder_point - proyectado
```

## Escrituras e idempotencia

| Qué | Dónde | Cómo |
|---|---|---|
| Orden preventiva | `maintenance.order.suggested` en `fp-outbox` (`source: aws.daily-planning`, `aggregate_id: ORD-<maquina>-<YYYYMMDD>`), validada contra `maintenance.order.schema.json` | `registry` (Alex) ya la materializa en `ORDER#<maquina>/<due_at>` (GSI1 `ORDERS`): un solo escritor de `ORDER#`, como fija el contrato |
| Pedido sugerido | `REORDER#<part>#<zone>/<created_at>` (GSI1 `REORDERS`), `source: planning`, `forecast_14d` = demanda preventiva | Directo: no hay tipo de evento para un pedido sin incidente y `registry` no se toca |
| Reclamo | `fp-inbox` `planning:<dia>:ORDER#<maquina>` y `planning:<dia>:REORDER#<part>#<zone>`, TTL 7 días | En la **misma** `TransactWriteItems` que el outbox o el `REORDER#`, con `attribute_not_exists` |

- Repetir la corrida el mismo día no duplica nada: el reclamo de `fp-inbox` cancela la transacción, y el pedido de
  la primera corrida ya cuenta como `en_pedido`.
- En días siguientes una máquina con una orden `suggested` no recibe otra; el pedido de ayer cuenta como en curso.
- `{"dry_run": true}` calcula y devuelve el plan sin escribir. `{"horizon_days": N}` cambia el horizonte.

## Demo manual (aceptación)

```bash
scripts/planning.sh run            # inicia daily-planning y espera el resultado (SUCCEEDED + resumen)
scripts/planning.sh run --dry-run  # solo calcula
curl -H "x-api-key: $KEY" "$REGISTRY/orders"; curl -H "x-api-key: $KEY" "$REGISTRY/reorders"
```

## Pruebas

`aws/planning/tests` con moto (`fp-state`, `fp-registry`, `fp-outbox`, `fp-inbox`): vence por horas, no vence,
stock suficiente contra faltante → pedido, repetir el mismo día, contrato del evento, registro vacío, orden abierta,
pedido en curso, `dry_run`, y el cableado de la ASL y del `serverless.yml`.
