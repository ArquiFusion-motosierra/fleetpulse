#!/usr/bin/env python3
"""Carga el catalogo de todos los parques (contracts/tenants.yaml: ids.yaml de mineria + tenants/*.yaml): stock en
fp-registry y maquinas/tecnicos en fp-state (modelo del plan de AWS), cada item con su `tenant_id`.

    python scripts/seed-registry.py            # stock en fp-registry + maquinas y tecnicos en fp-state
    python scripts/seed-registry.py --no-state # solo stock

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
    a = ap.parse_args()
    import boto3

    from fplib.ids import merge_catalogs
    from reg.seed import counts, seed

    ids = merge_catalogs(os.path.join(ROOT, "contracts"))
    client = boto3.client("dynamodb", region_name="us-east-1")
    print(json.dumps(seed(client, ids, with_state=not a.no_state)))
    print(json.dumps(counts(client, ids)))


if __name__ == "__main__":
    main()
