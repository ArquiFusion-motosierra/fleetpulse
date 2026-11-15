"""Seed de abastecimiento (T37): stock ampliado, proveedores y perfil de la demo (PRT-04 en 0 en mineria)."""
import os

import boto3
import pytest
import yaml
from moto import mock_aws

from fplib.ids import merge_catalogs
from reg import seed, store

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
IDS = merge_catalogs(os.path.join(ROOT, "contracts"))
with open(os.path.join(ROOT, "contracts", "supply.yaml"), encoding="utf-8") as f:
    SUPPLY = yaml.safe_load(f)
PK_SK = [{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}]


@pytest.fixture
def ddb():
    with mock_aws():
        c = boto3.client("dynamodb", region_name="us-east-1")
        attrs = [{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK")]
        for name in ("fp-registry", "fp-state"):
            c.create_table(TableName=name, BillingMode="PAY_PER_REQUEST", KeySchema=PK_SK, AttributeDefinitions=attrs)
        yield c


def stock(c, part, zone):
    return store.get(c, f"STOCK#{part}#{zone}", "META")


def test_stock_carries_supply_fields_and_suppliers_are_seeded(ddb):
    out = seed.seed(ddb, IDS, with_state=False, supply=SUPPLY)
    assert out["suppliers"] == 3 and out["supplier_parts"] == len(SUPPLY["supplier_parts"])
    s = stock(ddb, "PRT-04", "NORTE")
    assert (s["criticality"], s["safety_stock"], s["on_order"], s["in_transit"], s["tenant_id"]) == ("A", 1, 0, 0, "mineria")
    assert s["on_hand"] == 1  # sin perfil de demo, el catalogo
    assert store.get(ddb, "SUPPLIER#SUP-HIDRA", "META")["name"] == "HidraAndes SAC"
    offer = store.get(ddb, "SUPPLIER#SUP-HIDRA", "PART#PRT-04")
    assert (offer["unit_price_usd"], offer["lead_time_d"], offer["min_qty"]) == (6800, 7, 1)


def test_demo_profile_empties_prt04_in_mining_only(ddb):
    seed.seed(ddb, IDS, with_state=False, supply=SUPPLY, demo_supply=True)
    assert [stock(ddb, "PRT-04", z)["on_hand"] for z in ("NORTE", "CENTRO", "SUR")] == [0, 0, 0]
    assert stock(ddb, "PRT-04", "FUNDO-A")["on_hand"] == 3 and stock(ddb, "PRT-04", "FUNDO-A")["tenant_id"] == "agro"
    assert stock(ddb, "PRT-03", "NORTE")["on_hand"] == 6  # el resto de mineria no cambia


def test_reseed_keeps_dynamic_supply_fields(ddb):
    seed.seed(ddb, IDS, with_state=False, supply=SUPPLY)
    ddb.update_item(TableName="fp-registry", Key=store.key("STOCK#PRT-01#NORTE", "META"),
                    UpdateExpression="SET on_order = :o, in_transit = :t", ExpressionAttributeValues=store.vals(o=5, t=2))
    seed.seed(ddb, IDS, with_state=False, supply=SUPPLY)
    s = stock(ddb, "PRT-01", "NORTE")
    assert (s["on_order"], s["in_transit"]) == (5, 2)


def test_parts_without_policy_default_to_c_and_zero():
    client = seed.DryRun()
    seed.seed(client, IDS, with_state=False, supply={"part_policy": []})
    rows = [w for w in client.writes if w["PK"].startswith("STOCK#")]
    assert rows and all(w["criticality"] == "C" and w["safety_stock"] == 0 for w in rows)


def test_dry_run_reports_the_demo_story():
    client = seed.DryRun()
    seed.seed(client, IDS, with_state=False, supply=SUPPLY, demo_supply=True)
    prt04 = seed.stock_of(client.writes, "PRT-04")
    assert {z: prt04[z] for z in ("NORTE", "CENTRO", "SUR", "FUNDO-A")} == {"NORTE": 0, "CENTRO": 0, "SUR": 0, "FUNDO-A": 3}
