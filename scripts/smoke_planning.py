#!/usr/bin/env python3
"""Smoke en vivo de daily-planning (T18) en el Learner Lab: corrida manual de la maquina de estados y verificacion
de lo que escribe (fp-outbox, fp-inbox, fp-registry), lo que llega a BigQuery y lo que sirven la API y la consola.

    read -rs FP_TEST_PASSWORD && export FP_TEST_PASSWORD   # clave de jefe.mantenimiento (paso 10); nunca en archivos
    AWS_CONFIG_FILE=/dev/null AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 .venv/bin/python scripts/smoke_planning.py
    ... --dry-run-only   # pasos 1-3 y 14: solo calcula, no escribe ordenes ni pedidos
    ... --no-rerun       # sin la segunda corrida (queda WARN: no es verde)
    ... --console https://console-...run.app

Salida: 0 = todos los pasos OK; 1 = algun FAIL; 2 = sin FAIL pero con algun WARN (no cuenta como verde).

Cada corrida real escribe de verdad (es la aceptacion de T18): `maintenance.order.suggested` en fp-outbox (registry ->
`ORDER#<maquina>/<due_at>`, status suggested) y `REORDER#<part>#<zone>/<created_at>` con `source: planning`. Repetirla
el mismo dia de Lima no duplica (reclamos `planning:<dia>:...` en fp-inbox). Si esta corrida no escribe nada porque
otra de hoy ya lo hizo, los pasos 5-12 verifican lo que escribio esa otra ejecucion (se busca en el historial).
Los datos quedan (son la demo); la limpieza del `finally` solo detiene ejecuciones de este smoke que sigan RUNNING.
No toca la regla diaria (DISABLED por defecto). Owner: Benjamin (T18).

Pasos (de aws/planning/planning/{handler,store,plan}.py, aws/registry/reg/{apply,api}.py, gcp/console/app/main.py,
aws/integrations/gcp-exporter/exp/events.py):
  1 regla diaria                          8 REORDER# de hoy, source=planning, sin duplicados, qty consistente
  2 dry-run SUCCEEDED, no escribe         9 GET /orders y /reorders de la API de registry, todos los parques
  3 formula local = Lambda               10 consola /api/orders y /api/reorders sin token y como jefe.mantenimiento
  4 corrida real SUCCEEDED               11 KPI# en fp-registry coherente con /api/kpis (T18 no escribe KPI#)
  5 eventos en fp-outbox (trace_id)      12 maintenance.order.suggested en BigQuery domain_events (load job)
  6 reclamos en fp-inbox                 13 segunda corrida: no escribe nada
  7 ORDER# en fp-registry (registry)     14 la regla diaria no cambio
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(ROOT, "aws", "shared", "python"))
sys.path.append(os.path.join(ROOT, "aws", "planning"))

REGION = os.environ.get("AWS_REGION", "us-east-1")
STACK = os.environ.get("STACK", "fp-planning-dev")
CONSOLE = "https://console-qgbfzxdq2a-ue.a.run.app"
GCP_PROJECT = "fleetpulse-g19-2026"
CLIENT_ID = "ekbd3losd6961grf2emp3e9mi"  # app client de Cognito (USER_PASSWORD_AUTH), el mismo de smoke_reports
HEAD_USER = "jefe.mantenimiento@example.com"  # rol maintenance_head: ve todos los parques
EXEC_TIMEOUT_S = 240   # Lambda plan: 60 s x (1 + 3 reintentos) como maximo
REGISTRY_POLL_S, POLL_EVERY_S = 120, 3  # outbox stream -> SNS -> registry-q -> Lambda registry: segundos
BQ_POLL_S, BQ_EVERY_S = 300, 30  # gcp-events-q (ventana 60 s) -> load job (hasta 120 s)
STATES_OK = ["PlanPreventive"]
LIMA = timezone(timedelta(hours=-5))
NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,80}$")


class Fail(Exception):
    pass


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def lima_day_start(day: str) -> datetime:
    return datetime.combine(date.fromisoformat(day), datetime.min.time(), LIMA)


# --- AWS / GCP -------------------------------------------------------------------------------------------------------

class Lab:
    """Lecturas de DynamoDB, Step Functions, API, consola y BigQuery. Las unicas escrituras son StartExecution (las
    corridas del smoke) y StopExecution de esas mismas corridas si quedan RUNNING al salir."""

    def __init__(self, console: str = CONSOLE):
        import boto3
        from fplib import ids
        self.console = console.rstrip("/")
        self.sfn = boto3.client("stepfunctions", region_name=REGION)
        self.events = boto3.client("events", region_name=REGION)
        self.cfn = boto3.client("cloudformation", region_name=REGION)
        self.ddb = boto3.client("dynamodb", region_name=REGION)
        ident = boto3.client("sts", region_name=REGION).get_caller_identity()
        self.account = ident["Account"]
        if ":assumed-role/voclabs/" not in ident["Arn"] and os.environ.get("FP_ACCOUNT_ID") != self.account:
            raise SystemExit(f"Las credenciales no son del Learner Lab ({ident['Arn']}). Exporta las del lab con "
                             "eval \"$(scripts/lab-creds.sh)\", o FP_ACCOUNT_ID=<cuenta> si es otra cuenta de pruebas.")
        # El ids.json vendorizado en aws/shared/python/fplib/data puede ser viejo (solo mineria): el catalogo de la
        # layer desplegada sale de contracts/ (los tres parques), asi que el recalculo local usa ese.
        merged = ids.merge_catalogs(os.path.join(ROOT, "contracts"))
        ids.load = lambda: merged
        self.machine_arn = f"arn:aws:states:{REGION}:{self.account}:stateMachine:daily-planning"
        ssm = boto3.client("ssm", region_name=REGION)
        sm = boto3.client("secretsmanager", region_name=REGION)
        self.api = ssm.get_parameter(Name="/fleetpulse/registry/url")["Parameter"]["Value"].rstrip("/")
        self.key = json.loads(sm.get_secret_value(SecretId="fleetpulse/api/console-key")["SecretString"])["api_key"]

    # ---- catalogo ----
    def tenants(self) -> list[str]:
        from fplib import ids
        return list(ids.tenants())

    def tenant_of(self, machine_id: str) -> str:
        from fplib import ids
        return ids.tenant_of_machine(machine_id)

    def tenant_of_zone(self, zone_id: str) -> str:
        from fplib import ids
        return ids.tenant_of_zone(zone_id)

    # ---- Step Functions / EventBridge ----
    def rule_state(self) -> str | None:
        res = self.cfn.describe_stack_resources(StackName=STACK)["StackResources"]
        rules = [r["PhysicalResourceId"] for r in res if r["ResourceType"] == "AWS::Events::Rule"]
        return self.events.describe_rule(Name=rules[0])["State"] if rules else None

    def start(self, payload: dict, name: str) -> str:
        return self.sfn.start_execution(stateMachineArn=self.machine_arn, name=name,
                                        input=json.dumps(payload))["executionArn"]

    def describe(self, arn: str) -> dict:
        return self.sfn.describe_execution(executionArn=arn)

    def stop(self, arn: str) -> None:
        self.sfn.stop_execution(executionArn=arn, error="SmokeAborted", cause="smoke_planning interrumpido")

    def visited(self, arn: str) -> list[str]:
        events, kwargs = [], {"executionArn": arn, "maxResults": 1000}
        while True:
            page = self.sfn.get_execution_history(**kwargs)
            events += page["events"]
            if not page.get("nextToken"):
                break
            kwargs["nextToken"] = page["nextToken"]
        return [e["stateEnteredEventDetails"]["name"] for e in events if e["type"].endswith("StateEntered")]

    def writers_today(self, day: str) -> list[tuple[str, str, dict]]:
        """(nombre, inicio ISO, salida) de las ejecuciones SUCCEEDED de hoy (Lima) que escribieron algo."""
        since, out, kwargs = lima_day_start(day), [], {"stateMachineArn": self.machine_arn,
                                                       "statusFilter": "SUCCEEDED", "maxResults": 100}
        while True:
            page = self.sfn.list_executions(**kwargs)
            for e in page["executions"]:
                if e["startDate"] < since:
                    continue
                d = self.describe(e["executionArn"])
                o = json.loads(d.get("output") or "{}")
                if not o.get("dry_run") and o.get("day") == day and (o.get("orders") or o.get("reorders")):
                    out.append((e["name"], iso(e["startDate"]), o))
            if not page.get("nextToken"):  # primero las que escribieron ordenes Y pedidos; luego la mas reciente
                return sorted(out, key=lambda x: (bool(x[2].get("orders") and x[2].get("reorders")), x[1]),
                              reverse=True)
            kwargs["nextToken"] = page["nextToken"]

    # ---- DynamoDB ----
    def _query(self, **kwargs) -> list[dict]:
        from planning import store
        return store.query_all(self.ddb, **kwargs)

    def orders_of(self, machine_id: str) -> list[dict]:
        from planning.store import vals
        return self._query(TableName="fp-registry", KeyConditionExpression="PK = :p",
                           ExpressionAttributeValues=vals(p=f"ORDER#{machine_id}"), ConsistentRead=True)

    def planning_reorders(self, day: str) -> list[dict]:
        from planning.store import vals
        return self._query(TableName="fp-registry", IndexName="GSI1", KeyConditionExpression="GSI1PK = :p",
                           FilterExpression="#d = :d AND #s = :s",
                           ExpressionAttributeNames={"#d": "day", "#s": "source"},
                           ExpressionAttributeValues=vals(p="REORDERS", d=day, s="planning"))

    def kpi_rows(self, day: str) -> int:
        from planning.store import vals
        return len(self._query(TableName="fp-registry", KeyConditionExpression="PK = :p",
                               ExpressionAttributeValues=vals(p=f"KPI#{day}"), ProjectionExpression="SK"))

    def outbox_orders(self, trace_id: str) -> list[str]:
        """aggregate_id de los maintenance.order.suggested de fp-outbox cuyo sobre trae este trace_id."""
        from planning.store import des, vals
        items, kwargs = [], {
            "TableName": "fp-outbox", "FilterExpression": "#t = :t AND contains(payload, :tr)",
            "ExpressionAttributeNames": {"#t": "type"},
            "ExpressionAttributeValues": vals(t="maintenance.order.suggested", tr=f'"trace_id":"{trace_id}"'),
            "ProjectionExpression": "aggregate_id"}
        while True:
            page = self.ddb.scan(**kwargs)
            items += [des(i)["aggregate_id"] for i in page.get("Items", [])]
            if "LastEvaluatedKey" not in page:
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def claim(self, day: str, key: str) -> dict | None:
        from planning.store import des
        item = self.ddb.get_item(TableName="fp-inbox", Key={"message_id": {"S": f"planning:{day}:{key}"}},
                                 ConsistentRead=True).get("Item")
        return des(item) if item else None

    def recompute(self, machine_id: str, today: date, horizon: int) -> dict | None:
        """La misma formula que la Lambda (planning/plan.py) con el META y las lecturas de fp-state de ahora."""
        from fplib import clock, ids
        from planning import handler, plan, store
        meta = self.ddb.get_item(TableName="fp-state", ConsistentRead=True,
                                 Key={"PK": {"S": f"MACHINE#{machine_id}"}, "SK": {"S": "META"}}).get("Item")
        if not meta:
            return None
        meta = store.des(meta)
        since = clock.iso(clock.now() - handler.READINGS_WINDOW)
        rate = plan.hours_per_day(meta, store.oldest_reading(self.ddb, machine_id, since))
        intervals = {m["model_id"]: m["service_interval_h"] for m in ids.load()["models"]}
        return plan.assess(meta, ids.machines().get(machine_id, {}), intervals.get(meta.get("model")),
                           ids.load()["bom"], rate, today, horizon)

    # ---- HTTP ----
    def api_get(self, path: str, query: str = "") -> tuple[int, dict]:
        return http_get(self.api + path + (f"?{query}" if query else ""), {"x-api-key": self.key})

    def console_get(self, path: str, query: str = "", token: str | None = None) -> tuple[int, dict]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return http_get(self.console + path + (f"?{query}" if query else ""), headers)

    def head_token(self) -> str:
        """ID token de jefe.mantenimiento (USER_PASSWORD_AUTH, sin firma). Nunca se imprime."""
        if os.environ.get("FP_TOKEN_HEAD"):
            return os.environ["FP_TOKEN_HEAD"]
        password = os.environ.get("FP_TEST_PASSWORD", "")
        if not password:
            raise Fail("falta FP_TEST_PASSWORD en el entorno (read -rs FP_TEST_PASSWORD && export FP_TEST_PASSWORD)")
        import boto3
        from botocore import UNSIGNED
        from botocore.config import Config
        idp = boto3.client("cognito-idp", region_name=REGION, config=Config(signature_version=UNSIGNED))
        r = idp.initiate_auth(AuthFlow="USER_PASSWORD_AUTH", ClientId=CLIENT_ID,
                              AuthParameters={"USERNAME": HEAD_USER, "PASSWORD": password})
        if "AuthenticationResult" not in r:
            raise Fail(f"Cognito pidio un challenge ({r.get('ChallengeName')}) para {HEAD_USER}: avisar al lider")
        return r["AuthenticationResult"]["IdToken"]

    # ---- BigQuery (solo lectura, load jobs: nunca streaming) ----
    def bq_orders(self, trace_id: str, since_iso: str) -> list[str]:
        if not NAME_RE.match(trace_id) or not re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", since_iso):
            raise Fail(f"trace_id/fecha con forma inesperada: {trace_id!r} {since_iso!r}")
        sql = ("SELECT aggregate_id FROM `fleetpulse-g19-2026.fleetpulse.domain_events` "
               f"WHERE occurred_at >= TIMESTAMP_SUB(TIMESTAMP('{since_iso}'), INTERVAL 10 MINUTE) "
               f"AND occurred_at < TIMESTAMP_ADD(TIMESTAMP('{since_iso}'), INTERVAL 1 HOUR) "
               f"AND event_type = 'maintenance.order.suggested' AND trace_id = '{trace_id}'")
        cmd = ["bq", f"--project_id={GCP_PROJECT}", "--format=json", "--quiet", "query", "--use_legacy_sql=false",
               "--maximum_bytes_billed=100000000", "--max_rows=1000", sql]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except FileNotFoundError as e:
            raise Fail("no esta el comando bq (Google Cloud SDK)") from e
        if p.returncode != 0:
            raise Fail(f"bq query fallo (rc={p.returncode}): {(p.stderr or p.stdout).strip()[-300:]}")
        return [r["aggregate_id"] for r in json.loads(p.stdout or "[]")]


def http_get(url: str, headers: dict | None = None, timeout: float = 30.0) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, TimeoutError) as e:
        raise Fail(f"GET {url.split('?')[0]}: sin respuesta ({type(e).__name__})") from e


# --- Historia -------------------------------------------------------------------------------------------------------

class Story:
    def __init__(self, lab, dry_run_only=False, rerun=True, out=print, sleep=time.sleep):
        self.lab, self.dry_only, self.rerun, self.out, self.sleep = lab, dry_run_only, rerun, out, sleep
        self.steps: list[tuple[str, str, float, str]] = []
        self.name = f"smoke-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
        self.started: list[str] = []       # ARNs de las ejecuciones de este smoke (para el finally)
        self.rule0: str | None = None
        self.plan: dict = {}               # salida del dry-run
        self.run1: dict = {}               # salida de la corrida real de este smoke
        self.writer: tuple[str, str, dict] | None = None  # (nombre, inicio, salida) de la ejecucion que escribio
        self.expected_orders: list[dict] = []
        self.expected_reorders: set[tuple[str, str]] = set()

    def step(self, name, fn):
        t0 = time.monotonic()
        try:
            detail, level = fn() or "", "OK"
        except Fail as e:
            detail, level = str(e), "FAIL"
        except Exception as e:  # respuesta con otra forma: FAIL y se sigue (KeyboardInterrupt no entra aqui)
            detail, level = f"{type(e).__name__}: {e}"[:300], "FAIL"
        if level == "OK" and isinstance(detail, tuple):
            detail, level = detail
        dt = time.monotonic() - t0
        self.steps.append((level, name, dt, detail))
        self.out(f"[{level:4}] {dt:5.1f}s  {name}: {detail}")
        return level != "FAIL"

    def _run(self, payload: dict, name: str) -> tuple[dict, str]:
        arn = self.lab.start(payload, name)
        self.started.append(arn)
        end, d = time.monotonic() + EXEC_TIMEOUT_S, {}
        while time.monotonic() < end:
            d = self.lab.describe(arn)
            if d["status"] != "RUNNING":
                break
            self.sleep(POLL_EVERY_S)
        status = d.get("status", "RUNNING")
        if status != "SUCCEEDED":
            raise Fail(f"ejecucion {name}: {status} error={d.get('error')} cause={str(d.get('cause'))[:150]} "
                       f"(historial: {arn}; logs /aws/lambda/{STACK}-plan y /aws/states/daily-planning)")
        visited = self.lab.visited(arn)
        if visited != STATES_OK:
            raise Fail(f"ejecucion {name} paso por {visited} (esperaba {STATES_OK}): fallo y se recupero o el ASL "
                       "desplegado no es el del repo")
        return json.loads(d.get("output") or "{}"), arn

    # ---- pasos ----
    def s1_rule(self):
        self.rule0 = self.lab.rule_state()
        if self.rule0 is None:
            raise Fail(f"el stack {STACK} no tiene la regla DailyPlanningSchedule")
        note = "" if self.rule0 == "DISABLED" else " (el deploy la deja DISABLED: alguien la encendio; no tocarla, avisar)"
        return f"regla diaria {self.rule0}{note}"

    def s2_dry_run(self):
        out, _ = self._run({"dry_run": True}, f"{self.name}-dry")
        self.plan = out
        if out.get("dry_run") is not True or not isinstance(out.get("due"), list) or "reorders" not in out:
            raise Fail(f"salida inesperada del dry-run: {json.dumps(out)[:200]}")
        if not out.get("machines"):
            raise Fail("machines=0: fp-state sin MACHINE#/META (falta la semilla: avisar al lider, no correrla)")
        if not out["due"]:
            raise Fail(f"machines={out['machines']} pero ninguna vence en {out.get('horizon_days')} dias: "
                       "no se puede probar el 'Listo cuando' (avisar al lider)")
        return (f"day={out['day']} horizon={out['horizon_days']} machines={out['machines']} due={len(out['due'])} "
                f"skipped_open={len(out.get('skipped_open') or [])} orders(simuladas)={len(out.get('orders') or [])} "
                f"reorders(simulados)={len(out['reorders'])}")

    def s3_formula(self):
        today, horizon = date.fromisoformat(self.plan["day"]), int(self.plan["horizon_days"])
        diffs = []
        for e in self.plan["due"]:
            local = self.lab.recompute(e["machine_id"], today, horizon) or {}
            if local.get("order_id") != e["order_id"] or local.get("due_at") != e["due_at"]:
                diffs.append(f"{e['machine_id']}: lambda {e['order_id']} vs local {local.get('order_id')}")
        if diffs:
            raise Fail(f"{len(diffs)}/{len(self.plan['due'])} distintas: " + "; ".join(diffs[:5]))
        return f"{len(self.plan['due'])} maquinas recalculadas con planning/plan.py: mismo order_id y due_at"

    def s4_run(self):
        out, _ = self._run({}, self.name)
        self.run1 = out
        if out.get("dry_run") is not False or out.get("day") != self.plan.get("day"):
            raise Fail(f"salida inesperada: {json.dumps(out)[:200]}")
        due = {e["machine_id"]: e for e in out.get("due") or []}
        real_ids = {e["order_id"] for e in due.values()}
        dry_ids = {e["order_id"] for e in self.plan["due"]}
        if real_ids != dry_ids:
            raise Fail(f"dry-run y corrida real planifican distinto: solo dry {sorted(dry_ids - real_ids)[:4]}, "
                       f"solo real {sorted(real_ids - dry_ids)[:4]}")
        accounted = set(out.get("orders") or []) | {due[m]["order_id"] for m in
                                                    (out.get("skipped_open") or []) + (out.get("already_planned") or [])}
        if accounted != real_ids:
            raise Fail(f"orders + skipped_open + already_planned no cubren due: faltan {sorted(real_ids - accounted)[:4]}")
        if out.get("orders") or out.get("reorders"):
            self.writer = (self.name, iso(datetime.now(timezone.utc) - timedelta(minutes=5)), out)
            origin = "esta corrida"
        else:
            prev = self.lab.writers_today(out["day"])
            if not prev:
                raise Fail("no escribio nada y ninguna ejecucion SUCCEEDED de hoy escribio: todo esta skipped_open "
                           f"(ordenes de dias anteriores, skipped_open={len(out.get('skipped_open') or [])}) o el "
                           "stock alcanza; no se puede probar el 'Listo cuando' hoy (avisar al lider)")
            self.writer = prev[0]
            origin = f"la ejecucion anterior de hoy {prev[0][0]} (esta corrida no escribio: ya planificado)"
        w = self.writer[2]
        written = set(w.get("orders") or [])
        self.expected_orders = [e for e in w.get("due") or [] if e["order_id"] in written]
        self.expected_reorders = {(r["part_id"], r["zone_id"]) for r in w.get("reorders") or []}
        if not self.expected_orders or not self.expected_reorders:
            raise Fail(f"la ejecucion que escribio ({self.writer[0]}) tiene orders={len(written)} "
                       f"reorders={len(self.expected_reorders)}: el 'Listo cuando' pide ordenes Y pedidos")
        return (f"{self.name} SUCCEEDED: orders={len(out.get('orders') or [])} "
                f"skipped_open={len(out.get('skipped_open') or [])} already_planned={len(out.get('already_planned') or [])} "
                f"reorders={len(out.get('reorders') or [])}; se verifica lo escrito por {origin}: "
                f"{len(self.expected_orders)} ordenes, {len(self.expected_reorders)} pedidos")

    def s5_outbox(self):
        got = sorted(self.lab.outbox_orders(self.writer[0]))
        want = sorted(e["order_id"] for e in self.expected_orders)
        if got != want:
            raise Fail(f"fp-outbox trace_id={self.writer[0]}: {len(got)} eventos, esperaba {len(want)}; "
                       f"faltan {sorted(set(want) - set(got))[:4]} sobran {sorted(set(got) - set(want))[:4]}")
        dry = self.lab.outbox_orders(f"{self.name}-dry")
        if dry:
            raise Fail(f"el dry-run escribio {len(dry)} eventos en fp-outbox: {dry[:4]}")
        return f"{len(got)} maintenance.order.suggested en fp-outbox con trace_id={self.writer[0]}; dry-run: 0"

    def s6_claims(self):
        day = self.writer[2]["day"]
        keys = [f"ORDER#{e['machine_id']}" for e in self.expected_orders]
        keys += [f"REORDER#{p}#{z}" for p, z in sorted(self.expected_reorders)]
        missing = [k for k in keys if (self.lab.claim(day, k) or {}).get("consumer") != "planning"]
        if missing:
            raise Fail(f"faltan reclamos planning:{day}: {missing[:5]}")
        return f"{len(keys)} reclamos planning:{day}:... en fp-inbox (consumer=planning)"

    def s7_orders(self):
        deadline = time.monotonic() + REGISTRY_POLL_S
        while True:
            missing, bad = [], []
            for e in self.expected_orders:
                rows = [r for r in self.lab.orders_of(e["machine_id"]) if r.get("order_id") == e["order_id"]]
                if not rows:
                    missing.append(e["order_id"])
                    continue
                r = rows[0]
                if r.get("status") != "suggested" or r.get("kind") != "preventive" or r.get("SK") != e["due_at"] \
                        or r.get("GSI1PK") != "ORDERS" or not r.get("reason") or "hours_since_service" not in r:
                    bad.append(f"{e['order_id']}: status={r.get('status')} kind={r.get('kind')} SK={r.get('SK')}")
            if not missing or time.monotonic() > deadline:
                break
            self.sleep(POLL_EVERY_S)
        if missing:
            raise Fail(f"{len(missing)} ORDER# ausentes tras {REGISTRY_POLL_S}s: {missing[:5]} (registry-q, "
                       "registry-dlq, log /aws/lambda/fp-registry-dev-registry)")
        if bad:
            raise Fail("ORDER# con otra forma: " + "; ".join(bad[:3]))
        return f"{len(self.expected_orders)} ORDER#<maquina>/<due_at> en fp-registry, status=suggested kind=preventive"

    def s8_reorders(self):
        rows = self.lab.planning_reorders(self.writer[2]["day"])
        keys = [(r["part_id"], r["zone_id"]) for r in rows]
        dup = sorted({k for k in keys if keys.count(k) > 1})
        if dup:
            raise Fail(f"REORDER# de planning duplicados hoy (fallo la idempotencia): {dup[:5]}")
        if not self.expected_reorders <= set(keys):
            raise Fail(f"REORDER# de hoy con source=planning: faltan {sorted(self.expected_reorders - set(keys))[:5]}")
        extra = sorted(set(keys) - self.expected_reorders)  # de otra ejecucion de hoy que tambien escribio
        bad = [f"{r['part_id']}#{r['zone_id']}" for r in rows
               if int(r["qty"]) != int(r["reorder_point"]) - (int(r["available"]) + int(r["on_order"])
                                                              - int(r["forecast_14d"]))
               or int(r["qty"]) <= 0 or r.get("GSI1PK") != "REORDERS" or r.get("SK") != r.get("created_at")]
        if bad:
            raise Fail(f"REORDER# inconsistentes (qty != reorder_point - (available + on_order - forecast_14d)): {bad}")
        return (f"{len(rows)} REORDER# de hoy con source=planning, sin duplicados, qty consistente: {sorted(keys)[:4]}"
                + (f"; {len(extra)} de otra ejecucion de hoy: {extra[:3]}" if extra else ""))

    def _planning_keys(self, rows: list[dict]) -> set[tuple[str, str]]:
        day = self.writer[2]["day"]
        return {(x["part_id"], x["zone_id"]) for x in rows if x.get("source") == "planning" and x.get("day") == day}

    def s9_api(self):
        order_ids, reorder_keys = set(), set()
        for t in self.lab.tenants():
            st, o = self.lab.api_get("/orders", f"tenant={t}")
            st2, r = self.lab.api_get("/reorders", f"tenant={t}")
            if st != 200 or st2 != 200:
                raise Fail(f"API registry tenant={t}: /orders {st} /reorders {st2}")
            order_ids |= {x.get("order_id") for x in o.get("orders") or []}
            reorder_keys |= self._planning_keys(r.get("reorders") or [])
        missing = [e["order_id"] for e in self.expected_orders if e["order_id"] not in order_ids]
        if missing:
            raise Fail(f"GET /orders (todos los parques) no lista {len(missing)}: {missing[:5]} (limite 50 por parque)")
        if not self.expected_reorders <= reorder_keys:
            raise Fail(f"GET /reorders no lista {sorted(self.expected_reorders - reorder_keys)[:5]} (limite 50)")
        return (f"GET /orders lista las {len(self.expected_orders)} ordenes en {len(self.lab.tenants())} parques; "
                f"GET /reorders lista los {len(self.expected_reorders)} pedidos de planning de hoy")

    def s10_console(self):
        mine = {e["order_id"] for e in self.expected_orders if self.lab.tenant_of(e["machine_id"]) == "mineria"}
        others = {e["order_id"] for e in self.expected_orders} - mine
        st, o = self.lab.console_get("/api/orders")
        st2, r = self.lab.console_get("/api/reorders")
        if st != 200 or st2 != 200:
            raise Fail(f"consola sin token: /api/orders {st} /api/reorders {st2}")
        ids_ = {x.get("order_id") for x in o.get("orders") or []}
        if not mine <= ids_:
            raise Fail(f"/api/orders sin token (parque mineria) no lista {sorted(mine - ids_)[:5]}")
        if ids_ & others:
            raise Fail(f"/api/orders sin token muestra ordenes de otros parques: {sorted(ids_ & others)[:5]}")
        mine_re = {k for k in self.expected_reorders if self.lab.tenant_of_zone(k[1]) == "mineria"}
        got_mine = self._planning_keys(r.get("reorders") or [])
        if not mine_re <= got_mine:
            raise Fail(f"/api/reorders sin token (mineria) no lista {sorted(mine_re - got_mine)[:5]}")
        token = self.lab.head_token()
        everyone = ",".join(self.lab.tenants())
        st3, o2 = self.lab.console_get("/api/orders", f"tenant={everyone}", token)
        st4, r2 = self.lab.console_get("/api/reorders", f"tenant={everyone}", token)
        if st3 != 200 or st4 != 200:
            raise Fail(f"consola como jefe.mantenimiento ?tenant={everyone}: /api/orders {st3} /api/reorders {st4}")
        all_ids = {x.get("order_id") for x in o2.get("orders") or []}
        want = mine | others
        if not want <= all_ids:
            raise Fail(f"/api/orders como jefe no lista {sorted(want - all_ids)[:5]}")
        got_re = self._planning_keys(r2.get("reorders") or [])
        if not self.expected_reorders <= got_re:
            raise Fail(f"/api/reorders como jefe no lista {sorted(self.expected_reorders - got_re)[:5]}")
        return (f"sin token: {len(mine)} ordenes y {len(mine_re)} pedidos de mineria, nada de otro parque; "
                f"como jefe.mantenimiento: "
                f"las {len(want)} ordenes y los {len(self.expected_reorders)} pedidos de planning")

    def s11_kpis(self):
        today = date.fromisoformat(self.writer[2]["day"])
        counts = {(today - timedelta(days=b)).isoformat(): self.lab.kpi_rows((today - timedelta(days=b)).isoformat())
                  for b in (1, 2, 3)}
        st, k = self.lab.console_get("/api/kpis")
        if st != 200 or "day" not in k or not isinstance(k.get("machines"), list):
            raise Fail(f"/api/kpis: HTTP {st} {json.dumps(k)[:150]}")
        if not any(counts.values()) and (k["day"] is not None or k["machines"]):
            raise Fail(f"fp-registry sin KPI# {counts} pero /api/kpis devuelve day={k['day']}")
        if any(counts.values()) and k["day"] not in counts:
            raise Fail(f"fp-registry tiene KPI# {counts} pero /api/kpis devuelve day={k['day']}")
        return (f"KPI# en fp-registry {counts}; /api/kpis day={k['day']} machines={len(k['machines'])} "
                "(T18 incremento 1 no escribe KPI#: /kpis/daily es un estado posterior)")

    def s12_bigquery(self):
        name, since, _ = self.writer
        want = {e["order_id"] for e in self.expected_orders}
        deadline, got = time.monotonic() + BQ_POLL_S, []
        while True:
            got = self.lab.bq_orders(name, since)
            if want <= set(got) or time.monotonic() > deadline:
                break
            self.sleep(BQ_EVERY_S)
        if not want <= set(got):
            raise Fail(f"domain_events trace_id={name}: {len(set(got))}/{len(want)} tras {BQ_POLL_S}s; faltan "
                       f"{sorted(want - set(got))[:4]} (gcp-events-q, gcp-events-dlq, log "
                       "/aws/lambda/fp-gcp-exporter-dev-gcpEvents)")
        dup = len(got) - len(set(got))
        return (f"{len(set(got))} maintenance.order.suggested en BigQuery domain_events con trace_id={name}"
                + (f" ({dup} filas repetidas: ventana de duplicados tolerada de gcpEvents)" if dup else ""))

    def s13_rerun(self):
        if not self.rerun:
            return ("sin segunda corrida (--no-rerun)", "WARN")
        n_before = len(self.lab.planning_reorders(self.writer[2]["day"]))
        out, _ = self._run({}, f"{self.name}-again")
        if out.get("orders") or out.get("reorders"):
            raise Fail(f"la segunda corrida escribio: orders={out.get('orders')} reorders={out.get('reorders')}")
        covered = set(out.get("skipped_open") or []) | set(out.get("already_planned") or [])
        expected = {e["machine_id"] for e in self.expected_orders}
        if not expected <= covered:
            raise Fail(f"maquinas con orden de hoy que no salen en skipped_open/already_planned: "
                       f"{sorted(expected - covered)[:5]}")
        n_after = len(self.lab.planning_reorders(self.writer[2]["day"]))
        if n_after != n_before:
            raise Fail(f"REORDER# de planning de hoy paso de {n_before} a {n_after}")
        return (f"{self.name}-again SUCCEEDED sin escribir: skipped_open={len(out.get('skipped_open') or [])} "
                f"already_planned={len(out.get('already_planned') or [])} "
                f"reorders_already_planned={len(out.get('reorders_already_planned') or [])}; REORDER# sigue en {n_after}")

    def s14_rule(self):
        st = self.lab.rule_state()
        if st != self.rule0:
            raise Fail(f"la regla diaria paso de {self.rule0} a {st} durante el smoke (el smoke no la toca: avisar)")
        return f"regla diaria sigue {st}"

    def _cleanup(self):
        """Detiene las ejecuciones de ESTE smoke que sigan RUNNING (timeout o Ctrl-C). No borra datos."""
        for arn in self.started:
            try:
                if self.lab.describe(arn)["status"] == "RUNNING":
                    self.lab.stop(arn)
                    self.out(f"[LIMPIEZA] detenida {arn}")
            except Exception as e:  # noqa: BLE001 - la limpieza no debe tapar el resultado
                self.out(f"[LIMPIEZA] NO se pudo revisar/detener {arn}: {type(e).__name__}: {e} -> detenerla a mano")

    def run(self) -> int:
        try:
            self.step("1 regla diaria", self.s1_rule)
            if self.step("2 dry-run", self.s2_dry_run):
                self.step("3 formula local", self.s3_formula)
                if not self.dry_only and self.step("4 corrida real", self.s4_run):
                    self.step("5 eventos en fp-outbox", self.s5_outbox)
                    self.step("6 reclamos en fp-inbox", self.s6_claims)
                    self.step("7 ORDER# en fp-registry", self.s7_orders)
                    self.step("8 REORDER# en fp-registry", self.s8_reorders)
                    self.step("9 API de registry", self.s9_api)
                    self.step("10 consola", self.s10_console)
                    self.step("11 KPI#", self.s11_kpis)
                    self.step("12 BigQuery domain_events", self.s12_bigquery)
                    self.step("13 segunda corrida", self.s13_rerun)
            self.step("14 regla diaria al final", self.s14_rule)
        finally:
            self._cleanup()
            names = [a.rsplit(":", 1)[-1] for a in self.started]
            self.out(f"ejecuciones de este smoke: {names or 'ninguna'}")
            if self.writer:
                self.out(f"ejecucion que escribio lo verificado: {self.writer[0]} (inicio ~{self.writer[1]})")
        fails = [s for s in self.steps if s[0] == "FAIL"]
        warns = [s for s in self.steps if s[0] == "WARN"]
        expected = 4 if self.dry_only else 14
        self.out(f"\n{len(self.steps) - len(fails) - len(warns)}/{expected} OK, {len(warns)} WARN, {len(fails)} FAIL; "
                 f"total {sum(s[2] for s in self.steps):.1f}s")
        if fails:
            return 1
        return 2 if warns or len(self.steps) != expected else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run-only", action="store_true", help="solo pasos 1-3 y 14: no escribe")
    ap.add_argument("--no-rerun", action="store_true", help="sin la segunda corrida (WARN: no es verde)")
    ap.add_argument("--console", default=CONSOLE, help="URL de la consola (BFF /api/*)")
    a = ap.parse_args(argv)
    return Story(Lab(console=a.console), dry_run_only=a.dry_run_only, rerun=not a.no_rerun).run()


if __name__ == "__main__":
    sys.exit(main())
