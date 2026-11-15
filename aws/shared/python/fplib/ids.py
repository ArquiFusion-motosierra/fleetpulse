"""IDs de dominio (incidentes, grupos) y acceso a `ids.yaml` (maquinas, tecnicos, zonas, BOM) y a la ficha del
fabricante `oem_specs.yaml` (T26). Solo lectura de archivos del paquete o de contracts/, con cache en memoria."""
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


@lru_cache(maxsize=1)
def load() -> dict:
    bundled = HERE / "data" / "ids.json"
    if bundled.is_file():
        return json.loads(bundled.read_text())
    import yaml  # solo en desarrollo; en la layer existe data/ids.json
    return yaml.safe_load((schema.contracts_dir() / "ids.yaml").read_text())


def machines() -> dict[str, dict]:
    return {m["machine_id"]: m for m in load()["machines"]}


def technicians() -> list[dict]:
    return list(load()["technicians"])


def zones() -> dict[str, dict]:
    return {z["zone_id"]: z for z in load()["zones"]}


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
