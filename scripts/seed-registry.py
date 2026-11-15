#!/usr/bin/env python3
"""Carga contracts/ids.yaml en DynamoDB fp-registry y fp-state (maquinas, tecnicos, repuestos, BOM, stock).

    python scripts/seed-registry.py            # fp-registry + catalogo estatico en fp-state
    python scripts/seed-registry.py --no-state # solo fp-registry

Owner: Alex (T10). Idempotente: UpdateItem por clave; no pisa severidad, stock reservado ni estado de ingest.
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "aws", "registry"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-state", action="store_true", help="no escribir el catalogo en fp-state")
    a = ap.parse_args()
    import boto3
    import yaml

    from reg.seed import counts, seed

    with open(os.path.join(ROOT, "contracts", "ids.yaml"), encoding="utf-8") as f:
        ids = yaml.safe_load(f)
    client = boto3.client("dynamodb", region_name="us-east-1")
    print(json.dumps(seed(client, ids, with_state=not a.no_state)))
    print(json.dumps(counts(client)))


if __name__ == "__main__":
    main()
