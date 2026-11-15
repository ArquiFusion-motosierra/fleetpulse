"""Constantes y ayudas de las pruebas de aws/supply: la historia de la demo (EX-017 en mineria NORTE espera la bomba
PRT-04, que esta en 0 en mina y en 3 por zona en agro) con tablas moto.

Nombre unico a proposito: CI corre las pruebas de todos los servicios en una sola sesion de pytest.
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
from referencing import Registry, Resource

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
NOW = datetime(2026, 11, 15, 14, 35, 0, tzinfo=timezone.utc)
INC = "INC-EX-017-20261115T1430"
TRF = "TRF-FUNDO-A-NORTE-PRT-04-20261115T1434"
PO = "PO-NORTE-PRT-04-20261115T1434"
SHORTAGE_AT = "2026-11-15T14:34:10Z"
LIFECYCLE = ["supply.suggested", "supply.approved", "po.ordered", "transfer.shipped", "supply.cancelled",
             "supply.received", "part.reserved"]
MINING, AGRO, INDUSTRY = ("NORTE", "CENTRO", "SUR"), ("FUNDO-A", "FUNDO-B", "PACKING"), ("PLANTA-1", "PLANTA-2", "ALMACEN")


def make_tables(client):
    def gsi(n):
        return {"IndexName": n, "Projection": {"ProjectionType": "ALL"},
                "KeySchema": [{"AttributeName": f"{n}PK", "KeyType": "HASH"}, {"AttributeName": f"{n}SK", "KeyType": "RANGE"}]}

    attrs = [{"AttributeName": a, "AttributeType": "S"}
             for a in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK", "GSI3PK", "GSI3SK")]
    client.create_table(TableName="fp-registry", BillingMode="PAY_PER_REQUEST", AttributeDefinitions=attrs,
                        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                        GlobalSecondaryIndexes=[gsi("GSI1"), gsi("GSI2"), gsi("GSI3")])
    client.create_table(TableName="fp-outbox", BillingMode="PAY_PER_REQUEST",
                        AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}],
                        KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}])


def seed_stock(client, part_id, zone, on_hand, reorder_point=1, tenant=None, **extra):
    from supply import store  # despues del sys.path de conftest
    from fplib import ids
    client.put_item(TableName="fp-registry", Item=store.ser({
        "PK": f"STOCK#{part_id}#{zone}", "SK": "META", "part_id": part_id, "zone_id": zone, "on_hand": on_hand,
        "reserved": 0, "available": on_hand, "reorder_point": reorder_point, "tenant_id": tenant or ids.tenant_of_zone(zone),
        **ids.part_policy(part_id), **extra}))


def seed_demo(client, agro=3, agro_reorder=1):
    """Perfil --demo-supply: PRT-04 en 0 en mina, `agro` por zona en agro; industria sin stock."""
    for z in MINING:
        seed_stock(client, "PRT-04", z, 0)
    for z in AGRO:
        seed_stock(client, "PRT-04", z, agro, agro_reorder)
    for z in INDUSTRY:
        seed_stock(client, "PRT-04", z, 0)


def shortage(missing=None, zone="NORTE", event_id=None, occurred_at=SHORTAGE_AT, machine_id="EX-017", incident_id=INC):
    """part.shortage tal como lo escribe aws/registry (T38)."""
    return {"event_id": event_id or str(uuid.uuid4()), "event_type": "part.shortage", "schema_version": 1,
            "source": "aws.registry", "aggregate_type": "part_reservation", "aggregate_id": f"RSV-{incident_id}",
            "occurred_at": occurred_at, "incident_id": incident_id, "machine_id": machine_id, "tenant_id": "mineria",
            "payload": {"missing": missing or [{"part_id": "PRT-04", "qty": 1}], "zone_id": zone,
                        "reorder_suggested": True}}


def sqs(*events, raw=True):
    def body(e):
        return json.dumps(e) if raw else json.dumps({"Type": "Notification", "Message": json.dumps(e)})
    return {"Records": [{"messageId": f"m-{i}", "body": body(e)} for i, e in enumerate(events)]}


def get(client, pk, sk="META"):
    from supply import store
    item = client.get_item(TableName="fp-registry", Key=store.key(pk, sk)).get("Item")
    return store.des(item) if item else None


def query(client, pk):
    from supply import store
    return store.query_all(client, pk)


def outbox_events(client, event_type=None):
    from supply import store
    rows = [json.loads(store.des(i)["payload"]) for i in client.scan(TableName="fp-outbox")["Items"]]
    # con el reloj fijo todos ocurren en el mismo segundo: desempata el orden del ciclo de vida
    return sorted([e for e in rows if event_type in (None, e["event_type"])],
                  key=lambda e: (e["occurred_at"], LIFECYCLE.index(e["event_type"])))


def _registry():
    res = [(json.loads(p.read_text())["$id"], Resource.from_contents(json.loads(p.read_text())))
           for p in CONTRACTS.glob("*.schema.json")]
    return Registry().with_resources(res)


def validate(event, contract):
    schema = json.loads((CONTRACTS / f"{contract}.schema.json").read_text())
    jsonschema.Draft202012Validator(schema, registry=_registry(),
                                    format_checker=jsonschema.FormatChecker()).validate(event)


class FakeSfn:
    """start_execution con la excepcion real de botocore ante un nombre repetido (lo que hace Step Functions)."""

    def __init__(self):
        import boto3
        self.started, self.exceptions = [], boto3.client("stepfunctions", region_name="us-east-1").exceptions

    def start_execution(self, stateMachineArn, name, input):  # noqa: A002,N803 (firma de boto3)
        if any(s["name"] == name for s in self.started):
            raise self.exceptions.ExecutionAlreadyExists(
                {"Error": {"Code": "ExecutionAlreadyExists", "Message": name}}, "StartExecution")
        self.started.append({"arn": stateMachineArn, "name": name, "input": json.loads(input)})
        return {}
