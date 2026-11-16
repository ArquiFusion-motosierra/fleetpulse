"""Acceso a fp-registry y a Step Functions para la API `/supply/*` (T40). Las PO/TRF las escriben supply-sourcing y
supply-flow (T39); esta Lambda las lee, escribe solo el META de una PO/TRF manual y las decisiones (aprobar,
rechazar, recibir) antes de despertar al flujo con el `task_token` guardado.

Claves:
- `STOCK#<part>#<zone>` / `META` (registry, T38) y `MOVE#<part>#<zone>` / `<ts>#<ref>` (kind consumption, ...).
- `PO#<id>` / `META` y `TRF#<id>` / `META`: GSI3 `TENANT#<destino>` / `PO#<created_at>` o `TRF#<created_at>`; las
  transferencias tambien GSI1 `TRFFROM#<from_tenant>` / `<created_at>` (salientes del donante).
- `PO#<id>` / `EV#<ts>#<event_id>`: linea de tiempo (sobre completo en `payload`), como `INCIDENT#.../EV#`.
"""
from __future__ import annotations

import json
import os
from datetime import timedelta
from decimal import Decimal

from boto3.dynamodb.conditions import Attr, Key
from botocore.config import Config
from botocore.exceptions import ClientError

from fplib import clock, ids

MAX_DOCS = 200  # tope por consulta: la demo maneja decenas de ordenes
AWS_CONFIG = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 3})
_cache: dict = {}


class ConditionFailed(Exception):
    """La condicion de la escritura no se cumplio (estado ya cambiado o id repetido); no se escribio nada."""


def registry_table():
    if "registry" not in _cache:
        import boto3
        _cache["registry"] = boto3.resource("dynamodb").Table(os.environ.get("REGISTRY_TABLE", "fp-registry"))
    return _cache["registry"]


def sfn_client():
    if "sfn" not in _cache:
        import boto3
        _cache["sfn"] = boto3.client("stepfunctions", config=AWS_CONFIG)
    return _cache["sfn"]


def _query(table, **kw) -> list[dict]:
    out, start = [], None
    while True:
        if start:
            kw["ExclusiveStartKey"] = start
        r = table.query(**kw)
        out += r.get("Items", [])
        start = r.get("LastEvaluatedKey")
        if not start or len(out) >= MAX_DOCS:
            return out


# ---------- stock ----------

def stock_items(table, tenants, zone: str | None = None, part: str | None = None) -> list[dict]:
    """`STOCK#<part>#<zone>` / `META` de las lineas de stock del catalogo en los parques pedidos."""
    catalog = ids.load()
    keys = [{"PK": f"STOCK#{s['part_id']}#{z}", "SK": "META"} for t in tenants
            for s in catalog["part_stock"] if (s.get("tenant_id") or ids.default_tenant()) == t
            for z in ids.zones(t) if (not zone or z == zone) and (not part or s["part_id"] == part)]
    client, name, found = table.meta.client, table.name, []
    for i in range(0, len(keys), 100):
        pending = {name: {"Keys": keys[i:i + 100]}}
        while pending:
            r = client.batch_get_item(RequestItems=pending)
            found += r.get("Responses", {}).get(name, [])
            pending = r.get("UnprocessedKeys") or None
    return found


def consumed_30d(table, part_id: str, zone: str) -> float:
    since = clock.iso(clock.now() - timedelta(days=30))
    rows = _query(table, KeyConditionExpression=Key("PK").eq(f"MOVE#{part_id}#{zone}") & Key("SK").gte(since))
    return float(sum(-r["qty"] for r in rows if r.get("kind") == "consumption" and r.get("qty", 0) < 0))


def movements(table, part_id: str, zone: str, limit: int = 50) -> list[dict]:
    return table.query(KeyConditionExpression=Key("PK").eq(f"MOVE#{part_id}#{zone}"), ScanIndexForward=False,
                       Limit=limit).get("Items", [])


def get_stock(table, part_id: str, zone: str) -> dict | None:
    return table.get_item(Key={"PK": f"STOCK#{part_id}#{zone}", "SK": "META"}).get("Item")


# ---------- ordenes y transferencias ----------

def pk_of(doc_id: str) -> str:
    return ("TRF#" if doc_id.startswith("TRF-") else "PO#") + doc_id


def get_doc(table, doc_id: str) -> tuple[dict | None, list[dict]]:
    """(META, EV#) de una PO/TRF, o (None, []) si no existe."""
    items = _query(table, KeyConditionExpression=Key("PK").eq(pk_of(doc_id)))
    meta = next((i for i in items if i["SK"] == "META"), None)
    return meta, [i for i in items if i["SK"].startswith("EV#")]


def events_of(table, doc_id: str) -> list[dict]:
    return _query(table, KeyConditionExpression=Key("PK").eq(pk_of(doc_id)) & Key("SK").begins_with("EV#"))


def list_metas(table, tenants, prefix: str) -> list[dict]:
    """META de las PO (`prefix` PO) o TRF (TRF) cuyo destino u origen esta en `tenants`, sin repetir."""
    rows: dict[str, dict] = {}
    for t in tenants:
        for i in _query(table, IndexName="GSI3",
                        KeyConditionExpression=Key("GSI3PK").eq(f"TENANT#{t}") & Key("GSI3SK").begins_with(f"{prefix}#")):
            rows[i["PK"]] = i
        if prefix == "TRF":
            for i in _query(table, IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq(f"TRFFROM#{t}")):
                if i["PK"].startswith("TRF#"):
                    rows.setdefault(i["PK"], i)
    return [r for r in rows.values() if r.get("SK") == "META"]


def put_new(table, item: dict) -> None:
    try:
        item = json.loads(json.dumps(item), parse_float=Decimal)  # DynamoDB no acepta float
        table.put_item(Item=item, ConditionExpression=Attr("PK").not_exists())
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ConditionFailed() from None
        raise


def delete(table, doc_id: str) -> None:
    table.delete_item(Key={"PK": pk_of(doc_id), "SK": "META"})


def take_wait(table, doc_id: str, waiting_for: str, statuses: tuple[str, ...], task_token: str, **fields) -> None:
    """Toma la espera `waiting_for` (approval | receipt) de una vez: exige ese `waiting_for`, un estado de `statuses`
    y el mismo token, quita `waiting_for` y fija `fields`. Un doble clic o una decision en paralelo -> ConditionFailed.
    """
    names = {"#w": "waiting_for", "#s": "status", "#t": "task_token", "#u": "updated_at"}
    values = {":w": waiting_for, ":t": task_token, ":u": clock.iso(clock.now())}
    sets = ["#u = :u"]
    for i, (k, v) in enumerate(fields.items()):
        names[f"#f{i}"], values[f":f{i}"] = k, v
        sets.append(f"#f{i} = :f{i}")
    status_keys = []
    for i, s in enumerate(statuses):
        values[f":s{i}"] = s
        status_keys.append(f":s{i}")
    try:
        table.update_item(
            Key={"PK": pk_of(doc_id), "SK": "META"},
            UpdateExpression="SET " + ", ".join(sets) + " REMOVE #w",
            ConditionExpression=f"#w = :w AND #t = :t AND #s IN ({', '.join(status_keys)})",
            ExpressionAttributeNames=names, ExpressionAttributeValues=values)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ConditionFailed() from None
        raise


def restore_wait(table, doc_id: str, waiting_for: str) -> None:
    """Devuelve la espera si Step Functions no recibio la decision por un error transitorio (la consola reintenta)."""
    try:
        table.update_item(Key={"PK": pk_of(doc_id), "SK": "META"}, UpdateExpression="SET #w = :w",
                          ConditionExpression="attribute_not_exists(#w)",
                          ExpressionAttributeNames={"#w": "waiting_for"}, ExpressionAttributeValues={":w": waiting_for})
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
