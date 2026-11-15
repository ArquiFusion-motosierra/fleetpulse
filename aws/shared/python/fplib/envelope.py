"""Construccion y validacion del sobre de eventos de dominio (contrato `envelope`)."""
from __future__ import annotations

import uuid
from datetime import datetime

from fplib import clock, schema


def build(
    event_type: str,
    source: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict,
    *,
    incident_id: str | None = None,
    machine_id: str | None = None,
    occurred_at: datetime | None = None,
    trace_id: str | None = None,
) -> dict:
    env = {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "schema_version": 1,
        "source": source,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": clock.iso(occurred_at or clock.now()),
        "payload": payload,
    }
    for key, val in (("incident_id", incident_id), ("machine_id", machine_id), ("trace_id", trace_id)):
        if val is not None:
            env[key] = val
    schema.validate("envelope", env)
    family = event_type.split(".", 1)[0]
    if family in ("incident", "part", "report"):
        schema.validate(family, env)
    return env
