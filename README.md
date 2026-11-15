# FleetPulse — ArquiFusion Corp
Telemetría de maquinaria pesada → decisiones de mantenimiento, técnicos y repuestos. 100 % serverless en AWS + OCI + GCP.

▶ Video (4:30): _pendiente_

![Arquitectura](docs/architecture.png)

| Nube | Rol | Servicios |
|---|---|---|
| AWS | Ingesta, detección, orquestación, secretos | API Gateway, Lambda, DynamoDB, S3, SQS, SNS, Step Functions, Secrets Manager |
| OCI | Sistema de registro y consola operativa | Autonomous Database, ORDS, APEX |
| GCP | Analítica histórica e IA | Cloud Storage, BigQuery, BigQuery ML, Cloud Run, Gemini |

## Desplegar
1. `scripts/lab-creds.sh`
2. `terraform -chdir=gcp/terraform apply` y `terraform -chdir=oci/terraform apply`
3. `scripts/oci-sql.sh` (esquema, ORDS, OAuth, APEX)
4. `scripts/push-secrets.sh`
5. `cd aws && serverless deploy`
6. `scripts/smoke.sh`

## Costo mensual estimado
| Máquinas | 100 | 1.000 | 10.000 |
|---|---|---|---|
| USD/mes | ~10 | ~50 | ~475 |

Equipo: Benjamin · Alex · Fabricio
