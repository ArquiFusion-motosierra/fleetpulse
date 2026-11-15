"""Backfill: 30 dias de historia (10 lecturas por maquina cada 15 min = ~2,9 M lecturas).

    python simulator/backfill.py --days 30 --step 15m --out simulator/out

Salida (simulator/out/ esta en .gitignore):
    telemetry/dt=YYYY-MM-DD/hr=HH/backfill.ndjson.gz   mismo layout que archiver en s3://fp-raw/telemetry/
    context/context.ndjson.gz                          fuel.refill y weather
    events/domain_events.ndjson                        ~40 incidentes historicos (incident.*, part.*) para reproducir en fp-registry
    labels/faults.json                                 fallas inyectadas: verdad de terreno para calibrar el score
Con --s3-bucket sube todo a s3://<bucket>/backfill/.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import re
import time
import uuid
import zlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fpsim.fleet import fleet, load_ids
from fpsim.physics import elapsed_hours, fuel_cycle, iso, reading, refill_context, weather_context
from fpsim.scenarios import Fault, historical_effect

READINGS_PER_STEP = 10
N_HISTORICAL = 40
SEVERITY_BY_MODE = {"hydraulic_pump": "immediate", "engine_overheat": "immediate", "final_drive": "immediate"}


def parse_step(s: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([mh])", s)
    if not m:
        raise argparse.ArgumentTypeError("usa 15m o 1h")
    n = int(m.group(1))
    return timedelta(minutes=n) if m.group(2) == "m" else timedelta(hours=n)


def plan_faults(start: datetime, end: datetime, seed: int = 2311) -> list[Fault]:
    """~40 fallas historicas en horario de turno, con el modo de falla segun el BOM del modelo, mas S5."""
    rng = random.Random(seed)
    ids = load_ids()
    modes_by_model = defaultdict(set)
    for b in ids["bom"]:
        modes_by_model[b["model_id"]].add(b["failure_mode"])
    machines = [m for m in fleet() if m.machine_id not in {"EX-017", "EX-009"}]
    span_days = max((end - start).days, 1)
    faults = []
    # S5: EX-009 tuvo un incidente hidraulico hace 10 dias (cambio de sellos)
    s5_end = (end - timedelta(days=10)).replace(hour=17, minute=0, second=0, microsecond=0)
    if s5_end - timedelta(hours=5) > start:
        faults.append(Fault("EX-009", "hydraulic_contamination", s5_end - timedelta(hours=5), s5_end))
    n = min(N_HISTORICAL - len(faults), max(span_days * 4 // 3, 1))
    for _ in range(n):
        m = rng.choice(machines)
        day = start + timedelta(days=rng.randrange(span_days))
        # Turno pleno en Lima: 11:00-23:00 UTC
        f_start = day.replace(hour=11, minute=0, second=0, microsecond=0) + timedelta(minutes=15 * rng.randrange(28))
        dur = timedelta(hours=rng.choice([3, 4, 5, 6, 8]))
        if f_start < start or f_start + dur > end:
            continue
        mode = rng.choice(sorted(modes_by_model[m.model]))
        faults.append(Fault(m.machine_id, mode, f_start, f_start + dur))
    return sorted(faults, key=lambda f: f.start)


def _uuid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _env(rng, etype, agg_type, agg_id, ts, inc_id, mid, payload):
    return {"event_id": _uuid(rng), "event_type": etype, "schema_version": 1, "source": "simulator.backfill",
            "aggregate_type": agg_type, "aggregate_id": agg_id, "occurred_at": iso(ts),
            "incident_id": inc_id, "machine_id": mid, "payload": payload}


def incident_events(faults: list[Fault], seed: int = 15) -> list[dict]:
    """Eventos de dominio de cada falla historica, con el mismo formato que produce incident-flow."""
    rng = random.Random(seed)
    ids = load_ids()
    machines = {m.machine_id: m for m in fleet()}
    techs = ids["technicians"]
    spec = ids["specialty_by_failure_mode"]
    bom = defaultdict(list)
    for b in ids["bom"]:
        bom[(b["model_id"], b["failure_mode"])].append({"part_id": b["part_id"], "qty": b["qty"]})
    events = []
    for f in faults:
        m = machines[f.machine_id]
        opened = f.start + (f.end - f.start) * 0.7
        window = opened.replace(minute=opened.minute - opened.minute % 15, second=0, microsecond=0)
        inc = f"INC-{m.machine_id}-{window.strftime('%Y%m%dT%H%M')}"
        sev = SEVERITY_BY_MODE.get(f.failure_mode, "schedule")
        cands = [t for t in techs if t["specialty"] == spec[f.failure_mode]]
        tech = next((t for t in cands if t["zone_id"] == m.zone_id), cands[0])
        lines = bom[(m.model, f.failure_mode)] or [{"part_id": "PRT-11", "qty": 1}]
        assigned = opened + timedelta(minutes=rng.randint(2, 30))
        closed = f.end + timedelta(hours=rng.uniform(0.5, 3.0))
        rsv = f"RSV-{inc}"
        events += [
            _env(rng, "incident.opened", "incident", inc, opened, inc, m.machine_id, {
                "zone_id": m.zone_id, "severity": sev, "score": round(rng.uniform(2.0, 4.2), 2),
                "failure_mode": f.failure_mode,
                "deviated_vars": [{"var": "vibration_mm_s", "value": round(m.params["vibration"] * 1.3, 2),
                                   "z": round(rng.uniform(2.0, 3.5), 2)}],
                "group_id": None, "recurrence_flag": False, "opened_at": iso(opened),
                "lat": m.zone_lat, "lon": m.zone_lon}),
            _env(rng, "incident.assigned", "incident", inc, assigned, inc, m.machine_id, {
                "tech_id": tech["tech_id"], "assigned_at": iso(assigned),
                "approval": "auto" if sev == "immediate" else "approved",
                "explanation": {"source": "template", "summary": f"Firma de {f.failure_mode} en {m.machine_id}.",
                                "suggested_parts": [ln["part_id"] for ln in lines]}}),
            _env(rng, "part.reserve.requested", "part_reservation", rsv, assigned + timedelta(seconds=5), inc,
                 m.machine_id, {"zone_id": m.zone_id, "failure_mode": f.failure_mode, "lines": lines}),
            _env(rng, "incident.closed", "incident", inc, closed, inc, m.machine_id, {
                "closed_at": iso(closed), "downtime_h": round((closed - opened).total_seconds() / 3600, 2),
                "parts_used": [{**ln, "zone_id": m.zone_id} for ln in lines],
                "cause_summary": f"Intervencion por {f.failure_mode}.", "closed_by": tech["tech_id"]}),
        ]
    return sorted(events, key=lambda e: e["occurred_at"])


def run(days: int, step: timedelta, out: str, end: datetime | None = None, machine_ids: set[str] | None = None,
        verbose: bool = True) -> dict:
    end = end or datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    faults = plan_faults(start, end)
    by_machine = defaultdict(list)
    for f in faults:
        by_machine[f.machine_id].append(f)
    machines = [m for m in fleet() if not machine_ids or m.machine_id in machine_ids]
    spacing = step / READINGS_PER_STEP
    t0 = time.monotonic()
    n_readings, files = 0, 0
    contexts = []
    hour_start = start
    while hour_start < end:
        lines = []
        t = hour_start
        hour_end = min(hour_start + timedelta(hours=1), end)
        while t < hour_end:
            stamp = t.strftime("%Y%m%dT%H%M%SZ")
            bid = f"B-{stamp}-{zlib.crc32(('bf' + stamp).encode()) & 0xFFFFFFFF:08x}"
            for m in machines:
                for i in range(READINGS_PER_STEP):
                    ti = t + spacing * i
                    r = reading(m, ti, historical_effect(m.machine_id, by_machine, ti))
                    r["batch_id"] = bid
                    lines.append(json.dumps(r, separators=(",", ":")))
            t += step
        for m in machines:  # recargas dentro de la hora
            _, a = fuel_cycle(m, elapsed_hours(hour_start))
            _, b = fuel_cycle(m, elapsed_hours(hour_end))
            if b > a:
                contexts.append(refill_context(m, hour_start + (hour_end - hour_start) / 2))
        contexts.extend(weather_context(z["zone_id"], hour_start) for z in load_ids()["zones"])
        d = os.path.join(out, "telemetry", f"dt={hour_start:%Y-%m-%d}", f"hr={hour_start:%H}")
        os.makedirs(d, exist_ok=True)
        with gzip.open(os.path.join(d, "backfill.ndjson.gz"), "wt", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        n_readings += len(lines)
        files += 1
        if verbose and hour_start.hour == 0:
            print(f"{hour_start:%Y-%m-%d} {n_readings:,} lecturas ({time.monotonic() - t0:.0f} s)", flush=True)
        hour_start = hour_end

    os.makedirs(os.path.join(out, "context"), exist_ok=True)
    with gzip.open(os.path.join(out, "context", "context.ndjson.gz"), "wt", encoding="utf-8") as fh:
        fh.writelines(json.dumps(c, separators=(",", ":")) + "\n" for c in contexts)
    events = incident_events(faults)
    os.makedirs(os.path.join(out, "events"), exist_ok=True)
    with open(os.path.join(out, "events", "domain_events.ndjson"), "w", encoding="utf-8", newline="\n") as fh:
        fh.writelines(json.dumps(e, separators=(",", ":")) + "\n" for e in events)
    os.makedirs(os.path.join(out, "labels"), exist_ok=True)
    with open(os.path.join(out, "labels", "faults.json"), "w", encoding="utf-8") as fh:
        json.dump([{"machine_id": f.machine_id, "failure_mode": f.failure_mode, "start": iso(f.start),
                    "end": iso(f.end)} for f in faults], fh, indent=1)
    summary = {"start": iso(start), "end": iso(end), "readings": n_readings, "hour_files": files,
               "context": len(contexts), "faults": len(faults), "domain_events": len(events),
               "seconds": round(time.monotonic() - t0, 1)}
    if verbose:
        print(json.dumps(summary))
    return summary


def upload_s3(out: str, bucket: str, prefix: str = "backfill/") -> int:
    import boto3
    s3 = boto3.client("s3")
    n = 0
    for root, _dirs, names in os.walk(out):
        for name in names:
            path = os.path.join(root, name)
            key = prefix + os.path.relpath(path, out).replace(os.sep, "/")
            s3.upload_file(path, bucket, key)
            n += 1
    return n


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--step", type=parse_step, default=timedelta(minutes=15))
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(__file__)), "out"))
    ap.add_argument("--end", help="fin del backfill en UTC ISO (por defecto: la hora actual)")
    ap.add_argument("--machines", help="lista separada por comas (por defecto: las 100)")
    ap.add_argument("--s3-bucket", help="sube la salida a s3://<bucket>/backfill/")
    a = ap.parse_args(argv)
    end = datetime.fromisoformat(a.end.replace("Z", "+00:00")) if a.end else None
    run(a.days, a.step, a.out, end, set(a.machines.split(",")) if a.machines else None)
    if a.s3_bucket:
        print(f"subidos {upload_s3(a.out, a.s3_bucket)} archivos a s3://{a.s3_bucket}/backfill/")


if __name__ == "__main__":
    main()
