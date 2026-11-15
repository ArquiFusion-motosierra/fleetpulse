"""Lambda `supply-sourcing`: consume supply-q (SNS fp-domain-events filtrado a `part.shortage`) y, por cada linea
faltante, crea la sugerencia (PO o TRF, `supply.sourcing.suggest`) e inicia `supply-flow` con nombre = id.

En una transaccion por documento: META (condicion: el id no existe), BACKORDER# del incidente, EV# y
supply.suggested en fp-outbox. El id lleva el minuto del faltante (`occurred_at`), asi que un reintento del mismo
mensaje encuentra el documento y solo reintenta StartExecution (ExecutionAlreadyExists = ya iniciada). Si otro
incidente ya abrio la misma orden en ese minuto, su BACKORDER# se suma a ella. Anota `ref` en el REORDER# que dejo
registry. Fallo parcial por mensaje (ReportBatchItemFailures); tras 5 intentos va a supply-dlq.
"""
from __future__ import annotations

import json
import os

from botocore.exceptions import BotoCoreError, ClientError
from fplib import clock, log, schema

from supply import docs, sourcing, store

_clients: dict = {}


def _client(name: str):
    if name not in _clients:
        import boto3
        _clients[name] = boto3.client(name)
    return _clients[name]


def parse_body(raw: str) -> dict:
    """Con raw message delivery el cuerpo es el sobre; sin ella viene envuelto por SNS."""
    body = json.loads(raw)
    if isinstance(body, dict) and body.get("Type") == "Notification" and "Message" in body:
        body = json.loads(body["Message"])
    return body


def start(sfn, doc: dict) -> bool:
    """StartExecution con nombre = id. False si ya existia (reintento)."""
    try:
        sfn.start_execution(stateMachineArn=os.environ["SUPPLY_FLOW_ARN"], name=doc["id"],
                            input=json.dumps({"id": doc["id"], "kind": doc["kind"]}))
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ExecutionAlreadyExists":
            return False
        raise


def _mark_reorder(ddb, part_id: str, zone_id: str, incident_id: str, ref: str) -> None:
    for r in store.query_all(ddb, f"REORDER#{part_id}#{zone_id}"):
        if r.get("incident_id") == incident_id and not r.get("ref"):
            ddb.update_item(TableName=store.table("REGISTRY_TABLE"), Key=store.key(r["PK"], r["SK"]),
                            UpdateExpression="SET #r = :r", ExpressionAttributeNames={"#r": "ref"},
                            ExpressionAttributeValues=store.vals(r=ref))


def create(ddb, sfn, doc: dict) -> str:
    """Alta idempotente + inicio de la ejecucion. Devuelve created | existing | grouped."""
    now = doc["created_at"]
    items = docs.put_items(doc)
    if doc.get("incident_id"):
        items.insert(1, docs.backorder_item(doc, now))
    outcome = "created"
    try:
        store.transact(ddb, items)
    except store.ConditionFailed:
        existing = docs.load(ddb, doc["id"])
        outcome = "existing"
        if doc.get("incident_id") and existing.get("incident_id") != doc["incident_id"]:
            ddb.put_item(**docs.backorder_item({**existing, "incident_id": doc["incident_id"],
                                                "machine_id": doc.get("machine_id"),
                                                "needed_qty": doc.get("needed_qty")}, now)["Put"])
            outcome = "grouped"
        doc = existing
    if doc["status"] == "suggested":
        start(sfn, doc)
    return outcome


def handle_shortage(ddb, sfn, e: dict) -> list[dict]:
    schema.validate("part", e)
    p = e["payload"]
    zone = p.get("zone_id")
    if not zone:  # eventos anteriores a T38 no traen la zona: sin almacen destino no hay que abastecer
        log.event("supply_faltante_sin_zona", event_id=e["event_id"], incident_id=e.get("incident_id"))
        return []
    ts = clock.parse(e["occurred_at"])
    out = []
    for line in p["missing"]:
        found = sourcing.suggest(ddb, part_id=line["part_id"], qty=int(line["qty"]), zone_id=zone, ts=ts,
                                 incident_id=e.get("incident_id"), machine_id=e.get("machine_id"))
        if not found:
            log.event("supply_sin_opciones", part_id=line["part_id"], zone_id=zone, incident_id=e.get("incident_id"))
            continue
        for doc in found:
            outcome = create(ddb, sfn, doc)
            out.append({"id": doc["id"], "kind": doc["kind"], "outcome": outcome})
            log.event("supply_sugerencia", id=doc["id"], kind=doc["kind"], outcome=outcome, part_id=doc["part_id"],
                      zone_id=doc["zone_id"], cost_usd=doc["cost_usd"], incident_id=doc.get("incident_id"))
        if e.get("incident_id"):
            _mark_reorder(ddb, line["part_id"], zone, e["incident_id"], found[0]["id"])
    return out


def handler(event, context=None, ddb=None, sfn=None):
    ddb, sfn = ddb or _client("dynamodb"), sfn or _client("stepfunctions")
    failures = []
    for record in event.get("Records", []):
        try:
            e = parse_body(record["body"])
            if e.get("event_type") != "part.shortage":
                log.event("supply_evento_ignorado", event_type=e.get("event_type"), event_id=e.get("event_id"))
                continue
            handle_shortage(ddb, sfn, e)
        except (ClientError, BotoCoreError, ValueError, KeyError) as exc:
            log.error("supply_sourcing_fallo", message_id=record.get("messageId"), error=type(exc).__name__,
                      detail=str(exc)[:300])
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
