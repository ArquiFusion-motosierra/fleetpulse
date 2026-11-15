"""Lambda `admin` (T35): administracion en la HTTP API fp-api (payload v2, autorizador JWT `cognito`), solo rol
`admin` (claim `custom:role`); cualquier otro rol 403 y sin claims 401 (lib.web.dispatch).

Rutas:
- `GET /admin/technicians`: los tecnicos de ids.yaml con su estado en fp-state (`seeded: false` si aun no tienen
  item `TECH#`) y `allowed` (zonas y especialidades validas para el formulario). 200 `{technicians, allowed}`.
- `GET /admin/technicians/{tech_id}`: 200 `{technician}`; 404 sin item en fp-state.
- `PUT /admin/technicians/{tech_id}`: cuerpo con uno o mas de `zone_id` (zona de ids.yaml), `available` (bool) y
  `specialty` (de ids.yaml); otro campo o tipo -> 400. UpdateItem condicionado a que el tecnico exista (si no 404);
  cambiar `available` con un incidente en curso -> 409 (lo libera incident-flow al cerrar). Detalle en lib.crew.
  200 `{technician}`.
- `GET /admin/abnormality-types`: catalogo `CATALOG#abnormality_type` completo (con `created_at/updated_by`).
- `POST /admin/abnormality-types`: `{code, label_es, default_checks?}` crea o reemplaza el tipo; 201 nuevo, 200
  reemplazado. `default_checks` son los pasos que usa la plantilla (lib.recommend.fallback) para un tipo agregado.
- `DELETE /admin/abnormality-types/{code}`: 200 `{deleted}`; 404 si no existia.
Semilla del catalogo: perezosa, la misma que usa `GET /abnormality-types` de la Lambda `reports` (store.catalog con
recommend.seed_catalog, puts condicionales + marca `#seeded`); cada ruta del catalogo siembra antes de leer o
escribir, asi un tipo semilla borrado no reaparece. El formulario del operario y `POST /reports` leen ese mismo
catalogo, por eso los cambios del admin se ven al instante. Maquinas y repuestos son de solo lectura (ids.yaml).
"""
from __future__ import annotations

import re
from types import SimpleNamespace

from fplib import authz, clock, ids, log
from lib import crew, recommend, store, web

TECH_RE = re.compile(r"^TEC-[0-9]{2}$")
CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")  # contracts/report.schema.json#/$defs/abnormality_type
MAX_LABEL, MAX_CHECKS, MAX_STEP = 80, recommend.MAX_CHECKS, recommend.CAP["step"]
TYPE_FIELDS = {"code", "label_es", "default_checks"}


def _admin(event: dict) -> str:
    authz.require_role(event, "admin")
    return authz.user_of(event)[:128] or "admin"


def specialties() -> list[str]:
    return sorted({t["specialty"] for t in ids.technicians()} | set(ids.load()["specialty_by_failure_mode"].values()))


# --- tecnicos --------------------------------------------------------------------------------------------------------

def _tech_id(event: dict) -> str:
    tech_id = (event.get("pathParameters") or {}).get("tech_id") or ""
    if not TECH_RE.match(tech_id):
        raise web.HttpError(404, {"error": "technician not found", "tech_id": tech_id[:20]})
    return tech_id


def _tech_fields(body: dict) -> dict:
    extra = set(body) - {"zone_id", "available", "specialty"}
    if extra or not body:
        raise web.HttpError(400, {"error": "body must set zone_id, available and/or specialty only",
                                  "unknown": sorted(extra)})
    if "available" in body and not isinstance(body["available"], bool):
        raise web.HttpError(400, {"error": "available must be a boolean"})
    if "zone_id" in body and body["zone_id"] not in ids.zones():
        raise web.HttpError(400, {"error": "unknown zone_id", "allowed": sorted(ids.zones())})
    if "specialty" in body and body["specialty"] not in specialties():
        raise web.HttpError(400, {"error": "unknown specialty", "allowed": specialties()})
    return dict(body)


def list_technicians(event: dict, deps) -> dict:
    _admin(event)
    state = {it["tech_id"]: it for it in crew.list_all(deps.state)}
    rows = [{**t, **store.public(state[t["tech_id"]]), "seeded": True} if t["tech_id"] in state
            else {**t, "seeded": False} for t in ids.technicians()]
    rows += [{**store.public(it), "seeded": True} for tid, it in state.items()
             if tid not in {t["tech_id"] for t in ids.technicians()}]
    rows.sort(key=lambda r: r["tech_id"])
    return web.resp(200, {"technicians": rows, "allowed": {"zone_id": sorted(ids.zones()),
                                                           "specialty": specialties()}})


def get_technician(event: dict, deps) -> dict:
    _admin(event)
    tech_id = _tech_id(event)
    item = crew.get(deps.state, tech_id)
    if item is None:
        raise web.HttpError(404, {"error": "technician not found", "tech_id": tech_id})
    return web.resp(200, {"technician": store.public(item)})


def put_technician(event: dict, deps) -> dict:
    by = _admin(event)
    tech_id = _tech_id(event)
    fields = _tech_fields(web.body(event))
    try:
        item = crew.update(deps.state, tech_id, fields, clock.iso(clock.now()), by)
    except crew.NotFound:
        raise web.HttpError(404, {"error": "technician not found in fp-state", "tech_id": tech_id}) from None
    except crew.Busy as exc:
        log.event("tecnico_ocupado", tech_id=tech_id, current_incident=exc.current_incident, by=by)
        raise web.HttpError(409, {"error": "technician has an incident in progress; availability is released when "
                                           "it closes", "current_incident": exc.current_incident}) from None
    log.event("tecnico_actualizado", tech_id=tech_id, fields=sorted(fields), by=by, **fields)
    return web.resp(200, {"technician": store.public(item)})


# --- catalogo de tipos de anormalidad --------------------------------------------------------------------------------

def _catalog(deps, now_iso: str) -> list[dict]:
    return store.catalog(deps.registry, recommend.seed_catalog(), now_iso)


def _type_entry(body: dict) -> dict:
    extra = set(body) - TYPE_FIELDS
    if extra:
        raise web.HttpError(400, {"error": "unknown fields", "unknown": sorted(extra)})
    code, label = body.get("code"), body.get("label_es")
    if not isinstance(code, str) or not CODE_RE.match(code):
        raise web.HttpError(400, {"error": "code must match " + CODE_RE.pattern})
    if not isinstance(label, str) or not label.strip() or len(label.strip()) > MAX_LABEL:
        raise web.HttpError(400, {"error": f"label_es must be 1-{MAX_LABEL} characters"})
    checks = body.get("default_checks", [])
    if (not isinstance(checks, list) or len(checks) > MAX_CHECKS
            or not all(isinstance(s, str) and s.strip() and len(s.strip()) <= MAX_STEP for s in checks)):
        raise web.HttpError(400, {"error": f"default_checks must be up to {MAX_CHECKS} non-empty strings of at most "
                                           f"{MAX_STEP} characters"})
    return {"code": code, "label_es": label.strip(), "default_checks": [s.strip() for s in checks]}


def list_types(event: dict, deps) -> dict:
    _admin(event)
    return web.resp(200, {"abnormality_types": [store.public(t) for t in _catalog(deps, clock.iso(clock.now()))]})


def put_type(event: dict, deps) -> dict:
    by = _admin(event)
    entry = _type_entry(web.body(event))
    now_iso = clock.iso(clock.now())
    _catalog(deps, now_iso)
    created = store.put_type(deps.registry, entry, now_iso, by)
    log.event("tipo_anormalidad_guardado", code=entry["code"], created=created, checks=len(entry["default_checks"]),
              by=by)
    return web.resp(201 if created else 200, {"abnormality_type": {**entry, "updated_at": now_iso, "updated_by": by}})


def delete_type(event: dict, deps) -> dict:
    by = _admin(event)
    code = (event.get("pathParameters") or {}).get("code") or ""
    if not CODE_RE.match(code):
        raise web.HttpError(404, {"error": "abnormality type not found"})
    _catalog(deps, clock.iso(clock.now()))
    if not store.delete_type(deps.registry, code):
        raise web.HttpError(404, {"error": "abnormality type not found", "code": code})
    log.event("tipo_anormalidad_borrado", code=code, by=by)
    return web.resp(200, {"deleted": code})


ROUTES = {
    "GET /admin/technicians": list_technicians,
    "GET /admin/technicians/{tech_id}": get_technician,
    "PUT /admin/technicians/{tech_id}": put_technician,
    "GET /admin/abnormality-types": list_types,
    "POST /admin/abnormality-types": put_type,
    "DELETE /admin/abnormality-types/{code}": delete_type,
}


def handler(event, context=None, registry=None, state=None):
    deps = SimpleNamespace(registry=registry or store.table_from_env("REGISTRY_TABLE"),
                           state=state or store.table_from_env("STATE_TABLE"))
    return web.dispatch(event, ROUTES, deps)
