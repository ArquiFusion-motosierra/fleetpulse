"""Constantes, ayudas y dobles de las pruebas de incident-flow (EX-017 en NORTE, 2026-11-15 14:38:05 UTC).

Nombre unico a proposito: CI corre las pruebas de todos los servicios en una sola sesion de pytest.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

from flow import assign_technician, classify, create_incident, rank_technicians
from fplib import schema

INC = "INC-EX-017-20261115T1430"
NOW = datetime(2026, 11, 15, 14, 38, 5, tzinfo=timezone.utc)
NOW_ISO = "2026-11-15T14:38:05Z"
LAT, LON = -13.9003, -71.5611
KM_LAT = 1 / 111.195  # grados de latitud por km
DEVIATED = [
    {"var": "hydraulic_pressure_bar", "value": 287.4, "z": -2.6},
    {"var": "hydraulic_oil_temp_c", "value": 71.9, "z": 2.1},
    {"var": "vibration_mm_s", "value": 9.8, "z": 1.9},
]


def make_table(res, name, gsis):
    attrs = [("PK", "S"), ("SK", "S")] + [(f"{g}{k}", "S") for g in gsis for k in ("PK", "SK")]
    return res.create_table(
        TableName=name, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": t} for a, t in attrs],
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        GlobalSecondaryIndexes=[{"IndexName": g, "Projection": {"ProjectionType": "ALL"},
                                 "KeySchema": [{"AttributeName": f"{g}PK", "KeyType": "HASH"},
                                               {"AttributeName": f"{g}SK", "KeyType": "RANGE"}]} for g in gsis])


def outbox_events(outbox, event_type, contract="incident"):
    """Sobres en fp-outbox de un tipo, cada uno validado contra su contrato."""
    envs = [json.loads(i["payload"]) for i in outbox.scan()["Items"] if i["type"] == event_type]
    for env in envs:
        schema.validate(contract, env)
    return envs


def ev_items(reg, incident_id):
    return reg.query(KeyConditionExpression=Key("PK").eq(f"INCIDENT#{incident_id}") & Key("SK").begins_with("EV#"))[
        "Items"]


def get_meta(reg, incident_id):
    return reg.get_item(Key={"PK": f"INCIDENT#{incident_id}", "SK": "META"}).get("Item")


def seed_incident(reg, incident_id, opened_at, status="open", **attrs):
    reg.put_item(Item={
        "PK": f"INCIDENT#{incident_id}", "SK": "META", "incident_id": incident_id, "machine_id": "EX-017",
        "status": status, "opened_at": opened_at, "GSI1PK": "MACHINE#EX-017", "GSI1SK": f"INC#{opened_at}",
        "GSI2PK": f"STATUS#{status}", "GSI2SK": opened_at, **attrs})


def seed_tech(state, tech_id, specialty, lat, lon, available=True, zone="NORTE", **attrs):
    state.put_item(Item={
        "PK": f"TECH#{tech_id}", "SK": "META", "tech_id": tech_id, "name": f"Tecnico {tech_id}",
        "specialty": specialty, "zone_id": zone, "lat": Decimal(str(lat)), "lon": Decimal(str(lon)),
        "available": available, "GSI1PK": f"ZONE#{zone}", "GSI1SK": f"TECH#{tech_id}", **attrs})


def get_tech(state, tech_id):
    return state.get_item(Key={"PK": f"TECH#{tech_id}", "SK": "META"})["Item"]


def assign(ev, ddb):
    return assign_technician.handler(ev, state=ddb.state, registry=ddb.registry, outbox=ddb.outbox)


def classified(ev):
    return {**ev, "classified": classify.handler(ev)}


def created(ev, ddb):
    ev = classified(ev)
    return {**ev, "incident": create_incident.handler(ev, registry=ddb.registry, outbox=ddb.outbox)}


def ranked(ev, ddb):
    ev = created(ev, ddb)
    return {**ev, "ranking": rank_technicians.handler(ev, state=ddb.state)}


def assigned(ev, ddb):
    ev = ranked(ev, ddb)
    return {**ev, "assignment": assign(ev, ddb)}


def log_lines(caplog, msg):
    """Lineas JSON de fplib.log con ese `msg`."""
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == "fplib"]
    return [line for line in lines if line["msg"] == msg]


class FakeSfn:
    """Step Functions con las excepciones reales de botocore; `fail` = nombre de la excepcion a lanzar."""

    def __init__(self, fail=None):
        self.exceptions = boto3.client("stepfunctions", region_name="us-east-1").exceptions
        self.calls, self.fail = [], fail

    def send_task_success(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise getattr(self.exceptions, self.fail)({"Error": {"Code": self.fail, "Message": "x"}},
                                                      "SendTaskSuccess")
        return {}


class FakeSns:
    def __init__(self):
        self.calls = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": f"msg-{len(self.calls)}"}


class FakeSsm:
    def __init__(self, values):
        self.values, self.calls = values, []

    def get_parameter(self, Name):  # noqa: N803 (firma de boto3)
        self.calls.append(Name)
        return {"Parameter": {"Name": Name, "Value": self.values[Name]}}


class FakeSecrets:
    def __init__(self, values):
        self.values = values

    def get_secret_value(self, SecretId):  # noqa: N803 (firma de boto3)
        return {"SecretString": json.dumps(self.values[SecretId])}


class FakeHttp:
    """`http(url, body, headers, timeout) -> (status, bytes)`; `result` puede ser una excepcion a lanzar."""

    def __init__(self, result):
        self.result, self.calls = result, []

    def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": json.loads(body), "headers": headers, "timeout": timeout})
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result
