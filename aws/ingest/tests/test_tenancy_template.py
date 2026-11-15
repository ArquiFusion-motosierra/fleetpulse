"""Guarda de plantillas: el multi-tenancy se ve en la infraestructura (grupos de Cognito por parque, GSI3 por tenant en
fp-registry, parametros SSM por parque y el atributo tenant_id en cada aviso de fp-notify)."""
import json
from pathlib import Path

import yaml

AWS = Path(__file__).resolve().parents[2]
TENANTS = ("mineria", "agro", "industria")


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_multi_constructor("!", lambda loader, suffix, node: {suffix: loader.construct_scalar(node)
                                                                  if isinstance(node, yaml.ScalarNode)
                                                                  else loader.construct_sequence(node)})


def _resources(service: str) -> dict:
    return yaml.load((AWS / service / "serverless.yml").read_text(), Loader=_Loader)["resources"]["Resources"]


def test_cognito_has_one_group_per_park_and_the_cross_park_group():
    groups = {r["Properties"]["GroupName"]: r for r in _resources("auth").values()
              if r["Type"] == "AWS::Cognito::UserPoolGroup"}
    assert set(groups) == {f"tenant-{t}" for t in TENANTS} | {"maintenance-head"}
    assert groups["maintenance-head"]["Properties"]["Precedence"] == 0
    ssm = {r["Properties"]["Name"] for r in _resources("auth").values() if r["Type"] == "AWS::SSM::Parameter"}
    assert {f"/fleetpulse/tenants/{t}" for t in TENANTS} | {"/fleetpulse/tenants"} <= ssm


def test_user_pool_schema_is_untouched_so_the_pool_is_not_replaced():
    pool = _resources("auth")["UserPool"]["Properties"]
    assert [a["Name"] for a in pool["Schema"]] == ["role", "tech_id"]


def test_registry_table_has_the_tenant_index():
    table = _resources("ingest")["RegistryTable"]["Properties"]
    gsi3 = next(g for g in table["GlobalSecondaryIndexes"] if g["IndexName"] == "GSI3")
    assert [k["AttributeName"] for k in gsi3["KeySchema"]] == ["GSI3PK", "GSI3SK"]
    assert len(table["GlobalSecondaryIndexes"]) == 3  # CloudFormation agrega un GSI por despliegue


def test_every_role_notification_carries_the_park_attribute():
    asl = json.loads((AWS / "incidents" / "statemachine" / "incident-flow.asl.json").read_text())
    publishes = []

    def walk(states):
        for st in states.values():
            if st.get("Resource") == "arn:aws:states:::sns:publish" and "role" in (
                    (st.get("Parameters") or {}).get("MessageAttributes") or {}):
                publishes.append(st["Parameters"]["MessageAttributes"])
            for b in st.get("Branches", []):
                walk(b["States"])
    walk(asl["States"])
    assert publishes and all("tenant_id" in a for a in publishes)
