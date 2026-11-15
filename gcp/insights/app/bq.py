"""Wrapper inyectable de BigQuery para /peers y la analitica de T20. Usa google-cloud-bigquery (NO streaming).

La consulta pone maximum_bytes_billed para no salir del free tier. En los tests se inyecta un falso
con el mismo metodo `peers`, asi no hace falta red ni credenciales.
"""
from __future__ import annotations

import os

PROJECT = os.environ.get("GCP_PROJECT_ID", "")
DATASET = os.environ.get("BQ_DATASET", "fleetpulse")
MAX_BYTES = int(os.environ.get("BQ_MAX_BYTES", str(512 * 1024 * 1024)))  # 512 MiB por consulta

# Compara la maquina con sus pares (mismo modelo) usando v_machine_profile_30d.
# PERCENT_RANK da la posicion relativa (0 = mejor/menor, 1 = peor/mayor) dentro del cluster del modelo.
PEERS_SQL = """
WITH cohort AS (
  SELECT machine_id, model, zone_id, avg_fuel_rate_lph, avg_vibration_mm_s,
         avg_coolant_temp_c, avg_hydraulic_pressure_bar, operating_ratio, hours_total
  FROM `{dataset}.v_machine_profile_30d`
  WHERE model = (SELECT model FROM `{dataset}.v_machine_profile_30d` WHERE machine_id = @machine_id)
)
SELECT
  machine_id, model, zone_id, avg_fuel_rate_lph, avg_vibration_mm_s,
  avg_coolant_temp_c, avg_hydraulic_pressure_bar, operating_ratio, hours_total,
  ROUND(PERCENT_RANK() OVER (ORDER BY avg_fuel_rate_lph), 3) AS fuel_pctile,
  ROUND(PERCENT_RANK() OVER (ORDER BY avg_vibration_mm_s), 3) AS vibration_pctile,
  COUNT(*) OVER () AS peer_count,
  ROUND(AVG(avg_fuel_rate_lph) OVER (), 2) AS peer_avg_fuel_rate_lph,
  ROUND(AVG(avg_vibration_mm_s) OVER (), 3) AS peer_avg_vibration_mm_s
FROM cohort
QUALIFY machine_id = @machine_id
"""


class BqClient:
    def __init__(self, project=None, dataset=None, max_bytes=MAX_BYTES):
        self.project = project or PROJECT
        self.dataset = dataset or DATASET
        self.max_bytes = max_bytes
        self._client = None

    def _c(self):
        if self._client is None:
            from google.cloud import bigquery

            self._client = bigquery.Client(project=self.project)
        return self._client

    def peers(self, machine_id: str) -> list[dict]:
        from google.cloud import bigquery

        sql = PEERS_SQL.format(dataset=self.dataset)
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("machine_id", "STRING", machine_id)],
            maximum_bytes_billed=self.max_bytes,
        )
        job = self._c().query(sql, job_config=job_config)
        return [dict(row) for row in job.result()]


    def rows(self, sql: str, params: dict | None = None) -> list[dict]:
        """Consulta con parametros con nombre (date -> DATE, int -> INT64, float -> FLOAT64, str -> STRING)."""
        from google.cloud import bigquery

        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter(k, _bq_type(v), v) for k, v in (params or {}).items()],
            maximum_bytes_billed=self.max_bytes,
        )
        job = self._c().query(sql.format(dataset=self.dataset), job_config=job_config)
        return [dict(row) for row in job.result()]

    def submit(self, sql: str, max_bytes: int | None = None) -> str:
        """Lanza un job (CREATE MODEL) sin esperarlo y devuelve su id."""
        from google.cloud import bigquery

        job_config = bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes or self.max_bytes)
        return self._c().query(sql, job_config=job_config).job_id

    def job(self, job_id: str) -> dict:
        j = self._c().get_job(job_id)
        err = (j.error_result or {}).get("message") if j.state == "DONE" else None
        return {"job_id": job_id, "state": j.state, "error": err}


def _bq_type(v) -> str:
    import datetime

    if isinstance(v, bool):
        return "BOOL"
    if isinstance(v, int):
        return "INT64"
    if isinstance(v, float):
        return "FLOAT64"
    if isinstance(v, datetime.date):
        return "DATE"
    return "STRING"


def build_peers_response(rows: list[dict], machine_id: str) -> dict | None:
    """Arma la respuesta de /peers a partir de la fila de la maquina. Pura: sin red."""
    if not rows:
        return None
    r = rows[0]
    return {
        "machine_id": machine_id,
        "model": r.get("model"),
        "zone_id": r.get("zone_id"),
        "peer_count": r.get("peer_count"),
        "metrics": {
            "fuel_rate_lph": r.get("avg_fuel_rate_lph"),
            "vibration_mm_s": r.get("avg_vibration_mm_s"),
            "coolant_temp_c": r.get("avg_coolant_temp_c"),
            "hydraulic_pressure_bar": r.get("avg_hydraulic_pressure_bar"),
            "operating_ratio": r.get("operating_ratio"),
            "hours_total": r.get("hours_total"),
        },
        "peer_avg": {
            "fuel_rate_lph": r.get("peer_avg_fuel_rate_lph"),
            "vibration_mm_s": r.get("peer_avg_vibration_mm_s"),
        },
        "percentiles": {
            "fuel_rate_lph": r.get("fuel_pctile"),
            "vibration_mm_s": r.get("vibration_pctile"),
        },
    }
