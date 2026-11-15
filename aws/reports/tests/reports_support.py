"""Constantes, ayudas y dobles de las pruebas de aws/reports (EX-017 en NORTE, 2026-11-15 14:40:12 UTC).

Nombre unico a proposito: CI corre las pruebas de todos los servicios en una sola sesion de pytest.
"""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from fplib import schema

NOW = datetime(2026, 11, 15, 14, 40, 12, tzinfo=timezone.utc)
RPT = "RPT-EX-017-20261115T144012"
INC = "INC-EX-017-20261115T1430"
LAT, LON = -13.9003, -71.5611
AUDIENCE = "https://insights-abc123-ue.a.run.app"
SA_KEY = {"type": "service_account", "client_email": "fp-invoker@fleetpulse-g19-2026.iam.gserviceaccount.com",
          "private_key": "-----BEGIN PRIVATE KEY-----\nNO-ES-UNA-LLAVE\n-----END PRIVATE KEY-----\n"}
OPERATOR = {"custom:role": "operator", "email": "operador@example.com", "sub": "sub-op"}
OPERATOR2 = {"custom:role": "operator", "email": "otro.operador@example.com", "sub": "sub-op2"}
TECH1 = {"custom:role": "technician", "custom:tech_id": "TEC-01", "email": "tecnico@example.com", "sub": "sub-t1"}
TECH3 = {"custom:role": "technician", "custom:tech_id": "TEC-03", "email": "tecnico3@example.com", "sub": "sub-t3"}
SUPERVISOR = {"custom:role": "supervisor", "email": "supervisor@example.com", "sub": "sub-sup"}
ADMIN = {"custom:role": "admin", "email": "admin@example.com", "sub": "sub-adm"}
MANAGEMENT = {"custom:role": "management", "email": "direccion@example.com", "sub": "sub-mgmt"}
CREATE_BODY = {"machine_id": "EX-017", "abnormality_type": "high_engine_temp",
               "description": "Sale vapor y el indicador de temperatura esta en rojo", "severity_perceived": "high"}
CLOSE_BODY = {"findings": "Junta de culata con fuga; refrigerante con aceite.",
              "parts_used": [{"part_id": "PRT-11", "zone_id": "NORTE", "qty": 1}],
              "confirmed_cause": "Junta de culata danada"}
# Salida de ejemplo de T33 (backlog #30)
T33_EXAMPLE = {
    "probable_causes": [{"cause": "Junta de culata con fuga", "component": "head_gasket", "confidence": 0.6},
                        {"cause": "Termostato pegado", "component": "thermostat", "confidence": 0.25}],
    "checks": [{"order": 1, "step": "Verificar nivel y color del refrigerante", "tool": "visual",
                "expected": "nivel en marca, sin aceite", "if_fails": "sospechar junta de culata"},
               {"order": 2, "step": "Prueba de presion del sistema de refrigeracion", "tool": "bomba manual 1,2 bar",
                "expected": "mantiene presion 10 min", "if_fails": "fuga interna"},
               {"order": 3, "step": "Gases de combustion en el radiador", "tool": "kit block tester",
                "expected": "negativo", "if_fails": "cambiar junta (PRT-xx)"}],
    "urgency": "immediate", "suggested_parts": ["PRT-01"], "safety_notes": ["No abrir el radiador en caliente"],
    "source": "gemini", "cached": False,
}
BASELINE = {"coolant_temp_c": {"mu": 88.0, "sigma": 3.0}, "hydraulic_oil_temp_c": {"mu": 62.0, "sigma": 4.0},
            "hydraulic_pressure_bar": {"mu": 300.0, "sigma": 12.0}, "vibration_mm_s": {"mu": 6.5, "sigma": 1.0},
            "engine_rpm": {"mu": 1800.0, "sigma": 60.0}, "fuel_rate_lph": {"mu": 38.0, "sigma": 3.0}}


def make_table(res, name, gsis):
    attrs = [("PK", "S"), ("SK", "S")] + [(f"{g}{k}", "S") for g in gsis for k in ("PK", "SK")]
    return res.create_table(
        TableName=name, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": t} for a, t in attrs],
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        GlobalSecondaryIndexes=[{"IndexName": g, "Projection": {"ProjectionType": "ALL"},
                                 "KeySchema": [{"AttributeName": f"{g}PK", "KeyType": "HASH"},
                                               {"AttributeName": f"{g}SK", "KeyType": "RANGE"}]} for g in gsis])


def dec(o):
    return json.loads(json.dumps(o), parse_float=Decimal)


def seed_machine(state, machine_id="EX-017", baseline=BASELINE, **attrs):
    state.put_item(Item=dec({"PK": f"MACHINE#{machine_id}", "SK": "META", "machine_id": machine_id, "model": "EX",
                             "serial": "AFCEX01721", "zone_id": "NORTE", "lat": LAT, "lon": LON, "geohash6": "6mc5pd",
                             "hours_total": 2765.0, "status": "schedule", "baseline": baseline, **attrs}))


def reading(ts: str, engine_status="operating", fault_codes=None, **vals):
    base = {"coolant_temp_c": 104.0, "hydraulic_oil_temp_c": 68.0, "hydraulic_pressure_bar": 298.0,
            "vibration_mm_s": 6.9, "engine_rpm": 1810, "fuel_rate_lph": 41.3}
    return {"ts": ts, "engine_status": engine_status, "fault_codes": fault_codes or [], **base, **vals}


def seed_readings(state, rows, machine_id="EX-017"):
    for r in rows:
        state.put_item(Item=dec({"PK": f"MACHINE#{machine_id}", "SK": f"R#{r['ts']}", "machine_id": machine_id,
                                 **r}))


def hot_readings(n=10, start=NOW - timedelta(minutes=5)):
    return [reading((start + timedelta(seconds=30 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    fault_codes=[{"spn": 110, "fmi": 0, "severity": "high"}] if i == n - 1 else [])
            for i in range(n)]


def seed_tech(state, tech_id="TEC-01", zone="NORTE", specialty="hydraulics", available=True, **attrs):
    """`TECH#<id>` / `META` con los mismos atributos que siembra aws/registry/reg/seed.py (lat/lon del centroide)."""
    lat, lon = {"NORTE": (-13.9, -71.56), "CENTRO": (-13.92, -71.545), "SUR": (-13.94, -71.555)}[zone]
    state.put_item(Item=dec({"PK": f"TECH#{tech_id}", "SK": "META", "tech_id": tech_id, "name": f"Tecnico {tech_id}",
                             "specialty": specialty, "zone_id": zone, "lat": lat, "lon": lon, "available": available,
                             "GSI1PK": f"ZONE#{zone}", "GSI1SK": f"TECH#{tech_id}", **attrs}))


def seed_incident(reg, incident_id=INC, status="open", **attrs):
    opened_at = attrs.pop("opened_at", "2026-11-15T14:31:00Z")
    reg.put_item(Item={
        "PK": f"INCIDENT#{incident_id}", "SK": "META", "incident_id": incident_id, "machine_id": "EX-017",
        "status": status, "opened_at": opened_at, "severity": "schedule", "failure_mode": "hydraulic_pump",
        "GSI1PK": "MACHINE#EX-017", "GSI1SK": f"INC#{opened_at}", "GSI2PK": f"STATUS#{status}", "GSI2SK": opened_at,
        **attrs})


def api_event(route, claims=None, body=None, path=None, qs=None):
    ev = {"routeKey": route, "pathParameters": path or {}, "queryStringParameters": qs,
          "headers": {"authorization": "Bearer no-se-registra"},
          "requestContext": {"authorizer": {"jwt": {"claims": claims}}} if claims else {"requestContext": {}}}
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


def body_of(resp):
    return json.loads(resp["body"])


def outbox_events(outbox, event_type):
    """Sobres en fp-outbox de un tipo, validados contra el sobre y contra report.schema.json."""
    envs = [json.loads(i["payload"]) for i in outbox.scan()["Items"] if i["type"] == event_type]
    for env in envs:
        schema.validate("envelope", env)
        schema.validate("report", env)
    return envs


def ev_items(reg, pk):
    return reg.query(KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with("EV#"))["Items"]


def log_lines(caplog, msg):
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == "fplib"]
    return [line for line in lines if line["msg"] == msg]


class FakeSfn:
    """Step Functions con las excepciones reales de botocore; `fail`: codigo que lanza `start_execution`."""

    def __init__(self, fail=None):
        self.exceptions = boto3.client("stepfunctions", region_name="us-east-1").exceptions
        self.calls, self.fail = [], fail

    def start_execution(self, **kwargs):
        self.calls.append({**kwargs, "input": json.loads(kwargs["input"])})
        if self.fail:
            cls = getattr(self.exceptions, self.fail, ClientError)
            raise cls({"Error": {"Code": self.fail, "Message": "x"}}, "StartExecution")
        return {"executionArn": f"arn:aws:states:us-east-1:000000000000:execution:incident-flow:{kwargs['name']}"}


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
