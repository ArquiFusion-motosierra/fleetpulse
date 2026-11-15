# AWS: despliegue

Servicios (cada uno con su `serverless.yml`), en el orden de `serverless-compose.yml`:
`shared` (layer) -> `core` (ingest: tablas, bucket, topics, API) -> `outbox-publisher`, `registry`,
`gcp-exporter`, `incidents` -> `scoring` -> `simulator` (apagado). `oci-writer` no existe (plan B).

## Desplegar
```bash
scripts/lab-creds.sh                      # credenciales del Learner Lab (--gh las sube a GitHub)
bash aws/shared/build.sh && cd aws && npx serverless@3 deploy
scripts/subscribe-notify.sh technician tu@correo.com   # tambien supervisor, management, operator
scripts/smoke.sh ingest
```
Confirma el correo de AWS tras cada suscripcion. `compose` no soporta `package`: para validar
un servicio, `cd aws/<servicio> && npx serverless@3 package --stage ci`.

## Simulador
```bash
cd aws && npx serverless@3 deploy --param="enabled=true"    # enciende
scripts/drift.py on S1                                      # inyecta deriva
cd aws && npx serverless@3 deploy --param="enabled=false"   # apaga (costo)
```

## Tests
Con el venv del repo, por servicio:
```bash
.venv/bin/pytest -q aws/shared/tests aws/ingest/tests aws/scoring/tests aws/incidents/tests \
  aws/integrations/outbox-publisher/tests
```
