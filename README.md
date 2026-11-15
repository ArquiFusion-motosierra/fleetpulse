# FleetPulse — ArquiFusion Corp
Telemetría de maquinaria pesada → decisiones de mantenimiento, técnicos y repuestos. 100 % serverless en AWS + GCP.

▶ Video (4:30): _pendiente_

![Arquitectura](docs/architecture.png)

| Nube | Rol | Servicios |
|---|---|---|
| AWS | Ingesta, detección, orquestación, registro operativo, secretos | API Gateway, Lambda, DynamoDB, S3, SQS, SNS, Step Functions, Secrets Manager |
| GCP | Analítica histórica, IA y consola operativa | Cloud Storage, BigQuery, BigQuery ML, Cloud Run, Gemini, Secret Manager |

## Desplegar
1. `scripts/lab-creds.sh`
2. `terraform -chdir=gcp/terraform apply`
3. `scripts/push-secrets.sh`
4. `bash aws/shared/build.sh && (cd aws && npx serverless@3 deploy)`
5. `scripts/seed-registry.py` (catálogo de `contracts/ids.yaml`)
6. `scripts/smoke.sh`

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
