# simulator (T06)

| Comando | Qué hace |
|---|---|
| `python simulator/backfill.py --days 30 --step 15m` | 2,88 M lecturas en `simulator/out/telemetry/dt=/hr=/`, contexto, ~40 incidentes históricos (`events/`) y etiquetas de fallas (`labels/faults.json`). `--s3-bucket fp-raw-<cuenta>` lo sube a `backfill/` |
| `python simulator/plot_drift.py` | Gráfico de la deriva S1 de EX-017 en `simulator/out/ex017_s1.png` |
| `scripts/drift.py on S1` / `off all` / `status` | Escenarios en vivo en SSM `/fleetpulse/sim/scenarios` (S1, S2, S5) |
| `python simulator/fpsim/sync_ids.py` | Copia `contracts/ids.yaml` a `fpsim/ids.json` después de cambiarlo |
| `pytest -q simulator/tests` | Tests (contratos, deriva, backfill, handler) |

Lambda `fpsim/handler.handler`, cada minuto: 100 máquinas × 10 lecturas a `POST /telemetry` y contexto a `POST /context`. Sin estado: los acumulados son función del tiempo. `batch_id` es fijo por minuto, así que un reintento no duplica. S3 (CF-042), S4 (MN-005) y S6 (VQ-031..035, MN-003) son rasgos fijos de la flota.
