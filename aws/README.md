# AWS: despliegue

Servicios (cada uno con su `serverless.yml`), en el orden de `serverless-compose.yml`:
`shared` (layer) -> `core` (ingest: tablas, bucket, topics, API) -> `outbox-publisher`, `registry`,
`gcp-exporter`, `incidents` -> `scoring`, `planning` (regla diaria apagada) -> `simulator` (apagado).
`oci-writer` no existe (plan B).

## Desplegar
```bash
scripts/lab-creds.sh                      # credenciales del Learner Lab (--gh las sube a GitHub)
bash aws/shared/build.sh && cd aws && npm ci && npx serverless deploy
scripts/subscribe-notify.sh technician tu@correo.com   # tambien supervisor, management, operator
scripts/smoke.sh ingest
```
Confirma el correo de AWS tras cada suscripcion. `compose` no soporta `package`: para validar
un servicio, `cd aws/<servicio> && npx serverless@3 package --stage ci`.

## Lineas base del score (una vez tras el deploy)
```bash
scripts/drift.py off all                        # sin deriva mientras se siembra
.venv/bin/python scripts/seed-baselines.py --dry-run   # resumen, sin escribir
.venv/bin/python scripts/seed-baselines.py      # MACHINE#<id>/META.baseline con 24 h simuladas (~2 min)
```
Correrlo despues de `scripts/seed-registry.py` (necesita los META) y otra vez tras cualquier cambio de fisica del
simulador. Pisa las lineas base anteriores, tambien las de arranque en frio. Si el simulador ya corrio con el
score viejo, cerrar desde la consola los incidentes falsos que haya abierto.

## Simulador
```bash
scripts/simulator.sh on       # habilita la regla de EventBridge del stack fp-simulator-dev (un lote por minuto)
scripts/drift.py on S1        # inyecta deriva
scripts/simulator.sh off      # apaga (costo)
scripts/simulator.sh status   # estado de la regla
```
El deploy siempre deja la regla en `enabled: false` (compose 1.3.0 no acepta `--param`), y cuando CloudFormation
actualiza la regla la vuelve a deshabilitar. Por eso, durante la grabacion no se pushea nada a `aws/`, `simulator/`
ni `contracts/` (cada push despliega; `gh workflow disable deploy-aws` congela el deploy), y tras cualquier deploy
se corre `scripts/simulator.sh status`.

## daily-planning (incremento, T18)
```bash
scripts/planning.sh run --dry-run   # calcula ordenes preventivas y pedidos sin escribir
scripts/planning.sh run             # ejecucion de Step Functions; espera SUCCEEDED y muestra el resumen
scripts/planning.sh on|off|status   # regla diaria de las 05:00 Lima (el deploy la deja DISABLED)
```
Repetir `run` el mismo dia no duplica (reclamos `planning:<dia>:...` en fp-inbox). Las ordenes llegan a
`ORDER#` por outbox -> registry y los pedidos se escriben en `REORDER#`: `GET /orders` y `GET /reorders` de la
API de registry. Diseno: `docs/superpowers/plans/2026-11-15-t18-daily-planning.md`.

## Tests
Todos los `aws/**/tests` y `simulator/tests`, con el venv del repo (igual que el job python de CI):
```bash
.venv/bin/pytest -q $(find aws simulator -type d -name tests)
```
Los servicios de otros pueden necesitar su propio `requirements-dev.txt`.

## Usuarios y roles
Cognito (`aws/auth`, stack `fp-auth`) emite los JWT; el HTTP API `fp-api` los valida con el authorizer
`cognito` (hoy solo `GET /whoami`; `/telemetry` y `/context` siguen con `x-api-key`). Roles en `custom:role`:
`operator`, `technician` (con `custom:tech_id`), `supervisor`, `management`, `admin`.

Los roles los asigna solo un administrador con el AWS CLI; los usuarios no pueden cambiar su propio rol.

La consola debe enviar el **ID token** (el access token no trae `custom:role`). `auth` debe desplegarse antes que `core`: con los valores por defecto del authorizer (modo offline) `/whoami` responde 401. Orden: `auth` se despliega antes que `core` (ya esta en `serverless-compose.yml`). Luego los usuarios de prueba:
```bash
FP_TEST_PASSWORD='…' scripts/cognito-seed.sh     # operador@, tecnico@, supervisor@, direccion@, admin@example.com
TOKEN=$(aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH \
  --client-id $(aws ssm get-parameter --name /fleetpulse/auth/client_id --query Parameter.Value --output text) \
  --auth-parameters USERNAME=operador@example.com,PASSWORD=… --query AuthenticationResult.IdToken --output text)
curl -H "Authorization: Bearer $TOKEN" $API/whoami
```
`$API` es el valor de `/fleetpulse/api/url`. La URL de la consola para los callbacks se cambia con
`--param="consoleUrl=https://…/"`.
