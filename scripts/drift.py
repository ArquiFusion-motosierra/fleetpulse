#!/usr/bin/env python3
"""Enciende o apaga un escenario del simulador en SSM /fleetpulse/sim/scenarios.

    scripts/drift.py on S1
    scripts/drift.py off S1
    scripts/drift.py off all
    scripts/drift.py status

Owner: Alex (T06). El parametro es un JSON {"S1": {"since": "<UTC ISO>"}, "S2": false, ...}.
"since" marca el lote 0 de la deriva: el simulador avanza la falla un paso por minuto real.
"""
import json
import sys
from datetime import datetime, timezone

PARAM = "/fleetpulse/sim/scenarios"
LIVE = ("S1", "S2", "S5")  # S3, S4 y S6 son rasgos fijos de la flota: siempre activos


def read(ssm) -> dict:
    try:
        return json.loads(ssm.get_parameter(Name=PARAM)["Parameter"]["Value"])
    except ssm.exceptions.ParameterNotFound:
        return {}


def write(ssm, value: dict) -> None:
    ssm.put_parameter(Name=PARAM, Value=json.dumps(value), Type="String", Overwrite=True)


def apply(state: dict, action: str, name: str, now: datetime) -> dict:
    state = dict(state)
    names = LIVE if name == "all" else (name,)
    for n in names:
        if n not in LIVE:
            raise SystemExit(f"{n} no es un escenario en vivo; usa uno de {', '.join(LIVE)}")
        state[n] = {"since": now.strftime("%Y-%m-%dT%H:%M:%SZ")} if action == "on" else False
    return state


def main(argv: list[str]) -> None:
    if not argv or argv[0] not in ("on", "off", "status") or (argv[0] != "status" and len(argv) != 2):
        raise SystemExit(__doc__)
    import boto3

    ssm = boto3.client("ssm", region_name="us-east-1")
    state = read(ssm)
    if argv[0] != "status":
        state = apply(state, argv[0], argv[1].upper() if argv[1] != "all" else "all", datetime.now(timezone.utc))
        write(ssm, state)
    for n in LIVE:
        v = state.get(n)
        print(f"{n}: {'ON desde ' + v['since'] if isinstance(v, dict) and v.get('since') else 'off'}")


if __name__ == "__main__":
    main(sys.argv[1:])
