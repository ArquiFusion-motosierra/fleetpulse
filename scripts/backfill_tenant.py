#!/usr/bin/env python3
"""Estampa el parque (tenant) en los items de fp-registry y fp-state anteriores al multi-tenancy. Idempotente.

    python scripts/backfill_tenant.py --dry-run   # cuenta lo que cambiaria, sin escribir
    python scripts/backfill_tenant.py

Incidentes (`INCIDENT#<id>`/`META`) y reportes (`REPORT#<id>`/`META`) sin `GSI3PK` reciben `tenant_id` (el de su
maquina segun el catalogo, ver fplib.ids.tenant_of_item) y el GSI3 del parque (`TENANT#<tenant>` / `INC#<opened_at>` o
`RPT#<reported_at>`); stock, pedidos, ordenes, maquinas y tecnicos sin `tenant_id` reciben solo el atributo. Cada
escritura exige `attribute_not_exists(tenant_id)`: nunca pisa un parque ya escrito. Lo corre una persona con
credenciales del Learner Lab despues del deploy.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(ROOT, "aws", "shared", "python"))

# prefijo de PK -> (SK que se estampa, prefijo de GSI3SK y atributo de fecha, o None si no lleva GSI3)
KINDS = {
    "INCIDENT#": ("META", ("INC#", "opened_at")),
    "REPORT#": ("META", ("RPT#", "reported_at")),
    "STOCK#": ("META", None),
    "REORDER#": (None, None),
    "ORDER#": (None, None),
    "MACHINE#": ("META", None),
    "TECH#": ("META", None),
}


def update_for(item: dict) -> dict | None:
    """UpdateItem (sin TableName) que estampa el tenant en `item`, o None si no corresponde o ya lo tiene."""
    from fplib import ids

    pk, sk = item.get("PK", ""), item.get("SK", "")
    kind = next((k for k in KINDS if pk.startswith(k)), None)
    if kind is None or item.get("tenant_id"):
        return None
    want_sk, gsi = KINDS[kind]
    if want_sk is not None and sk != want_sk:
        return None
    if kind == "STOCK#":
        tenant = ids.tenant_of_zone(pk.split("#")[2] if pk.count("#") >= 2 else None)
    elif kind == "TECH#":
        tenant = ids.tenant_of_technician(pk.split("#", 1)[1])
    elif kind == "MACHINE#":
        tenant = ids.tenant_of_machine(pk.split("#", 1)[1])
    else:
        tenant = ids.tenant_of_item(item)
    sets, values = ["tenant_id = :t"], {":t": tenant}
    if gsi and item.get(gsi[1]):
        sets += ["GSI3PK = :g3pk", "GSI3SK = :g3sk"]
        values.update({":g3pk": f"TENANT#{tenant}", ":g3sk": f"{gsi[0]}{item[gsi[1]]}"})
    return {"Key": {"PK": pk, "SK": sk}, "UpdateExpression": "SET " + ", ".join(sets),
            "ConditionExpression": "attribute_not_exists(tenant_id)", "ExpressionAttributeValues": values}


def run(table, dry_run: bool) -> dict:
    from botocore.exceptions import ClientError

    counts: dict[str, int] = {}
    kwargs: dict = {"ProjectionExpression": "PK, SK, tenant_id, machine_id, zone_id, opened_at, reported_at"}
    while True:
        page = table.scan(**kwargs)
        for item in page.get("Items", []):
            upd = update_for(item)
            if upd is None:
                continue
            tenant = upd["ExpressionAttributeValues"][":t"]
            counts[tenant] = counts.get(tenant, 0) + 1
            if not dry_run:
                try:
                    table.update_item(**upd)
                except ClientError as e:
                    if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                        raise
        if "LastEvaluatedKey" not in page:
            return counts
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="solo contar")
    ap.add_argument("--tables", default="fp-registry,fp-state")
    a = ap.parse_args()
    import boto3

    ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    for name in a.tables.split(","):
        # fp-state tiene millones de lecturas R#: el scan las recorre pero solo estampa META/TECH (proyeccion minima)
        print(json.dumps({"table": name, "dry_run": a.dry_run, "by_tenant": run(ddb.Table(name), a.dry_run)}))


if __name__ == "__main__":
    main()
