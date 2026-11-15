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
from app import mock_reports as mr
from app.mock import MockRegistry

log = logging.getLogger("console")
logging.basicConfig(level=logging.INFO, format="%(message)s")

STATIC = os.environ.get("STATIC_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "web", "dist"))
INC_RE = re.compile(r"^INC-(EX|CF|VQ|MN|TO)-[0-9]{3}-[0-9]{8}T[0-9]{4}$")
MID_RE = re.compile(r"^(EX|CF|VQ|MN|TO)-[0-9]{3}$")

app = FastAPI(title="FleetPulse console", docs_url=None, redoc_url=None)
app.state.mock = MockRegistry() if os.environ.get("MOCK") == "1" else None
app.state.reports = mr.MockReports(app.state.mock) if app.state.mock else None
app.state.aws = AwsApi()
RID_RE = re.compile(r"^RPT-(EX|CF|VQ|MN|TO)-[0-9]{3}-[0-9]{8}T[0-9]{6}$")
TECH_RE = re.compile(r"^TEC-[0-9]{2}$")
CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


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


OEM_PATH = os.path.join(os.path.dirname(__file__), "oem.json")


@app.get("/api/oem/{model_id}")
def oem(model_id: str):
    """Ficha del fabricante del modelo (contracts/oem_specs.yaml, ilustrativa), empaquetada con la consola."""
    with open(OEM_PATH, encoding="utf-8") as f:
        specs = json.load(f)
    model = specs["models"].get(model_id)
    if not model:
        raise HTTPException(404, "MODEL_NOT_FOUND")
    return {"model_id": model_id, "illustrative": specs["illustrative"], **model}


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


# ---------- T34: login y rutas por rol ----------

@app.get("/api/auth/config")
def auth_config():
    """Configuracion publica del login. En modo de prueba no hay Cognito: la consola ofrece usuarios de prueba."""
    if app.state.mock:
        return {"mode": "mock", "users": [{"token": f"mock.{k}", **v} for k, v in mr.USERS.items()]}
    pool, client, domain = (os.environ.get(k, "") for k in ("AUTH_USER_POOL_ID", "AUTH_CLIENT_ID", "AUTH_DOMAIN"))
    if not (pool and client and domain):
        raise HTTPException(503, "AUTH_NOT_CONFIGURED")
    region = pool.split("_", 1)[0]
    return {"mode": "cognito", "region": region, "user_pool_id": pool, "client_id": client,
            "authority": f"https://cognito-idp.{region}.amazonaws.com/{pool}",
            "domain": domain if domain.startswith("https://") else f"https://{domain}"}


def _as_user(request: Request, method: str, path: str, body: dict | None = None, query: str = ""):
    """Modo real: reenvia el token de Cognito a la API compartida y devuelve lo que diga AWS."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "UNAUTHORIZED")
    try:
        status, data = app.state.aws.as_user(method, path, auth, body, query)
    except UpstreamError as exc:
        raise HTTPException(502, "AWS_UNAVAILABLE") from exc
    return JSONResponse(data, status_code=status)


def _mock(request: Request, fn, *args):
    try:
        user = mr.user_of(request.headers.get("authorization"))
        return fn(user, *args)
    except PermissionError as exc:
        raise HTTPException(401, "UNAUTHORIZED") from exc
    except mr.Forbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except mr.NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except mr.Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except mr.BadRequest as exc:
        raise HTTPException(400, str(exc)) from exc


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "BAD_JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "BAD_JSON")
    return body


@app.post("/api/reports")
async def create_report(request: Request):
    body = await _json(request)
    if app.state.reports:
        return JSONResponse({"report": _mock(request, app.state.reports.create, body)}, status_code=201)
    return _as_user(request, "POST", "/reports", body)


@app.get("/api/reports")
def list_reports(request: Request, status: str | None = None, machine_id: str | None = None,
                 incident_id: str | None = None, mine: int = 0):
    bad = status not in (None, "open", "in_review", "closed")
    bad = bad or bool(machine_id and not MID_RE.match(machine_id)) or bool(incident_id and not INC_RE.match(incident_id))
    if bad:
        raise HTTPException(400, "BAD_FILTER")
    if app.state.reports:
        return {"reports": _mock(request, app.state.reports.list, status, machine_id, incident_id, bool(mine))}
    pairs = (("status", status), ("machine_id", machine_id), ("incident_id", incident_id), ("mine", 1 if mine else None))
    return _as_user(request, "GET", "/reports", query="&".join(f"{k}={v}" for k, v in pairs if v))


@app.get("/api/reports/{report_id}")
def get_report(request: Request, report_id: str):
    if not RID_RE.match(report_id):
        raise HTTPException(400, "BAD_REPORT_ID")
    if app.state.reports:
        return _mock(request, app.state.reports.get, report_id)
    return _as_user(request, "GET", f"/reports/{report_id}")


@app.post("/api/reports/{report_id}/close")
async def close_report(request: Request, report_id: str):
    if not RID_RE.match(report_id):
        raise HTTPException(400, "BAD_REPORT_ID")
    body = await _json(request)
    if app.state.reports:
        return _mock(request, app.state.reports.close, report_id, body)
    return _as_user(request, "POST", f"/reports/{report_id}/close", body)


@app.get("/api/abnormality-types")
def abnormality_types(request: Request):
    if app.state.reports:
        return {"abnormality_types": _mock(request, app.state.reports.list_types)}
    return _as_user(request, "GET", "/abnormality-types")


TECH_IDS = [f"TEC-0{i}" for i in range(1, 7)]


@app.get("/api/admin/technicians")
def admin_technicians(request: Request):
    """Los 6 tecnicos del catalogo, cada uno leido de GET /admin/technicians/{id} (T35)."""
    if app.state.reports:
        return {"technicians": [_mock(request, app.state.reports.get_tech, t) for t in TECH_IDS]}
    out = []
    for t in TECH_IDS:
        resp = _as_user(request, "GET", f"/admin/technicians/{t}")
        if resp.status_code != 200:
            return resp
        out.append(json.loads(resp.body))
    return {"technicians": out}


@app.put("/api/admin/technicians/{tech_id}")
async def admin_put_technician(request: Request, tech_id: str):
    if not TECH_RE.match(tech_id):
        raise HTTPException(400, "BAD_TECH_ID")
    body = await _json(request)
    if app.state.reports:
        return _mock(request, app.state.reports.put_tech, tech_id, body)
    return _as_user(request, "PUT", f"/admin/technicians/{tech_id}", body)


@app.post("/api/admin/abnormality-types")
async def admin_add_type(request: Request):
    body = await _json(request)
    if app.state.reports:
        return JSONResponse(_mock(request, app.state.reports.add_type, body), status_code=201)
    return _as_user(request, "POST", "/admin/abnormality-types", body)


@app.delete("/api/admin/abnormality-types/{code}")
def admin_delete_type(request: Request, code: str):
    if not CODE_RE.match(code):
        raise HTTPException(400, "BAD_CODE")
    if app.state.reports:
        _mock(request, app.state.reports.delete_type, code)
        return {"deleted": code}
    return _as_user(request, "DELETE", f"/admin/abnormality-types/{code}")


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
