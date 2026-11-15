"""Cloud Run `insights` (FastAPI). T08: /health. T16: POST /explain y GET /peers/{machine_id}.

Clientes externos (Gemini, BigQuery) detras de wrappers inyectables en app.state; en los tests se
reemplazan por falsos (sin red ni credenciales).
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

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
        result = validate_explain(raw, data)
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
