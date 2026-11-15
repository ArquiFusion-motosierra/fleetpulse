"""Cloud Run `insights` (FastAPI). T08: /health. T16: POST /explain y GET /peers/{machine_id}.

Clientes externos (Gemini, BigQuery) detras de wrappers inyectables en app.state; en los tests se
reemplazan por falsos (sin red ni credenciales).
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from app import analytics
from app.bq import BqClient, build_peers_response
from app.explain import fallback_response, validate_explain
from app.gemini import GeminiClient, GeminiError

log = logging.getLogger("insights")
logging.basicConfig(level=logging.INFO, format="%(message)s")

MID_RE = r"^(EX|CF|VQ|MN|TO)-[0-9]{3}$"

app = FastAPI(title="FleetPulse insights")
app.state.gemini = GeminiClient()
app.state.bq = BqClient()
app.state.explain_cache = {}  # incident_id -> respuesta (cachea para no gastar cuota de Gemini)


class DeviatedVar(BaseModel):
    var: str
    value: float
    z: float


class ExplainRequest(BaseModel):
    incident_id: str
    machine_id: str
    model: str | None = None
    severity: str | None = None
    deviated_vars: list[DeviatedVar] = Field(default_factory=list)
    failure_mode: str | None = None
    group_id: str | None = None
    context: dict | None = None
    recent_incidents: list = Field(default_factory=list)


@app.get("/health")
def health():
    return {"status": "ok", "project": os.environ.get("GCP_PROJECT_ID", "")}


@app.post("/explain")
def explain(req: ExplainRequest):
    cached = app.state.explain_cache.get(req.incident_id)
    if cached is not None:
        return {**cached, "cached": True}

    data = req.model_dump()
    try:
        raw = app.state.gemini.explain(data)
        result = validate_explain(raw)
        if result is None:
            raise GeminiError("invalid_json")
    except GeminiError as exc:
        log.warning('{"msg": "gemini_fallback", "incident_id": "%s", "reason": "%s"}', req.incident_id, exc)
        result = fallback_response(data)

    app.state.explain_cache[req.incident_id] = result
    log.info('{"msg": "explain_done", "incident_id": "%s", "source": "%s"}', req.incident_id, result["source"])
    return {**result, "cached": False}


@app.get("/peers/{machine_id}")
def peers(machine_id: str):
    import re

    if not re.match(MID_RE, machine_id):
        raise HTTPException(400, "BAD_MACHINE_ID")
    try:
        rows = app.state.bq.peers(machine_id)
    except Exception as exc:  # BigQuery sin credenciales/datos: degradar a 503, no romper el servicio
        log.warning('{"msg": "bq_error", "machine_id": "%s", "error": "%s"}', machine_id, type(exc).__name__)
        raise HTTPException(503, "BQ_UNAVAILABLE") from exc
    resp = build_peers_response(rows, machine_id)
    if resp is None:
        raise HTTPException(404, "MACHINE_NOT_FOUND")
    return resp


# ---------- T20: analitica para daily-planning (BigQuery ML; consultas y jobs, nunca streaming) ----------

MODEL_MAX_BYTES = int(os.environ.get("BQ_MODEL_MAX_BYTES", "2000000000"))  # 2 GB por entrenamiento, como apply_models
JOB_RE = r"^[A-Za-z0-9_-]{1,1024}$"


def _day(day: str | None) -> date:
    """Dia pedido o, por defecto, ayer en hora de Lima (daily-planning corre a las 05:00 por el dia anterior)."""
    if day is None:
        return (datetime.now(timezone.utc) - timedelta(hours=5)).date() - timedelta(days=1)
    try:
        return date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, "BAD_DAY") from None


def _bq(kind: str, sql: str, params: dict) -> list[dict]:
    try:
        return app.state.bq.rows(sql, params)
    except Exception as exc:  # sin modelo, sin datos o sin credenciales: 503, nunca un 500
        log.warning('{"msg": "bq_error", "endpoint": "%s", "error": "%s"}', kind, type(exc).__name__)
        raise HTTPException(503, "BQ_UNAVAILABLE") from exc


@app.get("/kpis/daily")
def kpis_daily(day: str | None = None):
    """`payload` de kpi.daily para el dia (hora de Lima): horas, ralenti, uso, L/h, costo, cluster y cuadrante."""
    d = _day(day)
    rows = _bq("kpis", analytics.KPI_SQL, {"day": d})
    try:
        clusters = {r["machine_id"]: r["cluster_id"] for r in app.state.bq.rows(analytics.CLUSTER_SQL)}
    except Exception as exc:  # sin m_peer_clusters los pares son el mismo modelo
        log.warning('{"msg": "bq_error", "endpoint": "kpis_clusters", "error": "%s"}', type(exc).__name__)
        clusters = {}
    machines = analytics.kpi_rows(rows, clusters)
    if not machines:
        raise HTTPException(404, "NO_DATA_FOR_DAY")
    return {"day": d.isoformat(), "machines": machines}


@app.get("/anomalies/fuel")
def anomalies_fuel(day: str | None = None, days: int = Query(7, ge=1, le=30)):
    """Dias con consumo fuera del intervalo de m_fuel_trend (ML.DETECT_ANOMALIES), en los `days` hasta `day`."""
    d = _day(day)
    rows = _bq("anomalies_fuel", analytics.FUEL_ANOMALY_SQL,
               {"day": d, "days": days, "threshold": analytics.ANOMALY_THRESHOLD})
    return {"day": d.isoformat(), "days": days, "threshold": analytics.ANOMALY_THRESHOLD,
            "anomalies": analytics.fuel_anomalies(rows)}


@app.get("/forecast/parts")
def forecast_parts():
    """Demanda de repuestos de los proximos 14 dias por repuesto y zona (m_parts_demand, ARIMA_PLUS)."""
    rows = _bq("forecast_parts", analytics.FORECAST_SQL, {"horizon": analytics.HORIZON_DAYS})
    return {"horizon_days": analytics.HORIZON_DAYS, "parts": analytics.parts_forecast(rows)}


@app.post("/jobs/refresh")
def jobs_refresh():
    """Reentrena los tres modelos (CREATE OR REPLACE MODEL) sin esperar; devuelve los ids de job."""
    jobs, errors = [], []
    for model in analytics.MODELS:
        try:
            jobs.append({"model": model, "job_id": app.state.bq.submit(analytics.model_sql(model), MODEL_MAX_BYTES)})
        except Exception as exc:
            log.warning('{"msg": "bq_job_error", "model": "%s", "error": "%s"}', model, type(exc).__name__)
            errors.append({"model": model, "error": type(exc).__name__})
    if not jobs:
        raise HTTPException(503, "BQ_UNAVAILABLE")
    log.info('{"msg": "jobs_refresh", "jobs": %d, "errors": %d}', len(jobs), len(errors))
    return {"jobs": jobs, "errors": errors}


@app.get("/jobs/status")
def jobs_status(ids: str = Query(..., min_length=1, max_length=4000)):
    """Estado de los jobs (`ids` separados por coma). `done` cuando todos terminaron; `failed` los que fallaron."""
    import re

    job_ids = [j for j in ids.split(",") if j]
    if not job_ids or len(job_ids) > 10 or not all(re.match(JOB_RE, j) for j in job_ids):
        raise HTTPException(400, "BAD_JOB_IDS")
    try:
        jobs = [app.state.bq.job(j) for j in job_ids]
    except Exception as exc:
        log.warning('{"msg": "bq_error", "endpoint": "jobs_status", "error": "%s"}', type(exc).__name__)
        raise HTTPException(503, "BQ_UNAVAILABLE") from exc
    return {"jobs": jobs, "done": all(j["state"] == "DONE" for j in jobs),
            "failed": [j["job_id"] for j in jobs if j["state"] == "DONE" and j.get("error")]}
