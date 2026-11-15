#!/usr/bin/env python3
"""Carga a BigQuery fleetpulse.domain_events los eventos de incidentes ya guardados en fp-registry (`INCIDENT#<id>` /
`EV#<occurred_at>#<event_id>`, payload = sobre completo como JSON), con el mismo codigo que la Lambda gcp-events
(aws/integrations/gcp-exporter/exp/events.py): un NDJSON en GCS fp-landing y un load job por tanda, nunca streaming.

    eval "$(scripts/lab-creds.sh)"                       # credenciales del Learner Lab, nunca el perfil por defecto
    python scripts/backfill_domain_events.py --dry-run   # cuenta por event_type y cuantos ya estan cargados
    python scripts/backfill_domain_events.py

Idempotente: usa el mismo fp-inbox (`gcp-events:<event_id>`) que la Lambda, asi que lo que ya llego por la cola no se
vuelve a cargar y correrlo dos veces no duplica. Los EV# viejos sin sobre completo, o con un sobre que no cumple
contracts/envelope.schema.json, se cuentan y se saltan. Solo cubre lo que tiene EV# (incident.*, part.*); el resto de
los eventos de dominio llega solo por gcp-events-q desde el deploy. Lee la llave de fp-exporter de Secrets Manager.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(ROOT, "aws", "shared", "python"))
sys.path.append(os.path.join(ROOT, "aws", "integrations", "gcp-exporter"))


def scan_ev_items(ddb, table: str) -> list[dict]:
    from boto3.dynamodb.types import TypeDeserializer

    des = TypeDeserializer()
    items, kw = [], {"TableName": table, "FilterExpression": "begins_with(SK, :ev)",
                     "ExpressionAttributeValues": {":ev": {"S": "EV#"}}}
    while True:
        resp = ddb.scan(**kw)
        items += [{k: des.deserialize(v) for k, v in it.items()} for it in resp.get("Items", [])]
        if "LastEvaluatedKey" not in resp:
            return items
        kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def envelopes_from_items(items: list[dict]) -> tuple[list[dict], Counter]:
    """Sobres validos de los EV#, en orden de occurred_at, y la cuenta de lo saltado por motivo."""
    from fplib import schema

    good, skipped = [], Counter()
    for it in items:
        raw = it.get("payload")
        try:
            env = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            skipped["payload no es JSON"] += 1
            continue
        if not isinstance(env, dict) or "event_id" not in env or "payload" not in env:
            skipped["EV# viejo sin sobre"] += 1
            continue
        try:
            schema.validate("envelope", env)
        except ValueError:
            skipped["sobre invalido"] += 1
            continue
        good.append(env)
    good.sort(key=lambda e: e["occurred_at"])
    return good, skipped


def main(argv=None, ddb=None, gcp=None) -> dict:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dry-run", action="store_true", help="solo cuenta; no sube a GCS ni corre load jobs")
    p.add_argument("--registry", default="fp-registry")
    p.add_argument("--inbox", default="fp-inbox")
    p.add_argument("--bucket", default=os.environ.get("LANDING_BUCKET", "fp-landing-g19-2026"))
    p.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID", "fleetpulse-g19-2026"))
    p.add_argument("--dataset", default="fleetpulse")
    p.add_argument("--chunk", type=int, default=500, help="eventos por load job")
    p.add_argument("--sa-secret", default="fleetpulse/gcp/sa-exporter")
    args = p.parse_args(argv)

    from exp import events

    if ddb is None:
        import boto3

        ddb = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        account = boto3.client("sts").get_caller_identity()["Account"]
        print(f"cuenta AWS {account} (debe ser la del Learner Lab)", file=sys.stderr)

    envs, skipped = envelopes_from_items(scan_ev_items(ddb, args.registry))
    loaded_before = events.already_loaded(ddb, args.inbox, sorted({e["event_id"] for e in envs}))
    pending = [e for e in envs if e["event_id"] not in loaded_before]
    summary = {"ev_items": len(envs) + sum(skipped.values()), "valid": len(envs), "already_loaded": len(loaded_before),
               "pending": len(pending), "by_type": dict(Counter(e["event_type"] for e in pending)),
               "skipped": dict(skipped), "dry_run": args.dry_run, "objects": []}

    if not args.dry_run and pending:
        if gcp is None:
            from fplib import secrets

            from exp.gcp import GcpClient

            gcp = GcpClient(secrets.get_json(args.sa_secret))
        for i in range(0, len(pending), args.chunk):
            out = events.load_events(pending[i:i + args.chunk], gcp=gcp, ddb=ddb, bucket=args.bucket,
                                     project=args.project, dataset=args.dataset, inbox=args.inbox)
            if out["object"]:
                summary["objects"].append(out["object"])
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    main()
