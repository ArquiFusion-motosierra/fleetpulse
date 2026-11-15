#!/usr/bin/env python3
"""Carga el catalogo de todos los parques (contracts/tenants.yaml: ids.yaml de mineria + tenants/*.yaml): stock en
fp-registry y maquinas/tecnicos en fp-state (modelo del plan de AWS), cada item con su `tenant_id`.

    python scripts/seed-registry.py                          # stock, proveedores, maquinas y tecnicos
    python scripts/seed-registry.py --no-state               # solo fp-registry
    python scripts/seed-registry.py --demo-supply            # + PRT-04 en 0 en mineria (demo de abastecimiento, T37)
    python scripts/seed-registry.py --dry-run --demo-supply  # muestra lo que escribiria, sin AWS

Owner: Alex (T10). Idempotente: UpdateItem por clave; no pisa severidad, stock reservado ni estado de ingest.
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "aws", "registry"))
sys.path.append(os.path.join(ROOT, "aws", "shared", "python"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-state", action="store_true", help="no escribir el catalogo en fp-state")
    ap.add_argument("--demo-supply", action="store_true", help="PRT-04 en 0 en las zonas de mineria (demo T43)")
    ap.add_argument("--dry-run", action="store_true", help="no escribe: muestra el resumen de lo que haria")
    a = ap.parse_args()

    from fplib.ids import merge_catalogs
    from reg.seed import DryRun, counts, seed, stock_of

    ids = merge_catalogs(os.path.join(ROOT, "contracts"))
    supply_path = os.path.join(ROOT, "contracts", "supply.yaml")
    supply = {}
    if os.path.isfile(supply_path):
        import yaml
        with open(supply_path, encoding="utf-8") as f:
            supply = yaml.safe_load(f) or {}
    if a.dry_run:
        client = DryRun()
        print(json.dumps(seed(client, ids, with_state=not a.no_state, supply=supply, demo_supply=a.demo_supply)))
        print(json.dumps({"PRT-04 on_hand por zona": stock_of(client.writes, "PRT-04"),
                          "escrituras": len(client.writes)}, ensure_ascii=False))
        return
    import boto3

    client = boto3.client("dynamodb", region_name="us-east-1")
    print(json.dumps(seed(client, ids, with_state=not a.no_state, supply=supply, demo_supply=a.demo_supply)))
    print(json.dumps(counts(client, ids)))


if __name__ == "__main__":
    main()
