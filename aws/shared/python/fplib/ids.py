"""IDs de dominio (incidentes, grupos) y acceso a `ids.yaml` (maquinas, tecnicos, zonas, BOM)."""
from __future__ import annotations

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
