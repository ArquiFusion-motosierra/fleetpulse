# FleetPulse — ArquiFusion Corp
Telemetría de maquinaria pesada → decisiones de mantenimiento, técnicos y repuestos. 100 % serverless en AWS + GCP.

▶ Video (4:30): _pendiente_

![Arquitectura](docs/architecture.png)

| Nube | Rol | Servicios |
|---|---|---|
| AWS | Ingesta, detección, orquestación, registro operativo, secretos | API Gateway, Lambda, DynamoDB, S3, SQS, SNS, Step Functions, Secrets Manager |
| GCP | Analítica histórica, IA y consola operativa | Cloud Storage, BigQuery, BigQuery ML, Cloud Run, Gemini, Secret Manager |

**Multi-tenant por parque**: `mineria` (por defecto), `agro` e `industria`, cada uno con sus máquinas, zonas, técnicos y stock. El admin de un parque solo ve el suyo; el jefe de mantenimiento ve todos. El parque viaja en grupos de Cognito, `tenant_id` en eventos y DynamoDB (GSI3), atributos SNS, prefijos S3 `tenant=` y datasets BigQuery `fp_<parque>`. Ver [docs/architecture.md](docs/architecture.md#multi-tenancy-por-parque).

## Cómo evaluar

Consola: **https://console-qgbfzxdq2a-ue.a.run.app** · Contraseña de todas las cuentas: `LaMotosierra123`

En la pantalla de ingreso, un clic en un correo lo copia y abre el login de Cognito. Para cambiar de rol o de parque:
**Cambiar de rol / Salir** (abajo en la barra lateral, o en **Más** en el celular) y entrar con otra cuenta.

| Rol | Parque minero | Parque agrícola | Parque industrial |
|---|---|---|---|
| Operario | `operador@example.com` | `operador.agro@example.com` | `operador.industria@example.com` |
| Técnico | `tecnico@example.com` (TEC-01) | `tecnico.agro@example.com` (TEC-11) | `tecnico.industria@example.com` (TEC-21) |
| Supervisor | `supervisor@example.com` | `supervisor.agro@example.com` | `supervisor.industria@example.com` |
| Dirección | `direccion@example.com` | `direccion.agro@example.com` | `direccion.industria@example.com` |
| Administrador | `admin@example.com`, `admin.mineria@example.com` | `admin.agro@example.com` | `admin.industria@example.com` |
| Jefe de mantenimiento | `jefe.mantenimiento@example.com` (los tres parques) | | |

Qué ve cada rol al entrar (Inicio):
- **Operario**: botón grande para reportar una falla, sus últimos reportes y las máquinas con alerta de su parque.
- **Técnico**: sus incidentes y reportes asignados, el siguiente trabajo y los repuestos ya reservados para él.
- **Supervisor**: KPI del parque, lo que requiere atención (inmediatos sin técnico, aprobaciones pendientes, reportes sin revisar), mapa y stock bajo.
- **Dirección**: KPI del último día con datos e incidentes abiertos por severidad.
- **Administrador**: lo del supervisor más Administración (técnicos y tipos de anormalidad).
- **Jefe de mantenimiento**: compara los tres parques en una tabla; "Ver parque" filtra toda la consola a uno. Es la mejor cuenta para ver el multi-tenancy: un admin de parque solo ve el suyo.

## Desplegar
1. `scripts/lab-creds.sh`
2. `terraform -chdir=gcp/terraform apply`
3. `bash aws/shared/build.sh && (cd aws && npm ci && npx serverless deploy)`: el stack fp-core genera las API keys `fleetpulse/api/ingest-key` y `fleetpulse/api/console-key` (único dueño)
4. `scripts/push-secrets.sh` (solo las llaves de GCP `fleetpulse/gcp/sa-*` de `.env.example`)
5. Copiar la console key a GCP Secret Manager `aws-console-api-key` (la lee el BFF de la consola; el permiso de `fp-console` está en `gcp/console/deploy.sh`):
   ```bash
   aws secretsmanager get-secret-value --secret-id fleetpulse/api/console-key --query SecretString --output text \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"], end="")' \
     | gcloud secrets create aws-console-api-key --project fleetpulse-g19-2026 --replication-policy automatic --data-file=-
   ```
   Si el secreto ya existe (o fp-core se recreó), el mismo pipe termina en `gcloud secrets versions add aws-console-api-key --project fleetpulse-g19-2026 --data-file=-`.
6. `scripts/seed-registry.py` (catálogo de los tres parques, `contracts/tenants.yaml`) y, una vez tras el deploy del multi-tenancy, `python scripts/backfill_tenant.py --dry-run` y luego sin `--dry-run`
6b. `FP_TEST_PASSWORD=... scripts/cognito-seed.sh` (usuarios por parque y `jefe.mantenimiento@`)
7. `scripts/seed-baselines.py` (líneas base del score con 24 h simuladas; con `scripts/drift.py off all`)
8. `scripts/smoke.sh`
9. `scripts/simulator.sh on` (`off` al terminar, `status` para ver). Cada deploy deja el simulador en `enabled: false` y puede apagarlo: durante la grabación no se pushea a `aws/`, `simulator/` ni `contracts/`.

## Costo mensual estimado
| Máquinas | 100 | 1.000 | 10.000 |
|---|---|---|---|
| USD/mes | ~10 | ~50 | ~475 |

Equipo: Benjamin · Alex · Fabricio

## Cómo trabajamos (hackathon)
- Push directo a `main`. Sin aprobaciones ni PR obligatorios: nadie espera a nadie.
- CODEOWNERS es solo informativo: dice a quién preguntar, no bloquea.
- Commits `feat(aws): ...`, `fix(sim): ...`, `chore(gcp): ...`. Antes de pushear: `git pull --rebase`.
- Las credenciales del Learner Lab las renueva Benjamin con `scripts/lab-creds.sh --gh`; el deploy de AWS corre solo al tocar `aws/` en `main`.
