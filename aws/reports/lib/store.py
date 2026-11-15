"""Acceso a fp-registry para la Lambda `reports`: reportes de anormalidad, su linea de tiempo, el incidente abierto de
la maquina y el catalogo de tipos de anormalidad.

Modelo (docs/superpowers/plans/2026-11-15-reportes-operario-roles-admin.md, seccion 2):
- `REPORT#<report_id>` / `META` (GSI1 `MACHINE#<id>` / `RPT#<reported_at>`, GSI2 `RSTATUS#<status>` / `<reported_at>`).
  Los prefijos `RPT#` y `RSTATUS#` no chocan con los del incidente (`INC#`, `STATUS#`) en los mismos GSI.
- `REPORT#<report_id>` / `EV#<occurred_at>#<event_id>`: `event_type, type, source, payload` (sobre completo como JSON),
  igual que los `EV#` del incidente.
- `CATALOG#abnormality_type` / `<code>`: `code, label_es, default_checks[]`; `#seeded` marca que la semilla ya se
  escribio (asi un tipo borrado por el admin no reaparece).

Idempotencia: crear el META exige `attribute_not_exists(PK)` y cerrarlo exige que no este cerrado, en la misma
`TransactWriteItems` que el EV# y el outbox; si una condicion falla no se escribe nada y se lanza `ConditionFailed`.
La semilla del catalogo usa puts condicionales. `_transact`, `to_ddb`/`from_ddb` y la consulta del incidente abierto
se copian de aws/incidents/lib/registry.py para no importar codigo entre servicios.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal

from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from fplib import outbox

_DES = TypeDeserializer()
_tables: dict = {}
STATUSES = ("open", "in_review", "closed")
CATALOG_PK = "CATALOG#abnormality_type"
SEEDED_SK = "#seeded"
MAX_ITEMS = 1000  # tope por consulta: la demo maneja decenas de reportes
HIDDEN = {"PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK"}


class ConditionFailed(Exception):
    """Una condicion de la transaccion no se cumplio y no se escribio nada. `index`: posicion del item que fallo
    (0 = el META del reporte)."""

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
    """Decimal anidados -> int/float, para devolver JSON."""
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, dict):
        return {k: from_ddb(v) for k, v in o.items()}
    if isinstance(o, list):
        return [from_ddb(v) for v in o]
    return o


def public(item: dict) -> dict:
    """Item sin claves de tabla ni Decimal, listo para la respuesta JSON."""
    return from_ddb({k: v for k, v in item.items() if k not in HIDDEN})


def query_all(table, limit: int = MAX_ITEMS, **kwargs):
    """Paginas de un `Query` hasta `limit` items."""
    n = 0
    while True:
        page = table.query(**kwargs)
        for it in page["Items"]:
            yield it
            n += 1
            if n >= limit:
                return
        if "LastEvaluatedKey" not in page:
            return
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


# --- reportes -------------------------------------------------------------------------------------------------------

def report_key(report_id: str) -> dict:
    return {"PK": f"REPORT#{report_id}", "SK": "META"}


def incident_key(incident_id: str) -> dict:
    return {"PK": f"INCIDENT#{incident_id}", "SK": "META"}


def event_item(pk: str, envelope: dict) -> dict:
    """Item `EV#` bajo `pk` (`REPORT#<id>` o `INCIDENT#<id>`) con el sobre completo como JSON. `type` repite
    `event_type` porque la API de la consola (aws/registry/reg/api.py) arma la linea de tiempo con el."""
    return {
        "PK": pk, "SK": f"EV#{envelope['occurred_at']}#{envelope['event_id']}",
        "event_type": envelope["event_type"], "type": envelope["event_type"], "source": envelope["source"],
        "payload": json.dumps(envelope, separators=(",", ":")),
    }


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


def put_report(registry, outbox_tbl_name: str, item: dict, envelope: dict,
               extra_items: list[dict] | None = None) -> None:
    """META (solo si no existe) + EV# del reporte + `report.created` en el outbox + `extra_items` (ej. el EV#
    `report.linked` del incidente), todo en una transaccion. `ConditionFailed(0)` si el reporte ya existe."""
    _transact(registry.meta.client, [
        {"Put": {"TableName": registry.name, "Item": to_ddb(item), "ConditionExpression": "attribute_not_exists(PK)"}},
        {"Put": {"TableName": registry.name, "Item": event_item(item["PK"], envelope)}},
        _outbox_put(envelope, outbox_tbl_name),
        *(extra_items or []),
    ])


def close_report(registry, outbox_tbl_name: str, report_id: str, envelope: dict, attrs: dict, *,
                 tech_id: str | None = None, extra_items: list[dict] | None = None) -> None:
    """SET `attrs` + `status = closed` (y GSI2PK) en el META + EV# + `report.closed` en el outbox + `extra_items`.

    Condicion: el META existe y no esta cerrado; con `tech_id` (cierre de un tecnico) ademas el reporte no tiene
    tecnico asignado o es ese mismo. `ConditionFailed(0)` si no se cumple: no se escribio nada.
    """
    names, values, sets = {"#st": "status"}, {":closed": "closed", ":g2": "RSTATUS#closed"}, ["#st = :closed",
                                                                                              "GSI2PK = :g2"]
    for i, (k, v) in enumerate(attrs.items()):
        names[f"#a{i}"], values[f":a{i}"] = k, v
        sets.append(f"#a{i} = :a{i}")
    cond = "attribute_exists(PK) AND #st <> :closed"
    if tech_id is not None:
        names["#tech"], values[":tech"] = "assigned_tech_id", tech_id
        cond += " AND (attribute_not_exists(#tech) OR #tech = :tech)"
    _transact(registry.meta.client, [
        {"Update": {"TableName": registry.name, "Key": report_key(report_id), "UpdateExpression": "SET " + ", ".join(sets),
                    "ConditionExpression": cond, "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": to_ddb(values)}},
        {"Put": {"TableName": registry.name, "Item": event_item(f"REPORT#{report_id}", envelope)}},
        _outbox_put(envelope, outbox_tbl_name),
        *(extra_items or []),
    ])


def get_report(registry, report_id: str) -> dict | None:
    return registry.get_item(Key=report_key(report_id), ConsistentRead=True).get("Item")


def timeline(registry, report_id: str) -> list[dict]:
    """EV# del reporte en orden cronologico: `{type, source, ts, payload}` (payload del sobre)."""
    out = []
    key = Key("PK").eq(f"REPORT#{report_id}") & Key("SK").begins_with("EV#")
    for it in query_all(registry, KeyConditionExpression=key):
        env = json.loads(it["payload"])
        out.append({"type": it.get("event_type"), "source": it.get("source"), "ts": env.get("occurred_at"),
                    "event_id": env.get("event_id"), "payload": env.get("payload")})
    return out


def reports_by_status(registry, status: str) -> list[dict]:
    """Reportes con ese estado (GSI2 `RSTATUS#<status>`), el mas reciente primero."""
    return list(query_all(registry, IndexName="GSI2", ScanIndexForward=False,
                          KeyConditionExpression=Key("GSI2PK").eq(f"RSTATUS#{status}")))


def reports_by_machine(registry, machine_id: str) -> list[dict]:
    """Reportes de la maquina (GSI1 `MACHINE#<id>` / `RPT#`), el mas reciente primero."""
    key = Key("GSI1PK").eq(f"MACHINE#{machine_id}") & Key("GSI1SK").begins_with("RPT#")
    return list(query_all(registry, IndexName="GSI1", ScanIndexForward=False, KeyConditionExpression=key))


def set_assigned(registry, report_id: str, tech_id: str) -> bool:
    """Copia al reporte el tecnico que incident-flow asigno al incidente vinculado (lectura perezosa) y lo pasa a
    `in_review`. Condicionado a que siga `open` y sin tecnico: True si escribio, False si otro ya lo hizo."""
    try:
        registry.update_item(
            Key=report_key(report_id),
            UpdateExpression="SET assigned_tech_id = :t, #st = :rev, GSI2PK = :g2",
            ConditionExpression="attribute_exists(PK) AND #st = :open AND attribute_not_exists(assigned_tech_id)",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":t": tech_id, ":rev": "in_review", ":open": "open", ":g2": "RSTATUS#in_review"})
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False
    return True


# --- incidente (solo lectura) ---------------------------------------------------------------------------------------

def get_incident(registry, incident_id: str) -> dict | None:
    return registry.get_item(Key=incident_key(incident_id), ConsistentRead=True).get("Item")


def open_incident_for(registry, machine_id: str) -> dict | None:
    """Incidente abierto mas reciente de la maquina (GSI1 `MACHINE#<id>` / `INC#`), o None. Copia de
    aws/incidents/lib/registry.open_incident_for."""
    key = Key("GSI1PK").eq(f"MACHINE#{machine_id}") & Key("GSI1SK").begins_with("INC#")
    return next(query_all(registry, IndexName="GSI1", ScanIndexForward=False, KeyConditionExpression=key,
                          FilterExpression=Attr("status").eq("open")), None)


# --- catalogo de tipos de anormalidad ---------------------------------------------------------------------------------

def catalog(registry, seed: list[dict], now_iso: str) -> list[dict]:
    """Tipos del catalogo ordenados por codigo. La primera lectura (sin `#seeded`) escribe `seed` con puts
    condicionales (no pisa tipos existentes) y la marca; despues un tipo borrado no vuelve."""
    items = list(query_all(registry, KeyConditionExpression=Key("PK").eq(CATALOG_PK)))
    if not any(i["SK"] == SEEDED_SK for i in items):
        have = {i["SK"] for i in items}
        for entry in seed:
            if entry["code"] not in have:
                _put_if_absent(registry, {"PK": CATALOG_PK, "SK": entry["code"], **entry, "created_at": now_iso})
        _put_if_absent(registry, {"PK": CATALOG_PK, "SK": SEEDED_SK, "seeded_at": now_iso})
        items = list(query_all(registry, KeyConditionExpression=Key("PK").eq(CATALOG_PK)))
    return sorted((i for i in items if i["SK"] != SEEDED_SK), key=lambda i: i["SK"])


def _put_if_absent(registry, item: dict) -> bool:
    try:
        registry.put_item(Item=to_ddb(item), ConditionExpression="attribute_not_exists(PK)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False
    return True


def put_type(registry, entry: dict, now_iso: str, by: str) -> bool:
    """Crea o reemplaza `CATALOG#abnormality_type/<code>`. True si era nuevo."""
    old = registry.put_item(Item=to_ddb({"PK": CATALOG_PK, "SK": entry["code"], **entry, "updated_at": now_iso,
                                         "updated_by": by}), ReturnValues="ALL_OLD").get("Attributes")
    return not old  # sin item previo DynamoDB omite Attributes (moto devuelve {})


def delete_type(registry, code: str) -> bool:
    """Borra el tipo; False si no existia."""
    try:
        registry.delete_item(Key={"PK": CATALOG_PK, "SK": code}, ConditionExpression="attribute_exists(PK)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False
    return True
