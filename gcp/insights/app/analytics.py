"""Endpoints de analitica de daily-planning (T20): KPIs del dia, anomalias de combustible, pronostico de repuestos
y reentrenamiento de los modelos BigQuery ML.

Solo consultas y jobs de BigQuery ML con `maximum_bytes_billed` (nada de streaming). El SQL vive aqui y las
funciones `kpi_rows`, `fuel_anomalies` y `parts_forecast` son puras: arman la respuesta a partir de las filas, asi
los tests usan un BigQuery falso. Los CREATE MODEL son copias de gcp/bq/m_*.sql en app/sql/ (la imagen solo lleva
app/; tests/test_analytics.py verifica que coinciden).
"""
from __future__ import annotations

import os
from statistics import median

SQL_DIR = os.path.join(os.path.dirname(__file__), "sql")
MODELS = ("m_peer_clusters", "m_fuel_trend", "m_parts_demand")
HORIZON_DAYS = 14
ANOMALY_THRESHOLD = 0.95
# ASUNCION (igual que la consola en modo de prueba): costo por hora = combustible x 1,15 USD/L + 18 USD/h fijos.
FUEL_USD_L, FIXED_USD_H = 1.15, 18.0
HIGH_USE, COSTLY_PCT = 0.35, 5.0  # cuadrante: uso >= 35 % del dia; consumo > 5 % sobre la mediana de sus pares

# Horas y litros del dia (hora de Lima) por maquina: diferencia de los acumulados, como v_fuel_daily.
KPI_SQL = """
SELECT
  machine_id,
  ANY_VALUE(model) AS model,
  MAX(cumulative_operating_hours) - MIN(cumulative_operating_hours) AS operating_h,
  MAX(cumulative_idle_hours) - MIN(cumulative_idle_hours) AS idle_h,
  MAX(cumulative_fuel_used_l) - MIN(cumulative_fuel_used_l) AS fuel_l
FROM `{dataset}.telemetry`
WHERE ts >= TIMESTAMP(@day, 'America/Lima') AND ts < TIMESTAMP(DATE_ADD(@day, INTERVAL 1 DAY), 'America/Lima')
GROUP BY machine_id
"""

CLUSTER_SQL = """
SELECT machine_id, CENTROID_ID AS cluster_id
FROM ML.PREDICT(MODEL `{dataset}.m_peer_clusters`, (SELECT * FROM `{dataset}.v_machine_profile_30d`))
"""

# Sin query_statement, ML.DETECT_ANOMALIES evalua los datos de entrenamiento (/jobs/refresh reentrena a diario).
# El umbral va como LITERAL, no como @parametro: BigQuery exige una constante dentro del STRUCT de la funcion ML.
FUEL_ANOMALY_SQL = """
SELECT machine_id, DATE(day) AS day, fuel_l, lower_bound, upper_bound, anomaly_probability
FROM ML.DETECT_ANOMALIES(MODEL `{dataset}.m_fuel_trend`, STRUCT(%s AS anomaly_prob_threshold))
WHERE is_anomaly AND DATE(day) BETWEEN DATE_SUB(@day, INTERVAL @days - 1 DAY) AND @day
ORDER BY anomaly_probability DESC
""" % ANOMALY_THRESHOLD

FORECAST_SQL = """
SELECT part_id, zone_id, SUM(GREATEST(forecast_value, 0)) AS demand,
       SUM(GREATEST(prediction_interval_upper_bound, 0)) AS demand_upper
FROM ML.FORECAST(MODEL `{dataset}.m_parts_demand`, STRUCT(@horizon AS horizon, 0.8 AS confidence_level))
GROUP BY part_id, zone_id
ORDER BY part_id, zone_id
"""


def model_sql(model: str) -> str:
    with open(os.path.join(SQL_DIR, f"{model}.sql"), encoding="utf-8") as f:
        return f.read()


def _num(x) -> float:
    return float(x) if isinstance(x, (int, float)) else 0.0


def kpi_rows(rows: list[dict], clusters: dict[str, int] | None = None) -> list[dict]:
    """Filas de KPI_SQL -> `payload.machines` de kpi.daily (contracts/kpi.daily.schema.json). Pares = mismo cluster
    de m_peer_clusters, o mismo modelo si el cluster no esta."""
    clusters = clusters or {}
    out = []
    for r in rows:
        op, idle = max(_num(r.get("operating_h")), 0.0), max(_num(r.get("idle_h")), 0.0)
        on = op + idle
        op_day = min(op, 24.0)  # con TIME_WARP del simulador en vivo los acumulados corren mas rapido
        item = {"machine_id": r["machine_id"], "operating_h": round(op_day, 2),
                "idle_ratio": round(idle / on, 3) if on else 0.0, "utilization": round(op_day / 24, 3),
                "fuel_lph": round(max(_num(r.get("fuel_l")), 0.0) / on, 2) if on else 0.0,
                "_peers": clusters.get(r["machine_id"], r.get("model"))}
        if r["machine_id"] in clusters:
            item["cluster_id"] = int(clusters[r["machine_id"]])
        item["cost_per_h"] = round(item["fuel_lph"] * FUEL_USD_L + FIXED_USD_H, 2) if on else 0.0
        out.append(item)
    groups: dict = {}
    for item in out:
        if item["fuel_lph"] > 0:
            groups.setdefault(item["_peers"], []).append(item["fuel_lph"])
    for item in out:
        peers = groups.get(item.pop("_peers")) or []
        mid = median(peers) if peers else 0.0
        pct = round(100 * (item["fuel_lph"] - mid) / mid, 1) if mid and item["fuel_lph"] else 0.0
        item["fuel_vs_peers_pct"] = pct
        high, costly = item["utilization"] >= HIGH_USE, pct > COSTLY_PCT
        item["quadrant"] = ("workhorse" if costly else "star") if high else ("inefficient" if costly else "underused")
    return sorted(out, key=lambda i: i["machine_id"])[:100]


def fuel_anomalies(rows: list[dict]) -> list[dict]:
    return [{"machine_id": r["machine_id"], "day": str(r["day"]), "fuel_l": round(_num(r.get("fuel_l")), 1),
             "expected": [round(_num(r.get("lower_bound")), 1), round(_num(r.get("upper_bound")), 1)],
             "anomaly_probability": round(_num(r.get("anomaly_probability")), 3)} for r in rows]


def parts_forecast(rows: list[dict]) -> list[dict]:
    """Demanda de 14 dias por repuesto y zona en unidades enteras (se redondea hacia arriba)."""
    import math

    return [{"part_id": r["part_id"], "zone_id": r["zone_id"], "demand_14d": math.ceil(round(_num(r["demand"]), 6)),
             "demand_14d_upper": math.ceil(round(_num(r.get("demand_upper")), 6))} for r in rows]
