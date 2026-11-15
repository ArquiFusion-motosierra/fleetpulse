"""Escrituras de fp-state: lecturas, META de maquina y contexto. Funciones puras sobre un `Table` inyectado.

Idempotencia: las lecturas y el contexto usan clave determinista (`R#<ts>`, `<ts>#<context_id>`), asi que
reescribir el mismo lote deja el mismo estado. `update_meta` nunca retrocede `last_ts`.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from botocore.exceptions import ClientError

from fplib import clock, geohash, ids

READING_TTL = timedelta(days=3)
CONTEXT_TTL = timedelta(hours=24)


def _ttl(ts: str, delta: timedelta) -> int:
    return int((clock.parse(ts) + delta).timestamp())


class TenantMismatch(ValueError):
    """El lote declara un parque y trae maquinas de otro (o una lectura declara un parque que no es el suyo)."""


def assign_tenants(batch: dict) -> dict[str, int]:
    """Fija `tenant_id` en cada lectura segun el catalogo (fuente de verdad: la maquina pertenece a un parque).

    Un gateway de un parque no puede inyectar lecturas en otro: si el lote o la lectura declaran un tenant distinto
    al de la maquina, TenantMismatch. Devuelve cuantas lecturas hay por parque."""
    declared = batch.get("tenant_id")
    counts: dict[str, int] = {}
    for r in batch["readings"]:
        tenant = ids.tenant_of_machine(r["machine_id"])
        if (declared and declared != tenant) or (r.get("tenant_id") and r["tenant_id"] != tenant):
            raise TenantMismatch(f"{r['machine_id']} pertenece al parque {tenant}")
        r["tenant_id"] = tenant
        counts[tenant] = counts.get(tenant, 0) + 1
    return counts


def put_readings(table, batch: dict, now: datetime) -> int:
    """Escribe cada lectura como `MACHINE#<id>` / `R#<ts>` con ttl = ts + 3 dias (y `tenant_id`). Devuelve cuantas."""
    with table.batch_writer(overwrite_by_pkeys=["PK", "SK"]) as w:
        for r in batch["readings"]:
            w.put_item(Item={
                **r,
                "tenant_id": r.get("tenant_id") or ids.tenant_of_machine(r["machine_id"]),
                "PK": f"MACHINE#{r['machine_id']}",
                "SK": f"R#{r['ts']}",
                "batch_id": batch["batch_id"],
                "ttl": _ttl(r["ts"], READING_TTL),
            })
    return len(batch["readings"])


def latest_by_machine(batch: dict) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for r in batch["readings"]:
        cur = latest.get(r["machine_id"])
        if cur is None or r["ts"] > cur["ts"]:
            latest[r["machine_id"]] = r
    return latest


def update_meta(table, latest_by_machine: dict[str, dict], now: datetime) -> None:
    """Actualiza META solo si la lectura es mas nueva. No toca `baseline`/`baseline_n`; `status` solo se crea."""
    for machine_id, r in latest_by_machine.items():
        try:
            table.update_item(
                Key={"PK": f"MACHINE#{machine_id}", "SK": "META"},
                UpdateExpression=(
                    "SET machine_id = :id, model = :model, serial = :serial, zone_id = :zone, lat = :lat, "
                    "lon = :lon, geohash6 = :gh, hours_total = :ht, hours_idle = :hi, last_ts = :ts, "
                    "updated_at = :now, tenant_id = :tenant, GSI1PK = :g1pk, GSI1SK = :g1sk, #s = if_not_exists(#s, :ok)"),
                ConditionExpression="attribute_not_exists(last_ts) OR last_ts < :ts",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":id": machine_id, ":model": r["model"], ":serial": r["serial"], ":zone": r["zone_id"],
                    ":lat": r["lat"], ":lon": r["lon"],
                    ":gh": geohash.encode(float(r["lat"]), float(r["lon"]), 6),
                    ":ht": r["cumulative_operating_hours"], ":hi": r["cumulative_idle_hours"],
                    ":ts": r["ts"], ":now": clock.iso(now),
                    ":tenant": r.get("tenant_id") or ids.tenant_of_machine(machine_id),
                    ":g1pk": f"ZONE#{r['zone_id']}", ":g1sk": f"MACHINE#{machine_id}", ":ok": "ok",
                },
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise


def put_context(table, items: list[dict]) -> int:
    """Guarda eventos de contexto bajo `CTX#<zone_id>` / `<ts>#<context_id>` con ttl de 24 h."""
    with table.batch_writer(overwrite_by_pkeys=["PK", "SK"]) as w:
        for c in items:
            w.put_item(Item={
                **c,
                "PK": f"CTX#{c['zone_id']}",
                "SK": f"{c['ts']}#{c['context_id']}",
                "ttl": _ttl(c["ts"], CONTEXT_TTL),
            })
    return len(items)

