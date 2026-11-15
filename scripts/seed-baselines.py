#!/usr/bin/env python3
"""Siembra la linea base propia de cada maquina (MACHINE#<id>/META.baseline) con 24 h de historia simulada.

    python scripts/seed-baselines.py --dry-run   # calcula y resume, sin escribir (no necesita AWS)
    python scripts/seed-baselines.py             # escribe baseline y baseline_n en fp-state
    python scripts/seed-baselines.py --end 2026-11-15T14:00:00Z --hours 24 --step-min 1

Owner: Benjamin (T09, fix C1). El simulador es determinista: `fpsim.live.build_batch(t)` para un `t` pasado
reproduce exactamente el lote que habria enviado, sin escenarios. Se toma un lote por minuto (lo mismo que el
vivo), se calcula mu/sigma por variable con las lecturas `operating` usando `scoring.baselines.from_readings`
(la misma funcion que usa `score`) y se escribe en META, que debe existir (`scripts/seed-registry.py`).
Idempotente: `--end` por defecto es la hora en punto actual, asi que repetirlo en la misma hora escribe lo
mismo; pisa cualquier linea base anterior (tambien la de arranque en frio de `score`).
Correr una vez despues del deploy y despues de cualquier cambio de fisica del simulador, con
`scripts/drift.py off all`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(ROOT, "simulator"), os.path.join(ROOT, "aws", "scoring")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

TABLE = os.environ.get("STATE_TABLE", "fp-state")
REGION = os.environ.get("AWS_REGION", "us-east-1")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def default_end(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)


def machine_baselines(end: datetime, hours: int = 24, step_min: int = 1,
                      machine_ids: set[str] | None = None) -> dict[str, tuple[dict | None, int]]:
    """{machine_id: (baseline o None, lecturas operating)} con los lotes de [end - hours, end), sin escenarios.
    Funcion pura (determinista por `end`). Genera maquina por maquina para no tener 1,4 M de lecturas en memoria."""
    from fpsim.fleet import fleet
    from fpsim.live import build_batch
    from scoring import baselines

    times = [end - timedelta(minutes=step_min * i) for i in range(hours * 60 // step_min, 0, -1)]
    out = {}
    for m in fleet():
        if machine_ids and m.machine_id not in machine_ids:
            continue
        ids = {m.machine_id}
        rows = [r for t in times for r in build_batch(t, {}, 5, None, ids)["readings"]
                if r["engine_status"] == "operating"]
        out[m.machine_id] = (baselines.from_readings(rows), len(rows))
    return out


def write(table, computed: dict[str, tuple[dict | None, int]]) -> dict[str, list[str]]:
    """Escribe cada linea base en META. Devuelve las maquinas escritas, sin META y sin lecturas suficientes."""
    from botocore.exceptions import ClientError

    from scoring import baselines

    res: dict[str, list[str]] = {"written": [], "no_meta": [], "insufficient": []}
    for machine_id, (baseline, n) in sorted(computed.items()):
        if baseline is None:
            res["insufficient"].append(machine_id)
            continue
        try:
            baselines.persist(table, machine_id, baseline, n)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            res["no_meta"].append(machine_id)
            continue
        res["written"].append(machine_id)
    return res


def summary(computed: dict[str, tuple[dict | None, int]], end: datetime, hours: int) -> dict:
    ns = [n for _, n in computed.values()]
    return {"machines": len(computed), "window": [(end - timedelta(hours=hours)).isoformat(), end.isoformat()],
            "operating_readings": {"min": min(ns), "max": max(ns)},
            "insufficient": sorted(m for m, (b, _) in computed.items() if b is None)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="calcula y resume, sin escribir en DynamoDB")
    ap.add_argument("--end", help="fin de la ventana (UTC ISO); por defecto la hora en punto actual")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--step-min", type=int, default=1, help="minutos entre lotes (1 = como el simulador en vivo)")
    a = ap.parse_args(argv)
    end = _parse(a.end) if a.end else default_end()
    computed = machine_baselines(end, a.hours, a.step_min)
    print(json.dumps(summary(computed, end, a.hours)))
    if a.dry_run:
        print(json.dumps({"dry_run": True, "would_write": sum(1 for b, _ in computed.values() if b is not None)}))
        return 0
    import boto3

    res = write(boto3.resource("dynamodb", region_name=REGION).Table(TABLE), computed)
    print(json.dumps({"written": len(res["written"]), "no_meta": res["no_meta"], "insufficient": res["insufficient"]}))
    return 1 if res["no_meta"] or res["insufficient"] else 0


if __name__ == "__main__":
    sys.exit(main())
