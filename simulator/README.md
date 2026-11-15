# simulator (T06)

| Comando | Qué hace |
|---|---|
| `python simulator/backfill.py --days 30 --step 15m` | 2,88 M lecturas en `simulator/out/telemetry/dt=/hr=/`, contexto, ~40 incidentes históricos (`events/`) y etiquetas de fallas (`labels/faults.json`). `--s3-bucket fp-raw-<cuenta>` lo sube a `backfill/` |
| `python simulator/plot_drift.py` | Gráfico de la deriva S1 de EX-017 en `simulator/out/ex017_s1.png` |
| `scripts/drift.py on S1` / `off all` / `status` | Escenarios en vivo en SSM `/fleetpulse/sim/scenarios` (S1, S2, S5, S7, S8, S9) |
| `python simulator/fpsim/sync_ids.py` | Copia `contracts/ids.yaml` a `fpsim/ids.json` después de cambiarlo |
| `pytest -q simulator/tests` | Tests (contratos, deriva, backfill, handler) |

Lambda `fpsim/handler.handler`, cada minuto: un lote por parque (mineria 100, agro 12, industria 12 máquinas) × 10 lecturas a `POST /telemetry` y contexto a `POST /context`. Sin estado: los acumulados son función del tiempo. `batch_id` es fijo por minuto, así que un reintento no duplica. S3 (CF-042), S4 (MN-005) y S6 (VQ-031..035, MN-003) son rasgos fijos de la flota.

S2 (combustible contaminado, lote L-2311): mientras está activo, EX-003, VQ-011, VQ-014 y TO-002 quedan fijas junto a la estación EST-NORTE (centro de la zona NORTE, con un temblor de unos 45 m) y los volquetes dejan de rotar de zona. Así las cuatro caen en la misma celda geohash6 y `CorrelateZone` las agrupa en vivo. Al apagar S2 vuelven a sus posiciones normales.

Parques (T47): turnos por parque en `fleet.shift_factor` (mineria 6-18 y medio turno 18-22; agro tractores 6-18 y media cuadrilla 18-22, cosechadoras 7-18; industria dos turnos 6-22 y cuadrilla de noche, compresores siguen a la planta y grupos electrógenos de respaldo con turno de día 8-16). Volquetes, tractores, cosechadoras y montacargas recorren las zonas de su parque (`fleet.ROAMING`); compresores y grupos electrógenos no se mueven. S8 (TA-004, aceite hidráulico contaminado) y S9 (MC-002, bomba hidráulica) dan severidad en vivo a agro e industria. Después de cambiar la física: `scripts/seed-baselines.py`.
