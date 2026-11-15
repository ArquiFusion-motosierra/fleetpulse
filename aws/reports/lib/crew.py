"""Tecnicos en fp-state (`TECH#<id>` / `META`) para la Lambda `admin`: listar, leer y editar zona, disponibilidad y
especialidad. (No se llama `technicians.py` porque CI corre todos los servicios en una sola sesion de pytest y
`lib.technicians` ya es el de aws/incidents.)

Atributos: los de aws/registry/reg/seed.py, que son los que lee incident-flow (aws/incidents/lib/technicians.py):
`tech_id, name, specialty, zone_id, lat, lon, available`, GSI1 `ZONE#<zona>` / `TECH#<id>`; incident-flow agrega
`current_incident` (null al liberar). El admin escribe solo `zone_id` (+ GSI1PK y lat/lon al centroide de la zona,
porque el ranking mide la distancia con ellos), `specialty`, `available` y `updated_at/updated_by`; nunca
`current_incident`. Cambiar `available` exige que el tecnico no tenga incidente (`current_incident` ausente o null):
si no, la reserva o la liberacion de incident-flow quedarian inconsistentes.
"""
from __future__ import annotations

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from fplib import ids
from lib import store


class Busy(Exception):
    """El tecnico tiene un incidente en curso; `current_incident` es su id."""

    def __init__(self, current_incident: str):
        super().__init__(current_incident)
        self.current_incident = current_incident


class NotFound(Exception):
    """No hay item `TECH#<id>` en fp-state."""


def key(tech_id: str) -> dict:
    return {"PK": f"TECH#{tech_id}", "SK": "META"}


def get(state, tech_id: str) -> dict | None:
    return state.get_item(Key=key(tech_id), ConsistentRead=True).get("Item")


def list_all(state) -> list[dict]:
    """Items `TECH#` de fp-state por GSI1 `ZONE#<zona>` (Query por zona y no Scan: fp-state guarda las lecturas)."""
    return [it for z in ids.zones() for it in store.query_all(
        state, IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq(f"ZONE#{z}") & Key("GSI1SK").begins_with(
            "TECH#"))]


def update(state, tech_id: str, fields: dict, now_iso: str, by: str) -> dict:
    """SET de `fields` (ya validados) con la condicion de que el tecnico exista y, si cambia `available`, que este
    libre. Devuelve el item nuevo; `NotFound` o `Busy` si la condicion falla (no se escribio nada)."""
    values = {**fields, "updated_at": now_iso, "updated_by": by}
    if "zone_id" in fields:
        z = ids.zones()[fields["zone_id"]]
        values.update(GSI1PK=f"ZONE#{fields['zone_id']}", lat=z["lat"], lon=z["lon"])
    names = {f"#f{i}": k for i, k in enumerate(values)}
    expr_values = {f":f{i}": v for i, v in enumerate(values.values())}
    cond = "attribute_exists(PK)"
    if "available" in fields:
        names["#ci"] = "current_incident"
        expr_values[":null"] = "NULL"
        cond += " AND (attribute_not_exists(#ci) OR attribute_type(#ci, :null))"
    try:
        out = state.update_item(Key=key(tech_id), UpdateExpression="SET " + ", ".join(f"#f{i} = :f{i}" for i in
                                                                                       range(len(values))),
                                ConditionExpression=cond, ExpressionAttributeNames=names,
                                ExpressionAttributeValues=store.to_ddb(expr_values), ReturnValues="ALL_NEW")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        current = get(state, tech_id)
        if current is None:
            raise NotFound(tech_id) from None
        raise Busy(current.get("current_incident")) from None
    return out["Attributes"]
