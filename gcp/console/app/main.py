"""Cloud Run `console`: sirve el frontend React compilado y hace de BFF (/api/*) hacia las APIs de AWS.

MOCK=1 usa datos de prueba en memoria (desarrollo local o respaldo para grabar si AWS cae).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
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
INC_RE = re.compile(r"^INC-(EX|CF|VQ|MN|TO|TA|CS|MC|CP|GE)-[0-9]{3}-[0-9]{8}T[0-9]{4}$")
MID_RE = re.compile(r"^(EX|CF|VQ|MN|TO|TA|CS|MC|CP|GE)-[0-9]{3}$")

app = FastAPI(title="FleetPulse console", docs_url=None, redoc_url=None)
app.state.mock = MockRegistry() if os.environ.get("MOCK") == "1" else None
app.state.reports = mr.MockReports(app.state.mock) if app.state.mock else None
app.state.aws = AwsApi()
FLEET_PATH = os.path.join(os.path.dirname(__file__), "fleet.json")
with open(FLEET_PATH, encoding="utf-8") as _f:
    _FLEET = json.load(_f)
DEFAULT_TENANT = _FLEET.get("default_tenant", "mineria")
TENANTS = {t["tenant_id"]: t for t in _FLEET.get("tenants") or [{"tenant_id": DEFAULT_TENANT}]}
_TENANT_OF = {**{z["zone_id"]: z.get("tenant_id", DEFAULT_TENANT) for z in _FLEET["zones"]},
              **{m["machine_id"]: m.get("tenant_id", DEFAULT_TENANT) for m in _FLEET["machines"]}}
WHOAMI_TTL_S = 300
_whoami_cache: dict[str, tuple[float, dict]] = {}
RID_RE = re.compile(r"^RPT-(EX|CF|VQ|MN|TO|TA|CS|MC|CP|GE)-[0-9]{3}-[0-9]{8}T[0-9]{6}$")
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


# ---------- multi-tenancy: alcance por parque ----------
# El parque sale del token, nunca del navegador: en modo real el BFF llama `GET /whoami` en la HTTP API (API Gateway
# valida la firma del ID token) y cachea la respuesta 5 min; en modo de prueba, del usuario mock. Solo el jefe de
# mantenimiento (`global`) puede elegir parque con `?tenant=`. Sin token: el parque por defecto (consola anterior al
# login). Ver docs/superpowers/plans/2026-11-15-multi-tenancy.md.

def tenant_of_row(row: dict) -> str:
    return row.get("tenant_id") or _TENANT_OF.get(row.get("machine_id") or "") or \
        _TENANT_OF.get(row.get("zone_id") or "") or DEFAULT_TENANT


def _identity(request: Request) -> dict | None:
    """{tenant_id, tenants, global} del usuario, o None sin token."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    if app.state.mock:
        try:
            user = mr.user_of(auth)
        except PermissionError:
            return None
        own = user.get("tenant_id")
        return {"tenant_id": own, "tenants": [own] if own else list(TENANTS), "global": own is None,
                "role": user["role"]}
    key = hashlib.sha256(auth.encode()).hexdigest()
    hit = _whoami_cache.get(key)
    if hit and time.monotonic() - hit[0] < WHOAMI_TTL_S:
        return hit[1]
    try:
        status, data = app.state.aws.as_user("GET", "/whoami", auth)
    except UpstreamError as exc:
        raise HTTPException(502, "AWS_UNAVAILABLE") from exc
    if status == 401:
        raise HTTPException(401, "UNAUTHORIZED")
    if status != 200:
        raise HTTPException(502, "AWS_UNAVAILABLE")
    ident = {"tenant_id": data.get("tenant_id"), "tenants": data.get("tenants") or [DEFAULT_TENANT],
             "global": bool(data.get("global")), "role": data.get("role")}
    if len(_whoami_cache) > 1000:
        _whoami_cache.clear()
    _whoami_cache[key] = (time.monotonic(), ident)
    return ident


def scope(request: Request, tenant: str | None = None) -> list[str]:
    """Parques que esta peticion puede leer. Un usuario de parque que pide otro: 403."""
    wanted = [t for t in (tenant or "").split(",") if t]
    if any(t not in TENANTS for t in wanted):
        raise HTTPException(400, "BAD_TENANT")
    ident = _identity(request)
    if ident is None:
        allowed = [DEFAULT_TENANT]
    elif ident["global"]:
        allowed = [t for t in TENANTS if t in ident["tenants"]] or list(TENANTS)
    else:
        allowed = [ident["tenant_id"] or DEFAULT_TENANT]
    if wanted and not set(wanted) <= set(allowed):
        raise HTTPException(403, "TENANT_FORBIDDEN")
    return wanted or allowed


def _q(scope_: list[str], query: str = "") -> str:
    return "&".join(x for x in (query, "tenant=" + ",".join(scope_)) if x)


def _only(rows: list[dict], scope_: list[str]) -> list[dict]:
    return [r for r in rows if tenant_of_row(r) in scope_]


@app.get("/api/tenants")
def tenants(request: Request):
    """Parques que la consola puede mostrar: todos (para el selector del jefe de mantenimiento) y el alcance."""
    ident = _identity(request)
    allowed = scope(request)
    zones = [{k: z.get(k) for k in ("zone_id", "name", "lat", "lon", "tenant_id")} for z in _FLEET["zones"]]
    return {"tenants": [{k: v for k, v in TENANTS[t].items() if k != "cognito_group"} for t in TENANTS],
            "zones": [z for z in zones if (z["tenant_id"] or DEFAULT_TENANT) in allowed], "scope": allowed, "tenant_id": (ident or {}).get("tenant_id", DEFAULT_TENANT if ident is None else None),
            "global": bool(ident and ident["global"])}


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "console", "mode": "mock" if app.state.mock else "aws"}


@app.get("/api/machines")
def machines(request: Request, tenant: str | None = None):
    s = scope(request, tenant)
    m = app.state.mock
    if m:
        return {"machines": _only(m.list_machines()["machines"], s), "tenants": s}
    return upstream(app.state.aws.get, "/machines", _q(s))


@app.get("/api/machines/{machine_id}")
def machine(request: Request, machine_id: str, tenant: str | None = None):
    if not MID_RE.match(machine_id):
        raise HTTPException(400, "BAD_MACHINE_ID")
    s = scope(request, tenant)
    m = app.state.mock
    if m:
        d = m.machine(machine_id)
        if not d or tenant_of_row(d) not in s:
            raise HTTPException(404, "MACHINE_NOT_FOUND")
        return d
    return upstream(app.state.aws.get, f"/machines/{machine_id}", _q(s))


@app.get("/api/incidents")
def incidents(request: Request, status: str | None = None, limit: int = 50, tenant: str | None = None):
    if status not in (None, "open", "assigned", "closed"):
        raise HTTPException(400, "BAD_STATUS")
    limit = max(1, min(limit, 200))
    s = scope(request, tenant)
    m = app.state.mock
    if m:
        return {"incidents": _only(m.list_incidents(status, 200)["incidents"], s)[:limit]}
    q = f"limit={limit}" + (f"&status={status}" if status else "")
    return upstream(app.state.aws.get, "/incidents", _q(s, q))


@app.get("/api/incidents/{incident_id}")
def incident(request: Request, incident_id: str, tenant: str | None = None):
    if not INC_RE.match(incident_id):
        raise HTTPException(400, "BAD_INCIDENT_ID")
    s = scope(request, tenant)
    m = app.state.mock
    if m:
        d = m.incident(incident_id)
        if not d or tenant_of_row(d) not in s:
            raise HTTPException(404, "INCIDENT_NOT_FOUND")
        return d
    return upstream(app.state.aws.get, f"/incidents/{incident_id}", _q(s))


@app.get("/api/stock")
def stock(request: Request, tenant: str | None = None):
    s = scope(request, tenant)
    m = app.state.mock
    return {"stock": _only(m.list_stock()["stock"], s)} if m else upstream(app.state.aws.get, "/stock", _q(s))


@app.get("/api/reorders")
def reorders(request: Request, tenant: str | None = None):
    s = scope(request, tenant)
    m = app.state.mock
    if m:
        return {"reorders": _only(m.list_reorders()["reorders"], s)}
    return upstream(app.state.aws.get, "/reorders", _q(s))


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
def orders(request: Request, tenant: str | None = None):
    s = scope(request, tenant)
    m = app.state.mock
    return {"orders": _only(m.list_orders()["orders"], s)} if m else upstream(app.state.aws.get, "/orders", _q(s))


@app.get("/api/kpis")
def kpis(request: Request, tenant: str | None = None):
    """KPIs del ultimo dia con datos (daily-planning corre a las 05:00 de Lima por el dia anterior)."""
    s = scope(request, tenant)
    m = app.state.mock
    if m:
        data = m.kpis()
        return {**data, "machines": _only(data["machines"], s)}
    today = datetime.now(timezone.utc) - timedelta(hours=5)
    for back in range(1, 4):
        day = (today - timedelta(days=back)).strftime("%Y-%m-%d")
        data = upstream(app.state.aws.get, f"/kpis/{day}", _q(s))
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
    s = scope(request)
    m = app.state.mock
    if m:
        d = m.incident(incident_id)
        if not d or tenant_of_row(d) not in s:  # otro parque: como si no existiera
            raise HTTPException(404, "INCIDENT_NOT_FOUND")
        r = m.close(incident_id, payload)
        if r is None:
            raise HTTPException(404, "INCIDENT_NOT_FOUND")
        return r
    upstream(app.state.aws.get, f"/incidents/{incident_id}", _q(s))  # 404 si es de otro parque
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
                 incident_id: str | None = None, mine: int = 0, tenant: str | None = None):
    bad = status not in (None, "open", "in_review", "closed")
    bad = bad or bool(machine_id and not MID_RE.match(machine_id)) or bool(incident_id and not INC_RE.match(incident_id))
    if bad:
        raise HTTPException(400, "BAD_FILTER")
    if app.state.reports:
        rows = _mock(request, app.state.reports.list, status, machine_id, incident_id, bool(mine))
        return {"reports": _only(rows, scope(request, tenant))}
    pairs = (("status", status), ("machine_id", machine_id), ("incident_id", incident_id), ("mine", 1 if mine else None),
             ("tenant", tenant))
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


@app.get("/api/admin/technicians")
def admin_technicians(request: Request):
    """Tecnicos con su estado (aws/reports/api/admin.py, T35): {technicians, allowed: {zone_id, specialty}}."""
    if app.state.reports:
        return _mock(request, app.state.reports.list_techs)
    return _as_user(request, "GET", "/admin/technicians")


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
        created, entry = _mock(request, app.state.reports.add_type, body)
        return JSONResponse(entry, status_code=201 if created else 200)
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
