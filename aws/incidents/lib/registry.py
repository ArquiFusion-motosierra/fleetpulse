"""Acceso a fp-registry para incident-flow: META del incidente, sus EV# y el evento en fp-outbox en una transaccion.

Modelo (contracts/secrets.md):
`INCIDENT#<id>` / `META` (GSI1 `MACHINE#<id>` / `INC#<opened_at>`, GSI2 `STATUS#<status>` / `<opened_at>`) y
`INCIDENT#<id>` / `EV#<occurred_at>#<event_id>` con el sobre completo como JSON.

Idempotencia: toda escritura de META lleva una condicion (`attribute_not_exists(PK)` al crear, valores esperados al
actualizar) en la misma `TransactWriteItems` que el EV# y el outbox. Si la condicion falla no se escribe nada y se
lanza `ConditionFailed`, asi que reintentar un paso nunca duplica eventos.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal

from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from fplib import clock, ids, outbox

_DES = TypeDeserializer()
_tables: dict = {}


class ConditionFailed(Exception):
    """Una condicion de la transaccion no se cumplio y no se escribio nada. `index`: posicion del item que fallo
    (0 = el META del incidente)."""

    def __init__(self, index: int = 0):
        super().__init__(f"condicion fallida en el item {index} de la transaccion")
        self.index = index


def table_from_env(env_var: str):
    """`Table` de boto3 para el nombre en la variable de entorno `env_var`, creado una vez y solo al usarse."""
    if env_var not in _tables:
        import boto3
        _tables[env_var] = boto3.resource("dynamodb").Table(os.environ[env_var])
    return _tables[env_var]


def to_ddb(o):
    """floats anidados -> Decimal (DynamoDB no acepta float)."""
    if isinstance(o, float):
        return Decimal(str(o))
    if isinstance(o, dict):
        return {k: to_ddb(v) for k, v in o.items()}
    if isinstance(o, list):
        return [to_ddb(v) for v in o]
    return o


def from_ddb(o):
    """Decimal anidados -> int/float, para devolver JSON a Step Functions."""
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, dict):
        return {k: from_ddb(v) for k, v in o.items()}
    if isinstance(o, list):
        return [from_ddb(v) for v in o]
    return o


def meta_key(incident_id: str) -> dict:
    return {"PK": f"INCIDENT#{incident_id}", "SK": "META"}


def event_item(envelope: dict) -> dict:
    """Item `EV#` del incidente para un sobre ya validado (`payload` = sobre completo como JSON).

    `type` repite `event_type` porque la API de la consola (aws/registry/reg/api.py) arma la linea de tiempo con el.
    """
    return {
        **meta_key(envelope["incident_id"]), "SK": f"EV#{envelope['occurred_at']}#{envelope['event_id']}",
        "event_type": envelope["event_type"], "type": envelope["event_type"], "source": envelope["source"],
        "payload": json.dumps(envelope, separators=(",", ":")),
    }


def query_all(table, **kwargs):
    """Todas las paginas de un `Query`."""
    while True:
        page = table.query(**kwargs)
        yield from page["Items"]
        if "LastEvaluatedKey" not in page:
            return
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _machine_incidents(table, machine_id: str, flt):
    """Incidentes de la maquina (GSI1), el mas reciente primero, filtrados por `flt`."""
    key = Key("GSI1PK").eq(f"MACHINE#{machine_id}") & Key("GSI1SK").begins_with("INC#")
    return query_all(table, IndexName="GSI1", ScanIndexForward=False, KeyConditionExpression=key,
                      FilterExpression=flt)


def open_incident_for(table, machine_id: str) -> dict | None:
    """Incidente abierto mas reciente de la maquina (GSI1), o None."""
    return next(_machine_incidents(table, machine_id, Attr("status").eq("open")), None)


def recent_closed_same_mode(table, machine_id: str, failure_mode: str, since: datetime) -> bool:
    """True si la maquina cerro un incidente con el mismo modo de falla desde `since`."""
    flt = Attr("status").eq("closed") & Attr("failure_mode").eq(failure_mode) & Attr("closed_at").gte(
        clock.iso(since))
    return next(_machine_incidents(table, machine_id, flt), None) is not None


def get_incident(table, incident_id: str) -> dict | None:
    return table.get_item(Key=meta_key(incident_id), ConsistentRead=True).get("Item")


def _outbox_put(envelope: dict, outbox_tbl_name: str) -> dict:
    """`Put` de fplib.outbox con el item en tipos nativos: el cliente de un `Table` ya serializa por su cuenta."""
    put = outbox.transact_items(envelope, table=outbox_tbl_name)["Put"]
    return {"Put": {**put, "Item": {k: _DES.deserialize(v) for k, v in put["Item"].items()}}}


def _transact(client, items: list[dict]) -> None:
    """`client` es el de un `Table` (`table.meta.client`, tipos nativos); items[0] es la escritura del META."""
    try:
        client.transact_write_items(TransactItems=items)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        reasons = [r.get("Code") for r in exc.response.get("CancellationReasons") or []]
        if "ConditionalCheckFailed" not in reasons:
            raise
        raise ConditionFailed(reasons.index("ConditionalCheckFailed")) from exc


def put_incident_tx(registry_tbl_name: str, outbox_tbl_name: str, incident_item: dict, envelope: dict,
                    event_item: dict, client) -> None:
    """Crea META (solo si no existe) + EV# + outbox en una transaccion; `ConditionFailed` si el META ya existe.

    `client` es el de un `Table` de boto3 (`table.meta.client`), que recibe tipos nativos.
    """
    _transact(client, [
        {"Put": {"TableName": registry_tbl_name, "Item": to_ddb(incident_item),
                 "ConditionExpression": "attribute_not_exists(PK)"}},
        {"Put": {"TableName": registry_tbl_name, "Item": to_ddb(event_item)}},
        _outbox_put(envelope, outbox_tbl_name),
    ])


def update_incident(table, incident_id: str, *, envelope: dict, outbox_tbl_name: str, expect: dict | None = None,
                    extra_items: list[dict] | None = None, **attrs) -> None:
    """SET `attrs` en el META existente + EV# + evento `envelope` en el outbox, en una sola transaccion.

    `expect` = {atributo: valor esperado}; `None` exige que el atributo no exista. `extra_items`: elementos de
    TransactWriteItems que van en la misma transaccion, desde la posicion 3. `ConditionFailed(index)` dice cual
    fallo: 0 si el META no existe o no cumple `expect`.
    """
    names, values, sets, conds = {}, {}, [], ["attribute_exists(PK)"]
    for i, (k, v) in enumerate(attrs.items()):
        names[f"#a{i}"], values[f":a{i}"] = k, v
        sets.append(f"#a{i} = :a{i}")
    for i, (k, v) in enumerate((expect or {}).items()):
        names[f"#e{i}"] = k
        if v is None:
            conds.append(f"attribute_not_exists(#e{i})")
        else:
            values[f":e{i}"] = v
            conds.append(f"#e{i} = :e{i}")
    _transact(table.meta.client, [
        {"Update": {"TableName": table.name, "Key": meta_key(incident_id),
                    "UpdateExpression": "SET " + ", ".join(sets), "ConditionExpression": " AND ".join(conds),
                    "ExpressionAttributeNames": names, "ExpressionAttributeValues": to_ddb(values)}},
        {"Put": {"TableName": table.name, "Item": event_item(envelope)}},
        _outbox_put(envelope, outbox_tbl_name),
        *(extra_items or []),
    ])


def tenant_of(item: dict) -> str:
    """Parque del incidente: `tenant_id` del META o, si es anterior al multi-tenancy, el de su maquina."""
    return ids.tenant_of_item(item)
