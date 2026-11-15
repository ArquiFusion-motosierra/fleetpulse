"""API de lectura y reservas multi-tenant: cada parque ve y consume solo lo suyo (moto, catalogo unido de los 3)."""
import json

import boto3
import pytest
from moto import mock_aws

from fplib import ids
from reg import api, seed, store
from reg import apply as reg_apply

PK_SK = [{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}]


def _gsi(name):
    return {"IndexName": name, "Projection": {"ProjectionType": "ALL"},
            "KeySchema": [{"AttributeName": f"{name}PK", "KeyType": "HASH"},
                          {"AttributeName": f"{name}SK", "KeyType": "RANGE"}]}


def _attrs(*names):
    return [{"AttributeName": n, "AttributeType": "S"} for n in names]


class Key:
    def get_secret_value(self, SecretId):
        return {"SecretString": json.dumps({"api_key": "k"})}


@pytest.fixture
def mt():
    with mock_aws():
        c = boto3.client("dynamodb", region_name="us-east-1")
        c.create_table(TableName="fp-registry", BillingMode="PAY_PER_REQUEST", KeySchema=PK_SK,
                       AttributeDefinitions=_attrs("PK", "SK", *(f"GSI{i}{k}" for i in (1, 2, 3) for k in ("PK", "SK"))),
                       GlobalSecondaryIndexes=[_gsi("GSI1"), _gsi("GSI2"), _gsi("GSI3")])
        c.create_table(TableName="fp-state", BillingMode="PAY_PER_REQUEST", KeySchema=PK_SK,
                       AttributeDefinitions=_attrs("PK", "SK", "GSI1PK", "GSI1SK"), GlobalSecondaryIndexes=[_gsi("GSI1")])
        for name, k in (("fp-inbox", "message_id"), ("fp-outbox", "event_id")):
            c.create_table(TableName=name, BillingMode="PAY_PER_REQUEST", AttributeDefinitions=_attrs(k),
                           KeySchema=[{"AttributeName": k, "KeyType": "HASH"}])
        seed.seed(c, ids.load())
        yield c


def call(c, route, tenant=None, **params):
    ev = {"routeKey": route, "headers": {"x-api-key": "k"}, "pathParameters": params,
          "queryStringParameters": {"tenant": tenant} if tenant else None}
    r = api.handler(ev, client=c, secrets=Key())
    return r["statusCode"], json.loads(r["body"])


def incident(c, inc, machine, zone, opened_at, tenant=None):
    item = {"PK": f"INCIDENT#{inc}", "SK": "META", "status": "open", "machine_id": machine, "zone_id": zone,
            "opened_at": opened_at, "GSI1PK": f"MACHINE#{machine}", "GSI1SK": f"INC#{opened_at}",
            "GSI2PK": "STATUS#open", "GSI2SK": opened_at}
    if tenant:  # lo que escribe CreateIncident desde el multi-tenancy; sin tenant = incidente viejo
        item.update(tenant_id=tenant, GSI3PK=f"TENANT#{tenant}", GSI3SK=f"INC#{opened_at}")
    c.put_item(TableName="fp-registry", Item=store.ser(item))


AGRO_INC = "INC-CS-003-20261115T1430"
OLD_INC = "INC-EX-017-20261115T1400"


def test_seed_puts_each_park_stock_only_in_its_zones(mt):
    assert store.get(mt, "STOCK#PRT-17#PACKING", "META")["tenant_id"] == "agro"
    assert store.get(mt, "STOCK#PRT-17#NORTE", "META") is None
    assert store.get(mt, "STOCK#PRT-01#ALMACEN", "META")["tenant_id"] == "industria"
    assert store.get(mt, "MACHINE#GE-001", "META", table="fp-state")["tenant_id"] == "industria"


def test_default_is_mineria_for_a_console_without_tenant(mt):
    code, body = call(mt, "GET /machines")
    assert code == 200 and len(body["machines"]) == 100 and body["tenants"] == ["mineria"]
    assert {m["tenant_id"] for m in body["machines"]} == {"mineria"}


def test_each_park_sees_only_its_machines_stock_and_incidents(mt):
    incident(mt, AGRO_INC, "CS-003", "PACKING", "2026-11-15T14:31:00Z", tenant="agro")
    incident(mt, OLD_INC, "EX-017", "NORTE", "2026-11-15T14:01:00Z")  # anterior al multi-tenancy
    agro = call(mt, "GET /machines", tenant="agro")[1]["machines"]
    assert len(agro) == 12 and {m["zone_id"] for m in agro} == {"FUNDO-A", "FUNDO-B", "PACKING"}
    assert next(m for m in agro if m["machine_id"] == "CS-003")["open_incident_id"] == AGRO_INC
    assert [i["incident_id"] for i in call(mt, "GET /incidents", tenant="agro")[1]["incidents"]] == [AGRO_INC]
    assert [i["incident_id"] for i in call(mt, "GET /incidents")[1]["incidents"]] == [OLD_INC]  # legado = mineria
    stock = call(mt, "GET /stock", tenant="industria")[1]["stock"]
    assert stock and {s["zone_id"] for s in stock} == {"PLANTA-1", "PLANTA-2", "ALMACEN"}


def test_detail_of_another_park_is_not_found(mt):
    incident(mt, AGRO_INC, "CS-003", "PACKING", "2026-11-15T14:31:00Z", tenant="agro")
    assert call(mt, "GET /incidents/{id}", id=AGRO_INC)[0] == 404
    assert call(mt, "GET /incidents/{id}", tenant="agro", id=AGRO_INC)[0] == 200
    assert call(mt, "GET /machines/{id}", tenant="industria", id="EX-017")[0] == 404
    assert call(mt, "GET /machines/{id}", tenant="mineria", id="EX-017")[0] == 200


def test_maintenance_head_view_unions_parks(mt):
    incident(mt, AGRO_INC, "CS-003", "PACKING", "2026-11-15T14:31:00Z", tenant="agro")
    incident(mt, OLD_INC, "EX-017", "NORTE", "2026-11-15T14:01:00Z")
    code, body = call(mt, "GET /incidents", tenant="mineria,agro,industria")
    assert code == 200 and [i["incident_id"] for i in body["incidents"]] == [AGRO_INC, OLD_INC]
    assert len(call(mt, "GET /machines", tenant="mineria,agro,industria")[1]["machines"]) == 124


def test_unknown_tenant_is_400(mt):
    assert call(mt, "GET /machines", tenant="luna")[0] == 400


def test_reservation_never_takes_stock_from_another_park(mt):
    assert reg_apply.zones_by_distance("PACKING")[0] == "PACKING"
    assert set(reg_apply.zones_by_distance("PACKING")) == {"FUNDO-A", "FUNDO-B", "PACKING"}
    assert set(reg_apply.zones_by_distance("NORTE")) == {"NORTE", "CENTRO", "SUR"}


def test_mineria_part_stocked_in_an_agro_zone_belongs_to_agro(mt):
    # PRT-04 (bomba de mineria) tambien en FUNDO-A: el parque del STOCK es el de su zona (demo de transferencias)
    s = store.get(mt, "STOCK#PRT-04#FUNDO-A", "META")
    assert s["tenant_id"] == "agro" and s["on_hand"] > 0
    agro = call(mt, "GET /stock", tenant="agro")[1]["stock"]
    assert any(r["part_id"] == "PRT-04" and r["zone_id"] == "FUNDO-A" and r["tenant_id"] == "agro" for r in agro)
    mineria = call(mt, "GET /stock")[1]["stock"]
    assert {r["zone_id"] for r in mineria if r["part_id"] == "PRT-04"} == {"NORTE", "CENTRO", "SUR"}
