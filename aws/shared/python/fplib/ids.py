"""IDs de dominio (incidentes, grupos), catalogo de los parques (`tenants.yaml` + `ids.yaml` de mineria +
`tenants/<id>.yaml`: maquinas, tecnicos, zonas, BOM) y ficha del fabricante `oem_specs.yaml` (T26). Solo lectura de
archivos del paquete o de contracts/, con cache en memoria.

Multi-tenancy (docs/superpowers/plans/2026-11-15-multi-tenancy.md): `load()` es la union de los catalogos de todos los
parques y cada maquina, zona, estacion, tecnico, linea de BOM y de stock lleva `tenant_id`. Los IDs son unicos entre
parques, asi que `tenant_of_machine` / `tenant_of_zone` dan el tenant de cualquier dato viejo sin `tenant_id`."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from functools import lru_cache

from fplib import clock, schema

HERE = schema.HERE


def incident_id(machine_id: str, ts: datetime) -> str:
    return f"INC-{machine_id}-{clock.window15(ts)}"


def group_id(geohash6: str, ts: datetime) -> str:
    return f"GRP-{geohash6}-{ts.astimezone(timezone.utc).strftime('%Y%m%dT%H')}"


DEFAULT_TENANT = "mineria"
_TENANT_LISTS = ("zones", "fuel_stations", "models", "machines", "technicians", "bom", "part_stock")


def merge_catalogs(contracts_dir) -> dict:
    """Union de los catalogos de `tenants.yaml` (lo usan build.sh y sync_ids.py para vendorizar `ids.json`).

    Sin `tenants.yaml` devuelve `ids.yaml` con `tenant_id: mineria` en cada entidad (un solo parque)."""
    import yaml  # solo en desarrollo / build; en la layer existe data/ids.json
    from pathlib import Path

    contracts_dir = Path(contracts_dir)
    reg_path = contracts_dir / "tenants.yaml"
    if reg_path.is_file():
        reg = yaml.safe_load(reg_path.read_text(encoding="utf-8"))
    else:
        reg = {"default": DEFAULT_TENANT, "tenants": [{"tenant_id": DEFAULT_TENANT, "catalog": "ids.yaml"}]}
    default = reg.get("default", DEFAULT_TENANT)
    merged: dict = {}
    parts: dict[str, dict] = {}
    tenants = []
    for t in reg["tenants"]:
        cat = yaml.safe_load((contracts_dir / t["catalog"]).read_text(encoding="utf-8"))
        tid = t["tenant_id"]
        if tid == default:  # campos globales (failure_modes, specialty_by_failure_mode, timezone) del parque base
            merged.update({k: v for k, v in cat.items() if k not in _TENANT_LISTS + ("parts", "tenant_id")})
        for key in _TENANT_LISTS:
            merged.setdefault(key, []).extend({**row, "tenant_id": tid} for row in cat.get(key) or [])
        for p in cat.get("parts") or []:
            parts.setdefault(p["part_id"], p)
        tenants.append({k: v for k, v in t.items() if k != "catalog"}
                       | {"operation": t.get("operation") or cat.get("operation"),
                          "zones": [z["zone_id"] for z in cat.get("zones") or []]})
    merged["parts"] = sorted(parts.values(), key=lambda p: p["part_id"])
    merged["tenants"] = tenants
    merged["default_tenant"] = default
    merged["global_roles"] = reg.get("global_roles", [])
    merged["global_group"] = reg.get("global_group")
    return merged


@lru_cache(maxsize=1)
def load() -> dict:
    bundled = HERE / "data" / "ids.json"
    if bundled.is_file():
        return json.loads(bundled.read_text())
    return merge_catalogs(schema.contracts_dir())


def default_tenant() -> str:
    return load().get("default_tenant") or DEFAULT_TENANT


def tenants() -> dict[str, dict]:
    """Parques registrados: `{tenant_id: {name, operation, zones, center, cognito_group}}`."""
    return {t["tenant_id"]: t for t in load().get("tenants") or [{"tenant_id": DEFAULT_TENANT}]}


def _of(row: dict) -> str:
    return row.get("tenant_id") or default_tenant()


def machines(tenant: str | None = None) -> dict[str, dict]:
    return {m["machine_id"]: m for m in load()["machines"] if tenant is None or _of(m) == tenant}


def technicians(tenant: str | None = None) -> list[dict]:
    return [t for t in load()["technicians"] if tenant is None or _of(t) == tenant]


def zones(tenant: str | None = None) -> dict[str, dict]:
    return {z["zone_id"]: z for z in load()["zones"] if tenant is None or _of(z) == tenant}


def tenant_of_machine(machine_id: str | None) -> str:
    """Tenant de la maquina segun el catalogo; el por defecto si no esta (dato viejo o id desconocido)."""
    m = machines().get(machine_id or "")
    return _of(m) if m else default_tenant()


def tenant_of_zone(zone_id: str | None) -> str:
    z = zones().get(zone_id or "")
    return _of(z) if z else default_tenant()


def tenant_of_technician(tech_id: str | None) -> str:
    t = next((t for t in load()["technicians"] if t["tech_id"] == tech_id), None)
    return _of(t) if t else default_tenant()


def tenant_of_item(item: dict) -> str:
    """Tenant de un item de DynamoDB o evento: `tenant_id` si lo trae; si no, por su maquina o su zona."""
    if item.get("tenant_id"):
        return item["tenant_id"]
    if item.get("machine_id"):
        return tenant_of_machine(item["machine_id"])
    if item.get("zone_id"):
        return tenant_of_zone(item["zone_id"])
    return default_tenant()


def bom_for(model: str, failure_mode: str) -> list[dict]:
    return [
        {"part_id": b["part_id"], "qty": b["qty"]}
        for b in load()["bom"]
        if b["model_id"] == model and b["failure_mode"] == failure_mode
    ]


def specialty_for(failure_mode: str) -> str:
    return load()["specialty_by_failure_mode"][failure_mode]


@lru_cache(maxsize=1)
def oem_catalog() -> dict:
    """`oem_specs.yaml` completo (T26), o {} si todavia no existe.

    En la layer, build.sh la deja como `data/oem_specs.json` (la layer no trae PyYAML); en desarrollo se lee el YAML
    de contracts/.
    """
    bundled = HERE / "data" / "oem_specs.json"
    if bundled.is_file():
        return json.loads(bundled.read_text()) or {}
    try:
        path = schema.contracts_dir() / "oem_specs.yaml"
    except FileNotFoundError:
        return {}
    if not path.is_file():
        return {}
    import yaml  # solo en desarrollo
    return yaml.safe_load(path.read_text()) or {}


def oem_specs(model: str) -> dict:
    """Ficha del fabricante del modelo (`models.<model>` de oem_specs.yaml: limits, service_schedule,
    typical_failures, ...), copia independiente; {} si no hay ficha o el modelo no aparece."""
    return copy.deepcopy((oem_catalog().get("models") or {}).get(model) or {})
