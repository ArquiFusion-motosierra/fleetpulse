#!/usr/bin/env python3
"""Smoke en vivo de incident-flow (T14) en el Learner Lab: S1 y S2 de punta a punta, con la aprobacion por el
enlace del correo y el cierre por la API.

    scripts/smoke_incidents.py s1             # EX-017: schedule -> espera aprobacion -> escala a immediate -> aprobar
                                              #   -> asigna, reserva bomba y sellos -> cerrar -> tecnico libre
    scripts/smoke_incidents.py s2             # EX-003, VQ-011, VQ-014, TO-002 en una celda: un grupo, causa comun
                                              #   L-2311, aprobar los schedule -> cerrar los cuatro
    scripts/smoke_incidents.py all            # s1 y despues s2
    scripts/smoke_incidents.py s1 --sim       # con el simulador: drift.py on S1 y espera la ejecucion que inicia
                                              #   `score` (antes: scripts/simulator.sh on; tarda ~8-15 min)
    --close-stale   cierra antes (API de cierre) los incidentes abiertos de esas maquinas
    --keep-open     no cierra al final (para mirar la consola)

Modo directo (por defecto): arma la entrada como aws/scoring (posicion de MACHINE#<id>/META en fp-state o el
centroide de la zona) y llama StartExecution con nombre = incident_id. En S2 tambien hace POST /context con la
recarga del lote L-2311 y escribe los CELL# que dejaria `score`, con las cuatro maquinas en la misma celda (la
premisa del escenario). Lee el task token del historial de la ejecucion (evento TaskScheduled de WaitForApproval)
para armar el mismo enlace que recibe el supervisor, lo imprime, lo abre (GET: pagina de confirmacion, no aprueba)
y pulsa "Aprobar" (POST del formulario).
Verifica: rutas de la ejecucion, META en fp-registry, EV# del incidente, `incident.closed` en fp-outbox y que el
tecnico quede libre en fp-state. Reserva de stock (aws/registry) y explicacion de Gemini (GCP) solo avisan (WARN).

Solo corre en el Learner Lab (rol `voclabs`) o con FP_ACCOUNT_ID igual a la cuenta de las credenciales: crea y
cierra incidentes reales. Owner: Benjamin (T14).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote, urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(ROOT, "aws", "shared", "python"))

from fplib import clock, geohash, ids  # noqa: E402

REGION = os.environ.get("AWS_REGION", "us-east-1")
EXECUTION_TIMEOUT_S = 180
WAIT_RESOURCE = "invoke.waitForTaskToken"  # taskScheduledEventDetails.resource de lambda:invoke.waitForTaskToken
S1 = [{"var": "hydraulic_pressure_bar", "value": 287.4, "z": -2.6},
      {"var": "hydraulic_oil_temp_c", "value": 71.9, "z": 2.1}, {"var": "vibration_mm_s", "value": 9.8, "z": 1.9}]
S2 = [{"var": "fuel_rate_lph", "value": 41.2, "z": 2.9}, {"var": "engine_rpm", "value": 1710.0, "z": 1.6},
      {"var": "coolant_temp_c", "value": 94.1, "z": 1.4}]
S2_SCORES = {"EX-003": 3.8, "VQ-011": 3.6, "VQ-014": 2.4, "TO-002": 2.2}  # dos immediate y dos con aprobacion
S2_LOT, S2_STATION = "L-2311", "EST-NORTE"
FAILED: list[str] = []


def ok(msg):
    print(f"  OK   {msg}")


def warn(msg):
    print(f"  WARN {msg}")


def fail(msg):
    print(f"  FAIL {msg}", file=sys.stderr)
    FAILED.append(msg)


def check(cond, msg) -> bool:
    (ok if cond else fail)(msg)
    return bool(cond)


# --- puro (lo prueba aws/incidents/tests/test_flow_e2e.py) ----------------------------------------------------------

def build_input(machine_id: str, position: dict, score: float, failure_mode: str, deviated: list[dict],
                now: datetime, window: datetime | None = None) -> dict:
    """Entrada de incident-flow como la arma aws/scoring. `position`: zone_id, lat, lon (y geohash6 si se fuerza
    la celda). `window`: instante del que sale el incident_id (por defecto `now`)."""
    lat, lon = float(position["lat"]), float(position["lon"])
    ts = clock.iso(now)
    return {
        "incident_id": ids.incident_id(machine_id, window or now), "machine_id": machine_id,
        "model": machine_id.split("-", 1)[0], "serial": position.get("serial", ""), "zone_id": position["zone_id"],
        "lat": lat, "lon": lon, "geohash6": position.get("geohash6") or geohash.encode(lat, lon, 6), "ts": ts,
        "batch_id": f"B-{now.strftime('%Y%m%dT%H%M%SZ')}-5m0k3000", "score": score,
        "severity": "immediate" if score >= 3.5 else "schedule",
        "z": {d["var"]: {"self": d["z"], "peer": None} for d in deviated},
        "deviated_vars": [dict(d) for d in deviated], "failure_mode": failure_mode, "hours_total": 5000,
    }


def approval_link(api_url: str, token: str, tech_id: str | None, incident_id: str) -> str:
    """El mismo enlace que manda WaitForApproval (flow/wait_for_approval.py)."""
    query = {"tech": tech_id} if tech_id else {}
    return f"{api_url.rstrip('/')}/approvals/{quote(token.translate(str.maketrans('+/', '-_')), safe='')}?" + urlencode({**query, "inc": incident_id})


def waiting_token(history: list[dict]) -> dict | None:
    """Task token y tecnico propuesto de un WaitForApproval que ya mando el correo (TaskSubmitted) y sigue
    esperando, desde el historial de la ejecucion (eventos en orden). None si no esta esperando. Un reintento de la
    tarea trae otro token: vale el ultimo."""
    scheduled = found = None
    for ev in history:
        if ev["type"] == "TaskScheduled" and ev["taskScheduledEventDetails"]["resource"] == WAIT_RESOURCE:
            payload = json.loads(ev["taskScheduledEventDetails"]["parameters"])["Payload"]
            top = (payload["input"].get("ranking") or {}).get("top") or []
            scheduled, found = {"token": payload["token"], "tech_id": top[0]["tech_id"] if top else None}, None
        elif ev["type"] == "TaskSubmitted" and ev["taskSubmittedEventDetails"]["resource"] == WAIT_RESOURCE:
            found = scheduled
        elif ev["type"] == "TaskStateExited" and ev["stateExitedEventDetails"]["name"] == "WaitForApproval":
            scheduled = found = None
    return found


def visited(history: list[dict]) -> list[str]:
    return [ev["stateEnteredEventDetails"]["name"] for ev in history if ev["type"].endswith("StateEntered")
            and "stateEnteredEventDetails" in ev]


# --- AWS -------------------------------------------------------------------------------------------------------------

class Lab:
    def __init__(self):
        import boto3
        from boto3.dynamodb.conditions import Attr, Key
        self.Key, self.Attr = Key, Attr
        self.sts, self.sfn = boto3.client("sts", region_name=REGION), boto3.client("stepfunctions", region_name=REGION)
        ssm, sm = boto3.client("ssm", region_name=REGION), boto3.client("secretsmanager", region_name=REGION)
        self.ssm = ssm
        ddb = boto3.resource("dynamodb", region_name=REGION)
        self.state, self.registry, self.outbox = ddb.Table("fp-state"), ddb.Table("fp-registry"), ddb.Table("fp-outbox")
        ident = self.sts.get_caller_identity()
        self.account = ident["Account"]
        if ":assumed-role/voclabs/" not in ident["Arn"] and os.environ.get("FP_ACCOUNT_ID") != self.account:
            raise SystemExit(f"Las credenciales no son del Learner Lab ({ident['Arn']}). Exporta las del lab con "
                             "scripts/lab-creds.sh, o FP_ACCOUNT_ID=<cuenta> si es otra cuenta de pruebas.")
        self.machine_arn = f"arn:aws:states:{REGION}:{self.account}:stateMachine:incident-flow"
        self.api = ssm.get_parameter(Name="/fleetpulse/api/url")["Parameter"]["Value"].rstrip("/")
        self.keys = {n: json.loads(sm.get_secret_value(SecretId=f"fleetpulse/api/{n}")["SecretString"])["api_key"]
                     for n in ("console-key", "ingest-key")}

    def execution_arn(self, name: str) -> str:
        return f"arn:aws:states:{REGION}:{self.account}:execution:incident-flow:{name}"

    def history(self, arn: str) -> list[dict]:
        events, kwargs = [], {"executionArn": arn, "maxResults": 1000}
        while True:
            page = self.sfn.get_execution_history(**kwargs)
            events += page["events"]
            if not page.get("nextToken"):
                return events
            kwargs["nextToken"] = page["nextToken"]

    def start(self, payload: dict) -> str | None:
        try:
            return self.sfn.start_execution(stateMachineArn=self.machine_arn, name=payload["incident_id"],
                                            input=json.dumps(payload))["executionArn"]
        except self.sfn.exceptions.ExecutionAlreadyExists:
            fail(f"{payload['incident_id']} ya existe: espera a la siguiente ventana de 15 min y vuelve a correr")
            return None

    def wait(self, arn: str, approval: bool, timeout: float = EXECUTION_TIMEOUT_S) -> tuple[str, dict | None]:
        """Espera a que la ejecucion termine o (si `approval`) quede en WaitForApproval. (status, token)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            status = self.sfn.describe_execution(executionArn=arn)["status"]
            if status != "RUNNING":
                return status, None
            if approval and (tok := waiting_token(self.history(arn))):
                return "WAITING", tok
            time.sleep(3)
        return "TIMEOUT", None

    def meta(self, incident_id: str) -> dict:
        return self.registry.get_item(Key={"PK": f"INCIDENT#{incident_id}", "SK": "META"},
                                      ConsistentRead=True).get("Item") or {}

    def ev_types(self, incident_id: str) -> list[str]:
        items = self.registry.query(KeyConditionExpression=self.Key("PK").eq(f"INCIDENT#{incident_id}")
                                    & self.Key("SK").begins_with("EV#"))["Items"]
        return sorted(i["event_type"] for i in items)

    def in_outbox(self, incident_id: str, event_type: str) -> bool:
        kwargs = {"FilterExpression": self.Attr("incident_id").eq(incident_id) & self.Attr("type").eq(event_type),
                  "ProjectionExpression": "event_id"}
        while True:
            page = self.outbox.scan(**kwargs)
            if page["Items"]:
                return True
            if "LastEvaluatedKey" not in page:
                return False
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def tech(self, tech_id: str) -> dict:
        return self.state.get_item(Key={"PK": f"TECH#{tech_id}", "SK": "META"}, ConsistentRead=True).get("Item") or {}

    def position(self, machine_id: str) -> dict:
        meta = self.state.get_item(Key={"PK": f"MACHINE#{machine_id}", "SK": "META"}).get("Item") or {}
        zone = meta.get("zone_id") or ids.machines()[machine_id]["zone_id"]
        if meta.get("lat") is None:
            z = ids.zones()[zone]
            return {"zone_id": zone, "lat": z["lat"], "lon": z["lon"]}
        return {"zone_id": zone, "lat": float(meta["lat"]), "lon": float(meta["lon"]), "serial": meta.get("serial", "")}

    def open_incidents(self, machine_id: str) -> list[str]:
        items = self.registry.query(IndexName="GSI1", KeyConditionExpression=self.Key("GSI1PK").eq(
            f"MACHINE#{machine_id}") & self.Key("GSI1SK").begins_with("INC#"),
            FilterExpression=self.Attr("status").eq("open"))["Items"]
        return [i["incident_id"] for i in items]

    def http(self, method: str, path_or_url: str, body=None, key: str | None = None,
             form: dict | None = None) -> tuple[int, str]:
        """`body` va como JSON; `form` como un formulario HTML (application/x-www-form-urlencoded)."""
        url = path_or_url if path_or_url.startswith("https://") else self.api + path_or_url
        headers = {"content-type": "application/x-www-form-urlencoded" if form is not None else "application/json"}
        if key:
            headers["x-api-key"] = self.keys[key]
        data = urlencode(form).encode() if form is not None else json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()


# --- pasos -----------------------------------------------------------------------------------------------------------

def approve(lab: Lab, incident_id: str, tok: dict) -> None:
    link = approval_link(lab.api, tok["token"], tok["tech_id"], incident_id)
    print(f"  enlace de aprobacion (el mismo del correo al supervisor):\n    {link}")
    # GET solo muestra la confirmacion (los escaneres de correo abren los enlaces); el boton hace POST a la misma URL
    code, body = lab.http("GET", link)
    asks = '<form method="post">' in body and incident_id in body
    check(code == 200 and asks, f"GET /approvals -> {code} {'pide confirmar' if asks else body[:80]}")
    if lab.meta(incident_id).get("tech_id"):
        fail(f"{incident_id} asignado solo con el GET del enlace")
    code, body = lab.http("POST", link, form={"confirm": "1"})
    done = "Aprobado" in body
    check(code == 200 and done, f"POST /approvals -> {code} {'Aprobado' if done else body[:80]}")


def close_and_check(lab: Lab, incident_id: str, parts_used: list[dict]) -> None:
    meta = lab.meta(incident_id)
    tech_id = meta.get("tech_id")
    code, body = lab.http("POST", f"/incidents/{incident_id}/close", key="console-key",
                          body={"closed_by": tech_id or "smoke", "parts_used": parts_used,
                                "cause_summary": "smoke T14"})
    check(code == 200, f"POST /incidents/{incident_id}/close -> {code} {body[:120]}")
    meta = lab.meta(incident_id)
    check(meta.get("status") == "closed", f"{incident_id} META status {meta.get('status')}")
    if tech_id:
        t = lab.tech(tech_id)
        check(t.get("available") is True and t.get("current_incident") is None,
              f"{tech_id} libre (available {t.get('available')}, current_incident {t.get('current_incident')})")
    check("incident.closed" in lab.ev_types(incident_id), f"{incident_id} EV# incident.closed")
    check(lab.in_outbox(incident_id, "incident.closed"), f"{incident_id} incident.closed en fp-outbox")


def after_assign(lab: Lab, incident_id: str, arn: str) -> dict:
    path = visited(lab.history(arn))
    check("NotifyByRole" in path and path[-2:] == ["ReserveParts", "Done"], f"{incident_id} ruta: {' > '.join(path)}")
    meta = lab.meta(incident_id)
    check(meta.get("tech_id"), f"{incident_id} asignado a {meta.get('tech_id')} (approval {meta.get('approval')})")
    if meta.get("tech_id") and not meta.get("tech_busy"):
        check(lab.tech(meta["tech_id"]).get("current_incident") == incident_id, f"{meta['tech_id']} reservado")
    check(meta.get("parts_requested_at"), f"{incident_id} repuestos pedidos: {meta.get('parts_requested')}")
    source = (meta.get("explanation") or {}).get("source")
    (ok if source == "gemini" else warn)(f"{incident_id} explicacion {source} (template = Cloud Run /explain no "
                                         "respondio; ver GCP)")
    return meta


def reservation_landed(lab: Lab, incident_id: str) -> None:
    for _ in range(10):
        if {"part.reserved", "part.shortage"} & set(lab.ev_types(incident_id)):
            ok(f"{incident_id} aws/registry aplico la reserva")
            return
        time.sleep(3)
    warn(f"{incident_id} sin part.reserved ni part.shortage en 30 s (consumidor aws/registry, Alex)")


def close_stale(lab: Lab, machines) -> None:
    for mid in machines:
        for inc in lab.open_incidents(mid):
            code, _ = lab.http("POST", f"/incidents/{inc}/close", key="console-key",
                               body={"closed_by": "smoke", "parts_used": [], "cause_summary": "smoke: limpieza"})
            print(f"  cierre previo de {inc} -> {code}")


def precheck(lab: Lab, machines, args) -> bool:
    if args.close_stale:
        close_stale(lab, machines)
    stale = {m: lab.open_incidents(m) for m in machines}
    stale = {m: v for m, v in stale.items() if v}
    return check(not stale, f"sin incidentes abiertos previos ({stale or 'ninguno'}; --close-stale los cierra)")


def wait_sim_execution(lab: Lab, machine_id: str, since: datetime, minutes: int) -> str | None:
    print(f"  esperando hasta {minutes} min la ejecucion de {machine_id} que inicia `score`...")
    end = time.monotonic() + minutes * 60
    while time.monotonic() < end:
        for page in lab.sfn.get_paginator("list_executions").paginate(stateMachineArn=lab.machine_arn,
                                                                        PaginationConfig={"MaxItems": 200}):
            for e in page["executions"]:
                if e["name"].startswith(f"INC-{machine_id}-") and e["startDate"] >= since:
                    return e["executionArn"]
        time.sleep(15)
    fail(f"sin ejecucion de {machine_id} en {minutes} min (simulador encendido? lineas base sembradas?)")
    return None


def drift(lab: Lab, action: str, name: str) -> None:
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import drift as d  # scripts/drift.py (Alex, T06)
    d.write(lab.ssm, d.apply(d.read(lab.ssm), action, name, datetime.now(timezone.utc)))
    print(f"  drift {action} {name}")


def s1(lab: Lab, args) -> None:
    print("[s1] EX-017 bomba hidraulica")
    if not precheck(lab, ["EX-017"], args):
        return
    now = datetime.now(timezone.utc)
    if args.sim:
        drift(lab, "on", "S1")
        arn = wait_sim_execution(lab, "EX-017", now, 25)
        if not arn:
            return
        inc = arn.rsplit(":", 1)[1]
    else:
        payload = build_input("EX-017", lab.position("EX-017"), 2.4, "hydraulic_pump", S1, now)
        inc, arn = payload["incident_id"], lab.start(payload)
        if not arn:
            return
    status, tok = lab.wait(arn, approval=True)
    if not check(status in ("WAITING", "SUCCEEDED"), f"{inc} {status}"):
        return
    if not args.sim:
        worse = build_input("EX-017", lab.position("EX-017"), 3.7, "hydraulic_pump", S1, now,
                            window=now + timedelta(minutes=15))
        esc = lab.start(worse)
        if esc:
            status_esc, _ = lab.wait(esc, approval=False)
            path = visited(lab.history(esc))
            check(status_esc == "SUCCEEDED" and "NotifyEscalation" in path, f"escalamiento {status_esc}: "
                                                                             f"{' > '.join(path)}")
            check(lab.meta(inc).get("severity") == "immediate", f"{inc} severidad immediate")
            check(not lab.meta(worse["incident_id"]), "la ejecucion que escala no abre otro incidente")
    if status == "WAITING":
        approve(lab, inc, tok)
        status, _ = lab.wait(arn, approval=False)
    check(status == "SUCCEEDED", f"{inc} {status}")
    after_assign(lab, inc, arn)
    reservation_landed(lab, inc)
    if args.sim:
        drift(lab, "off", "S1")
    if not args.keep_open:
        close_and_check(lab, inc, [{"part_id": "PRT-04", "zone_id": "NORTE", "qty": 1}])


def s2(lab: Lab, args) -> None:
    print("[s2] EX-003, VQ-011, VQ-014, TO-002 combustible contaminado")
    machines = list(S2_SCORES)
    if not precheck(lab, machines, args):
        return
    now = datetime.now(timezone.utc)
    runs = {}
    if args.sim:
        drift(lab, "on", "S2")
        for mid in machines:
            arn = wait_sim_execution(lab, mid, now, 25)
            if arn:
                runs[mid] = (arn.rsplit(":", 1)[1], arn)
    else:
        z = ids.zones()["NORTE"]
        spot = {"zone_id": "NORTE", "lat": z["lat"], "lon": z["lon"]}
        gh = geohash.encode(spot["lat"], spot["lon"], 6)
        ctx = [{"context_id": f"CTX-{now.strftime('%Y%m%dT%H%M%SZ')}-5m0k{k:02d}", "type": "fuel.refill",
                "ts": clock.iso(now - timedelta(minutes=10)), "zone_id": "NORTE", "machine_id": mid,
                "station_id": S2_STATION, "fuel_lot": S2_LOT, "liters": 320} for k, mid in enumerate(machines)]
        code, body = lab.http("POST", "/context", body=ctx, key="ingest-key")
        check(code in (200, 202), f"POST /context (recargas {S2_LOT}) -> {code} {body[:80]}")
        for mid in machines:  # lo que escribe aws/scoring para cada anomalia (CELL#, ttl 2 h)
            lab.state.put_item(Item={"PK": f"CELL#{gh}", "SK": f"A#{clock.iso(now)}#{mid}", "machine_id": mid,
                                     "score": Decimal(str(S2_SCORES[mid])), "severity": "schedule",
                                     "deviated_vars": ["fuel_rate_lph"],
                                     "ttl": int((now + timedelta(hours=2)).timestamp())})
        for mid in machines:
            payload = build_input(mid, {**spot, "geohash6": gh}, S2_SCORES[mid], "fuel_contamination", S2, now)
            arn = lab.start(payload)
            if arn:
                runs[mid] = (payload["incident_id"], arn)
    groups = set()
    for mid, (inc, arn) in runs.items():
        status, tok = lab.wait(arn, approval=True)
        if status == "WAITING":
            approve(lab, inc, tok)
            status, _ = lab.wait(arn, approval=False)
        if not check(status == "SUCCEEDED", f"{inc} {status}"):
            continue
        meta = after_assign(lab, inc, arn)
        groups.add(meta.get("group_id"))
        common = (meta.get("explanation") or {}).get("common_cause")
        (ok if common and S2_LOT in common else warn)(f"{inc} causa comun: {common}")
    check(len(runs) == 4 and len(groups) == 1 and None not in groups, f"las cuatro en un grupo: {groups}")
    if args.sim:
        drift(lab, "off", "S2")
    if not args.keep_open:
        for mid, (inc, _) in runs.items():
            close_and_check(lab, inc, [{"part_id": "PRT-01", "zone_id": "NORTE", "qty": 1}])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario", choices=("s1", "s2", "all"))
    ap.add_argument("--sim", action="store_true", help="usar el simulador (drift.py) en vez de StartExecution")
    ap.add_argument("--close-stale", action="store_true", help="cerrar antes los incidentes abiertos de esas maquinas")
    ap.add_argument("--keep-open", action="store_true", help="no cerrar los incidentes al final")
    args = ap.parse_args(argv)
    lab = Lab()
    for name in (("s1", "s2") if args.scenario == "all" else (args.scenario,)):
        {"s1": s1, "s2": s2}[name](lab, args)
    print("smoke incidents: " + ("VERDE" if not FAILED else f"ROJO ({len(FAILED)} fallas)"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
