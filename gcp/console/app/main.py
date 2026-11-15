"""Cloud Run `console`: sirve el frontend React compilado y hace de BFF (/api/*) hacia las APIs de AWS.

MOCK=1 usa datos de prueba en memoria (desarrollo local o respaldo para grabar si AWS cae).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.aws_api import AwsApi, UpstreamError
from app.mock import MockRegistry

log = logging.getLogger("console")
logging.basicConfig(level=logging.INFO, format="%(message)s")

STATIC = os.environ.get("STATIC_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "web", "dist"))
INC_RE = re.compile(r"^INC-(EX|CF|VQ|MN|TO)-[0-9]{3}-[0-9]{8}T[0-9]{4}$")
MID_RE = re.compile(r"^(EX|CF|VQ|MN|TO)-[0-9]{3}$")

app = FastAPI(title="FleetPulse console", docs_url=None, redoc_url=None)
app.state.mock = MockRegistry() if os.environ.get("MOCK") == "1" else None
app.state.aws = AwsApi()


def upstream(fn, *args):
    try:
        return fn(*args)
    except UpstreamError as exc:
        log.warning(json.dumps({"msg": "aws_error", "status": exc.status, "detail": exc.body[:200]}))
        if exc.status == 404:
            raise HTTPException(404, "NOT_FOUND") from exc
        raise HTTPException(502, "AWS_UNAVAILABLE") from exc


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "console", "mode": "mock" if app.state.mock else "aws"}


@app.get("/api/machines")
def machines():
    m = app.state.mock
    return m.list_machines() if m else upstream(app.state.aws.get, "/machines")


@app.get("/api/machines/{machine_id}")
def machine(machine_id: str):
    if not MID_RE.match(machine_id):
        raise HTTPException(400, "BAD_MACHINE_ID")
    m = app.state.mock
    if m:
        d = m.machine(machine_id)
        if not d:
            raise HTTPException(404, "MACHINE_NOT_FOUND")
        return d
    return upstream(app.state.aws.get, f"/machines/{machine_id}")


@app.get("/api/incidents")
def incidents(status: str | None = None, limit: int = 50):
    if status not in (None, "open", "assigned", "closed"):
        raise HTTPException(400, "BAD_STATUS")
    limit = max(1, min(limit, 200))
    m = app.state.mock
    if m:
        return m.list_incidents(status, limit)
    q = f"limit={limit}" + (f"&status={status}" if status else "")
    return upstream(app.state.aws.get, "/incidents", q)


@app.get("/api/incidents/{incident_id}")
def incident(incident_id: str):
    if not INC_RE.match(incident_id):
        raise HTTPException(400, "BAD_INCIDENT_ID")
    m = app.state.mock
    if m:
        d = m.incident(incident_id)
        if not d:
            raise HTTPException(404, "INCIDENT_NOT_FOUND")
        return d
    return upstream(app.state.aws.get, f"/incidents/{incident_id}")


@app.get("/api/stock")
def stock():
    m = app.state.mock
    return m.list_stock() if m else upstream(app.state.aws.get, "/stock")


@app.get("/api/reorders")
def reorders():
    m = app.state.mock
    return m.list_reorders() if m else upstream(app.state.aws.get, "/reorders")


@app.get("/api/orders")
def orders():
    m = app.state.mock
    return m.list_orders() if m else upstream(app.state.aws.get, "/orders")


@app.get("/api/kpis")
def kpis():
    """KPIs del ultimo dia con datos (daily-planning corre a las 05:00 de Lima por el dia anterior)."""
    m = app.state.mock
    if m:
        return m.kpis()
    today = datetime.now(timezone.utc) - timedelta(hours=5)
    for back in range(1, 4):
        day = (today - timedelta(days=back)).strftime("%Y-%m-%d")
        data = upstream(app.state.aws.get, f"/kpis/{day}")
        if data.get("machines"):
            return data
    return {"day": None, "machines": []}


@app.post("/api/incidents/{incident_id}/close")
async def close(incident_id: str, request: Request):
    """AWS es el unico que cambia el estado: calcula downtime, libera al tecnico y emite incident.closed."""
    if not INC_RE.match(incident_id):
        raise HTTPException(400, "BAD_INCIDENT_ID")
    try:
        body = await request.json()
    except ValueError:
        body = {}
    payload = {"closed_by": str(body.get("closed_by") or "console")[:64],
               "cause_summary": str(body.get("cause_summary") or "")[:1000]}
    parts = body.get("parts_used")
    if isinstance(parts, list):
        payload["parts_used"] = [{"part_id": str(p["part_id"]), "zone_id": str(p["zone_id"]), "qty": int(p["qty"])}
                                 for p in parts if isinstance(p, dict) and {"part_id", "zone_id", "qty"} <= p.keys()]
    log.info(json.dumps({"msg": "close_requested", "incident_id": incident_id}))
    m = app.state.mock
    if m:
        r = m.close(incident_id, payload)
        if r is None:
            raise HTTPException(404, "INCIDENT_NOT_FOUND")
        return r
    return upstream(app.state.aws.close_incident, incident_id, payload)


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


# Frontend compilado (Vite). Rutas desconocidas fuera de /api devuelven index.html (SPA).
if os.path.isdir(STATIC):
    app.mount("/assets", StaticFiles(directory=os.path.join(STATIC, "assets")), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "ROUTE_NOT_FOUND")
        candidate = os.path.join(STATIC, path)
        if path and os.path.isfile(candidate) and os.path.realpath(candidate).startswith(os.path.realpath(STATIC)):
            return FileResponse(candidate)
        return FileResponse(os.path.join(STATIC, "index.html"))
