# Plan: multi-tenancy por parque (mineria, agro, industria)

## Contexto

Hasta el sábado FleetPulse modelaba **una** operación ("Operación Cerro Azul", `contracts/ids.yaml`). NORTE, CENTRO y SUR son áreas geográficas dentro de esa operación, no clientes. No existe el concepto de tenant: Cognito solo tiene `custom:role` y `custom:tech_id`, las claves de DynamoDB (`MACHINE#`, `ZONE#`, `INCIDENT#`, `REPORT#`, `STOCK#`) no llevan prefijo de tenant, hay un solo dataset de BigQuery y un solo fan-out SNS.

El jurado mira sobre todo la **infraestructura**. Por eso el multi-tenancy tiene que verse en el IaC (Cognito, autorizador, diseño de claves y GSI de DynamoDB, atributos y filtros de SNS, prefijos de S3, datasets por tenant en BigQuery, diagrama), no solo como un campo en el código.

Restricciones que mandan sobre el diseño:

- **Learner Lab**: todo corre con `LabRole`; no se pueden crear roles IAM. Descarta aislar con políticas IAM por tenant (`dynamodb:LeadingKeys`, roles asumidos por tenant). Las palancas reales son Cognito (grupos y claims), el autorizador JWT, recursos por tenant y el filtrado en la capa de API.
- **El stack ya está desplegado y el MVP debe seguir andando**. Todo dato o evento sin `tenant_id` es de `mineria`.
- **Costo**: GCP Free Trial sin upgrade, BigQuery solo con load jobs (nunca streaming), sin Vertex AI, Compute Engine, Cloud SQL ni load balancers. Las vistas autorizadas no cuestan nada en reposo.

## 1. Modelo de tenants

| `tenant_id` | Parque | Operación | Zonas | Modelos | Técnicos |
|---|---|---|---|---|---|
| `mineria` (por defecto) | Parque minero | Operación Cerro Azul (Apurímac) | `NORTE`, `CENTRO`, `SUR` | `EX`, `CF`, `VQ`, `MN`, `TO` (100 máquinas) | `TEC-01..06` |
| `agro` | Parque agrícola | Fundo Valle Sur (Ica) | `FUNDO-A`, `FUNDO-B`, `PACKING` | `TA` tractor agrícola, `CS` cosechadora (12 máquinas) | `TEC-11..14` |
| `industria` | Parque industrial | Planta Lurín (Lima) | `PLANTA-1`, `PLANTA-2`, `ALMACEN` | `MC` montacargas, `CP` compresor de tornillo, `GE` grupo electrógeno (12 máquinas) | `TEC-21..24` |

- `tenant_id`: `^[a-z][a-z0-9-]{1,30}$`. Es el único identificador de tenant en las dos nubes (claim de Cognito, sobre de eventos, atributo de DynamoDB, atributo SNS, columna de BigQuery, segmento de S3).
- **Los IDs de negocio son únicos entre tenants** (prefijos de modelo, zonas, técnicos y repuestos distintos por parque). Por eso `incident_id`, `report_id`, `RSV-` y las claves `MACHINE#<id>` siguen siendo globalmente únicos sin cambiar su formato, y **el tenant de cualquier entidad se deduce del catálogo** (máquina → tenant, zona → tenant). Esa es la red de seguridad de la compatibilidad hacia atrás.
- Cada parque tiene su propio catálogo con la misma forma que `ids.yaml` (zonas, estaciones, modelos, máquinas, técnicos, repuestos, BOM, stock inicial). Los repuestos nuevos van de `PRT-14` en adelante; el stock vive por zona (`STOCK#<part>#<zone>`), así que queda separado por parque sin cambiar la clave.

### Archivos de contrato

- `contracts/tenants.yaml`: registro de tenants (`tenant_id`, nombre, operación, zona horaria, centro del mapa, archivo de catálogo, `default: true` para `mineria`).
- `contracts/ids.yaml`: **sigue siendo** el catálogo de `mineria` (congelado con `m1-contratos`; solo gana la línea `tenant_id: mineria`).
- `contracts/tenants/agro.yaml`, `contracts/tenants/industria.yaml`: catálogos de los parques nuevos, misma forma que `ids.yaml`.
- `contracts/oem_specs.yaml`: fichas ilustrativas de los 5 modelos nuevos (rangos, plan de servicio, fallas típicas con su BOM).
- Los patrones de `machine_id`/`incident_id`/`report_id` de los esquemas pasan a `(EX|CF|VQ|MN|TO|TA|CS|MC|CP|GE)`; los `enum` de zonas y modelos se amplían. `envelope.schema.json` gana `tenant_id` (opcional en el contrato para no romper eventos viejos; `fplib.envelope.build` siempre lo escribe).
- `fplib.ids.load()` devuelve la **unión** de los tres catálogos y cada máquina, zona, técnico y estación lleva `tenant_id`. Quien hoy itera `ids.machines()`/`ids.zones()` sigue funcionando; quien necesita un parque usa `ids.zones(tenant)`, `ids.machines(tenant)`, `ids.technicians(tenant)`. `build.sh` empaqueta el resultado en la layer como `data/ids.json` (la layer no trae PyYAML).

## 2. Roles

Los cinco roles de hoy pasan a estar **acotados a un parque**. Se agrega un rol transversal.

| Rol (`custom:role`) | Alcance | Qué ve | Qué hace |
|---|---|---|---|
| `operator` | su parque | sus reportes, mapa de su parque | crea reportes de máquinas de su parque |
| `technician` | su parque | sus incidentes y reportes asignados, stock de su parque | cierra lo suyo |
| `supervisor` | su parque | todo su parque | cierra incidentes y reportes de su parque, aprueba |
| `management` | su parque | planificación y KPIs de su parque | — |
| `admin` (administrador del parque) | su parque | todo su parque | técnicos y catálogo de anormalidades **de su parque** |
| `maintenance_head` (jefe de mantenimiento) | **todos** | todos los parques, con selector de parque o vista consolidada | lo mismo que `supervisor` en cualquier parque; no administra técnicos de un parque |

- Un usuario de parque que pide datos de otro parque recibe **403** (`TENANT_FORBIDDEN`), nunca una lista vacía. Un recurso de otro parque pedido por ID responde **404** para no filtrar su existencia (máquina, incidente, reporte).
- `maintenance_head` sin selector ve la unión; con `?tenant=agro` ve solo ese parque.

## 3. Cómo viaja el tenant

### Cognito (`aws/auth/serverless.yml`)

**Decisión: grupos de Cognito, no un atributo `custom:tenant_id`.**

- Grupos `AWS::Cognito::UserPoolGroup` en el IaC: `tenant-mineria`, `tenant-agro`, `tenant-industria` y `maintenance-head` (precedencia 0). El ID token los trae en `cognito:groups`; el autorizador JWT de la HTTP API los pasa a la Lambda.
- Por qué grupos: (1) solo un administrador IAM puede meter a alguien en un grupo (`AdminAddUserToGroup`), igual de cerrado que un atributo no escribible; (2) **no toca el `Schema` del user pool ya desplegado**: agregar un atributo custom a un pool existente por CloudFormation es la operación con más riesgo de terminar en reemplazo (nuevo pool, nuevo issuer, consola y autorizador rotos a horas de la entrega); (3) los grupos se ven como recursos de primer nivel en la consola de Cognito y en el diagrama; (4) un grupo puede llevar después un rol IAM por tenant (Identity Pool) cuando no estemos en Learner Lab.
- Compatibilidad: un usuario sin grupo de tenant (los 5 sembrados el sábado) es de `mineria`. `maintenance_head` se reconoce por `custom:role = maintenance_head` **y** por el grupo `maintenance-head` (cualquiera de los dos; el seed pone ambos).
- `scripts/cognito-seed.sh` (lo corre el usuario, no CI): mantiene los 5 usuarios actuales (`mineria`) y suma `admin.agro@`, `supervisor.agro@`, `operador.agro@`, `tecnico.agro@` (`TEC-11`), `admin.industria@`, `supervisor.industria@`, `operador.industria@`, `tecnico.industria@` (`TEC-21`), `admin.mineria@` y `jefe.mantenimiento@` (`maintenance_head`).

### Autorización (`fplib.authz`)

- `tenant_of(event) -> str | None`: tenant del usuario a partir de `cognito:groups` (`tenant-<id>`); `None` si es transversal; `mineria` si no tiene grupo. Tolera el formato con el que la HTTP API aplana arreglos (`"[tenant-agro maintenance-head]"`).
- `is_global(event)`: `maintenance_head`.
- `scope(event, requested=None) -> tuple[str, ...]`: parques que la petición puede ver. Usuario de parque: su parque (si `requested` es otro, `TenantForbidden`). Transversal: `requested` o todos.
- `require_tenant_access(event, resource_tenant)`: `TenantForbidden` si el recurso es de otro parque.
- `TenantForbidden` es subclase de `Forbidden`: los handlers que ya traducen `Forbidden` a 403 no cambian.

### Sobre de eventos y telemetría

- `envelope.tenant_id`: lo escribe `fplib.envelope.build` (explícito, o deducido de `machine_id`, o `mineria`).
- Lote de telemetría: `tenant_id` opcional a nivel de lote. `ingest` deduce el tenant de cada máquina por catálogo y **rechaza con 400 (`TENANT_MISMATCH`)** un lote que declara un tenant y trae máquinas de otro: un gateway de un parque no puede inyectar lecturas en otro.
- `outbox-publisher` publica en `fp-domain-events` con el atributo SNS `tenant_id` (además de `event_type`), así cualquier suscriptor puede filtrar por parque.

## 4. DynamoDB: atributo `tenant_id` + GSI por tenant (sin re-clavear)

**Opciones evaluadas**

| Opción | Pro | Contra |
|---|---|---|
| A. Prefijo en la clave (`T#<tenant>#MACHINE#...`) | Aislamiento por clave, apto para `LeadingKeys` | Re-clavear todas las tablas en caliente, migrar `fp-state`/`fp-registry` con stream activo, tocar cada consulta de 8 stacks y del ASL; `LeadingKeys` no aplica con `LabRole` |
| B. Tabla por tenant | Aislamiento físico | Multiplica tablas, streams y consumidores; nombres fijos en todo el código |
| **C. Atributo `tenant_id` en cada ítem + GSI3 `TENANT#<tenant>` (elegida)** | Cero migración de claves; los IDs ya son únicos; los ítems viejos siguen válidos (tenant por catálogo); el GSI da la consulta por parque | El aislamiento lo impone la capa de API (no IAM), igual que con A en Learner Lab |

- `fp-registry` gana **`GSI3`** (`GSI3PK = TENANT#<tenant>`, `GSI3SK = <TIPO>#<ts>`), proyección ALL. Lo escriben incidentes (`INC#<opened_at>`) y reportes (`RPT#<reported_at>`). Es el índice "todo lo de un parque".
- `fp-state`: META de máquina y `TECH#` llevan `tenant_id`; la consulta por parque usa el GSI1 existente por zona (las zonas pertenecen a un parque), así que no hace falta otro índice.
- Ítems viejos sin `tenant_id`: se tratan como del tenant del catálogo de su máquina/zona (`mineria`). `scripts/backfill-tenant.py` (lo corre el usuario, idempotente, `--dry-run`) estampa `tenant_id` y `GSI3*` en incidentes y reportes existentes. Mientras no se corra, la API de `mineria` une los incidentes sin `tenant_id` del GSI2 con los del GSI3.
- CloudFormation crea un solo GSI por tabla por despliegue: este cambio agrega **uno** en `fp-registry` y ninguno en `fp-state`.

## 5. Infraestructura por tenant

| Capa | Recurso | Por tenant |
|---|---|---|
| Identidad | Cognito `fleetpulse-users` | grupos `tenant-mineria`, `tenant-agro`, `tenant-industria`, `maintenance-head` |
| Configuración | SSM | `/fleetpulse/tenants/<tenant>` (nombre, operación, zonas) y `/fleetpulse/tenants` (lista) |
| Eventos | SNS `fp-domain-events` | atributo `tenant_id` en cada mensaje |
| Avisos | SNS `fp-notify` | atributo `tenant_id` en cada aviso; `scripts/subscribe-notify.sh <rol> <correo> [tenant]` suscribe con `FilterPolicy {role, tenant_id}`; el jefe de mantenimiento se suscribe sin filtro de tenant |
| Datos | DynamoDB `fp-registry` | `GSI3` `TENANT#<tenant>` |
| Crudo | S3 `fp-raw-<cuenta>` | `telemetry/dt=/hr=/tenant=<tenant>/…` (subpartición Hive: no rompe el lifecycle por prefijo `telemetry/` ni el exportador, que lee `dt=`/`hr=`) |
| Analítica | BigQuery | columna `tenant_id` en `telemetry` y `domain_events`; datasets `fp_mineria`, `fp_agro`, `fp_industria` con **vistas autorizadas** `telemetry` y `domain_events` filtradas por tenant (`COALESCE(tenant_id, 'mineria')`), y lectores por dataset (`var.tenant_readers`) |
| Consola | Cloud Run `console` | badge del parque, selector de parque para el jefe de mantenimiento; el BFF resuelve el parque del token |

Vistas autorizadas y no row-level security: funcionan con la versión de proveedor fijada (`~> 6.0`), no requieren reescribir consultas de `insights` y un lector de `fp_agro` no puede consultar `fleetpulse` directamente. Row access policies quedan como siguiente paso.

## 6. Aislamiento en las APIs

- **API de lectura `registry`** (API key de la consola): el BFF es el único que tiene la llave y le pasa `?tenant=<id>` (uno o varios separados por coma). Sin `tenant`, responde `mineria` (compatibilidad). Cada ruta filtra: máquinas por las zonas del parque, incidentes por GSI3 (+ legado), stock por zonas del parque, órdenes y pedidos por máquina/zona, KPIs por máquina; `GET /machines/{id}` y `GET /incidents/{id}` de otro parque responden 404.
- **BFF de la consola**: con el ID token llama `GET /whoami` en la HTTP API (el autorizador JWT de API Gateway valida la firma), cachea 5 min por token y decide el alcance; el navegador nunca elige su parque salvo el jefe de mantenimiento, cuyo `?tenant=` el BFF solo acepta si `whoami` dice `maintenance_head`.
- **`/whoami`** devuelve también `tenant_id`, `tenants` (alcance) y `groups`.
- **Reportes** (`aws/reports`): crear exige que la máquina sea del parque del usuario; listar filtra por alcance; ver y cerrar validan el parque del reporte. El reporte guarda `tenant_id` y `GSI3`.
- **Administración**: el `admin` de un parque solo lista y edita técnicos de su parque, y solo puede moverlos a zonas de su parque. El catálogo de tipos de anormalidad es compartido (se documenta como pendiente partirlo por parque).
- **Cierre de incidentes** (`POST /me/incidents/{id}/close`): valida que el incidente sea del parque del usuario; `maintenance_head` puede cerrar en cualquier parque.
- **incident-flow**: el tenant va en la entrada (lo pone `score`), el ranking de técnicos solo considera técnicos del parque de la máquina, el incidente y sus eventos llevan `tenant_id`, y los avisos SNS llevan el atributo `tenant_id`.

## 7. Simulador

- `fpsim` simula los tres parques (124 máquinas): física por modelo para los 5 nuevos (montacargas eléctrico-diésel liviano, compresor y grupo electrógeno estacionarios con rpm fija), zonas y estaciones por parque. Los escenarios S1–S7 siguen en `mineria`; se agrega uno por parque nuevo para la demo (S8 sobrecalentamiento de cosechadora `CS-003` en agro; S9 vibración de compresor `CP-002` en industria).
- `FP_TENANTS=mineria,agro,industria` (default todos) para apagar parques sin tocar código.

## 8. Consola

- Badge con el nombre del parque en la cabecera (en lugar de "Operación Cerro Azul" fijo).
- Jefe de mantenimiento: selector "Todos los parques / Minería / Agro / Industria" que el BFF traduce a `?tenant=`; el mapa encuadra el parque elegido.
- Pestañas del jefe: mapa, incidentes, reportes, stock, planificación.

## 9. Pruebas de aislamiento

- `fplib.authz`: grupos en lista y aplanados, usuario sin grupo = `mineria`, transversal, `scope` con tenant ajeno = 403.
- `registry` API: un tenant no ve máquinas, incidentes ni stock de otro; detalle de otro parque = 404; legado sin `tenant_id` aparece en `mineria`.
- `reports`: operador de `agro` no puede reportar `EX-017` (403); listado filtrado; cierre cruzado = 403.
- `admin`: admin de `agro` no ve `TEC-01` ni puede moverlo a `NORTE`.
- `ingest`: `TENANT_MISMATCH`; lecturas con `tenant_id`; clave S3 con `tenant=`.
- `publisher`: atributo SNS `tenant_id`.
- Consola: `whoami` simulado, selector solo para el jefe.
- Catálogo: cada máquina, zona y técnico pertenece a exactamente un tenant; BOM de modelos nuevos referencia repuestos existentes; `oem_specs` cubre los 10 modelos.

## 10. Migración y orden de despliegue

1. Merge a `main` → GitHub Actions despliega los stacks AWS por compose (`auth` crea los grupos; `core` agrega `GSI3` a `fp-registry`; el resto toma la nueva layer). Todo es aditivo: atributos nuevos, un GSI nuevo, grupos nuevos, atributos SNS nuevos.
2. Usuario: `FP_TEST_PASSWORD=... scripts/cognito-seed.sh` (usuarios por parque y jefe de mantenimiento).
3. Usuario: `python scripts/seed-registry.py` (stock y máquinas/técnicos de agro e industria; idempotente) y `python scripts/seed-baselines.py` (líneas base de las máquinas nuevas).
4. Usuario: `python scripts/backfill-tenant.py --dry-run` y luego sin `--dry-run`.
5. Fabricio / usuario: `terraform apply` en `gcp/terraform` (columna `tenant_id`, datasets por tenant, vistas autorizadas).
6. Redeploy de la consola (`gcp/console/deploy.sh`) con `AWS_INCIDENTS_URL` ya configurado (lo usa para `/whoami`).

Si el despliegue de la consola llega después que el de AWS, la consola vieja sigue viendo `mineria` (la API de lectura sin `tenant` responde el parque por defecto).

## 11. Lo que NO se hace

- Aislamiento por IAM (`LeadingKeys`, roles por tenant): no se puede en Learner Lab; se documenta como el siguiente paso en producción.
- API keys de ingesta por parque: hoy hay una sola `ingest-key`; el tenant se valida contra el catálogo.
- Catálogo de tipos de anormalidad por parque (sigue compartido).
- Modelos de BigQuery ML por parque: `m_peer_clusters` y `m_fuel_trend` siguen sobre toda la flota (los pares se agrupan igual por modelo, y los modelos no se repiten entre parques).
- Row access policies en BigQuery.
- Facturación o cuotas por tenant.
