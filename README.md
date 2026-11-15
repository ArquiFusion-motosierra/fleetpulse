# FleetPulse

Monitoreo predictivo de flota en tres nubes. Hackaton UTEC Cloud, 14-15 nov 2026.

| Carpeta | Frente | Responsable |
|---|---|---|
| `contracts/` | JSON Schema de eventos, IDs y nombres de secretos | Equipo (T04) |
| `aws/` | Ingesta, scoring, `incident-flow`, integraciones hacia OCI y GCP | Benjamin |
| `oci/` | PL/SQL, ORDS, OAuth y APEX | Alex |
| `gcp/` | Terraform, Cloud Run `insights`, BigQuery y Gemini | Fabricio |
| `simulator/` | Simulador de flota, escenarios S1-S6 y backfill | Alex |
| `scripts/` | `lab-creds.sh`, `push-secrets.sh`, `smoke.sh` | Equipo |
| `docs/` | `architecture.png` y guion del video | Equipo |

Backlog e issues: https://github.com/ArquiFusion-motosierra/backlog

## Reglas que no se negocian

- OCI solo en **Always Free**. Nunca Upgrade to Pay As You Go.
- GCP solo en **Free Trial**, sin upgrade. Prohibido Vertex AI, Compute Engine, Cloud SQL y load balancers.
- BigQuery solo con **load jobs**, nunca streaming. `require_partition_filter` activo.
- Los contratos en `contracts/` solo cambian con revision de los tres despues del tag `m1-contratos`.
- `main` protegida: todo entra por PR con aprobacion del CODEOWNER del frente.

## Hitos y tags

| Tag | Hito | Limite (Lima) |
|---|---|---|
| `m1-contratos` | M1 Contratos | Sab 20:30 |
| `m2-datos` | M2 Datos fluyen | Dom 02:00 |
| `m3-e2e` | M3 Punta a punta | Dom 14:30 |
| `v1.0-demo` | M4 Congelamiento | Dom 17:00 |

## Flujo de trabajo

```bash
git checkout -b <frente>/<tarea>-<descripcion>   # ej. aws/t05-ingesta
# ... commits ...
gh pr create --fill                              # el CODEOWNER del frente aprueba
```

El deploy de AWS corre en GitHub Actions al mergear a `main` cambios bajo `aws/`. Las credenciales del Learner Lab caducan cada sesion: renovarlas con `scripts/lab-creds.sh --gh`.
