#!/usr/bin/env python3
"""Smoke de la analitica de insights (T20 y T42) contra Cloud Run, con el tiempo de cada llamada.

    scripts/smoke_analytics.py   # lecturas baratas: health, /supply/analytics (x4), /anomalies/fuel y /forecast/parts;
                                 # BigQuery factura minimo 10 MB por consulta: ~100 MB por corrida
    --job-ids ID1,ID2   ademas GET /jobs/status de esos jobs (gratis: no corre consultas)
    --kpis              ademas GET /kpis/daily (m_peer_clusters sobre v_machine_profile_30d: ~280 MB por llamada)
    --refresh           ademas POST /jobs/refresh y espera /jobs/status (reentrena los 3 modelos: ~300 MB)
    --strict-forecast   FAIL (no WARN) si /forecast/parts no trae repuestos

Token: el ID token del usuario de gcloud (`gcloud auth print-identity-token`), que Cloud Run acepta si el usuario tiene
run.routes.invoke. Con --impersonate SA usa `--impersonate-service-account` (solo con TokenCreator sobre la SA). El
token vive en memoria y nunca se imprime. Owner: Benjamin (T20).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from statistics import median

BASE = "https://insights-qgbfzxdq2a-ue.a.run.app"
PROJECT = "fleetpulse-g19-2026"  # GCP_PROJECT_ID de insights; insights-00008 tenia "yes" y todo daba 503
SUPPLY_SLO_S = 3.0  # backlog#39: /supply/analytics?tenant=mineria en < 3 s
# contracts/kpi.daily.schema.json (payload.machines[])
QUADRANTS = {"star", "workhorse", "underused", "inefficient"}
KPI_REQUIRED = {"machine_id", "operating_h", "idle_ratio", "utilization", "fuel_lph", "cost_per_h", "quadrant"}
MID_RE = re.compile(r"^(EX|CF|VQ|MN|TO|TA|CS|MC|CP|GE)-[0-9]{3}$")
POLL_S, POLL_EVERY_S = 300, 10


class Fail(Exception):
    pass


def gcloud_token(base: str, impersonate: str | None = None) -> str:
    cmd = ["gcloud", "auth", "print-identity-token"]
    if impersonate:
        cmd += [f"--impersonate-service-account={impersonate}", f"--audiences={base}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not r.stdout.strip():
        raise SystemExit("no se pudo obtener el ID token con gcloud (gcloud auth login?)")
    return r.stdout.strip()


def http_caller(base: str, token: str | None, timeout: float = 60.0):
    """call(method, path, auth=True) -> (status, json, segundos)."""
    def call(method, path, auth=True):
        headers = {"content-type": "application/json"}
        if auth and token:
            headers["authorization"] = f"Bearer {token}"
        req = urllib.request.Request(base.rstrip("/") + path, data=b"{}" if method == "POST" else None,
                                     method=method, headers=headers)
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw, status = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        dt = time.monotonic() - t0
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {"raw": raw[:200].decode(errors="replace")}
        return status, body, dt
    return call


class Smoke:
    def __init__(self, call, job_ids=(), kpis=False, refresh=False, strict_forecast=False, out=print,
                 sleep=time.sleep):
        self.call, self.out, self.sleep = call, out, sleep
        self.job_ids, self.kpis, self.refresh, self.strict_forecast = list(job_ids), kpis, refresh, strict_forecast
        self.steps: list[tuple[str, str, float, str]] = []

    def get(self, path, expect=200, auth=True):
        status, body, dt = self.call("GET", path, auth)
        if status != expect:
            raise Fail(f"GET {path}: HTTP {status} {body.get('detail', '')} (esperado {expect})")
        return body, dt

    def step(self, name, fn):
        t0 = time.monotonic()
        try:
            detail, level = fn() or "", "OK"
            if isinstance(detail, tuple):
                detail, level = detail
        except Fail as e:
            detail, level = str(e), "FAIL"
        except Exception as e:  # noqa: BLE001 - un KeyError no debe abortar el resto de la corrida
            detail, level = f"{type(e).__name__}: {e}", "FAIL"
        dt = time.monotonic() - t0
        self.steps.append((level, name, dt, detail))
        self.out(f"[{level:4}] {dt:5.1f}s  {name}: {detail}")
        return level != "FAIL"

    # ---- pasos ----
    def s_private(self):
        status, _, _ = self.call("GET", "/health", False)
        if status not in (401, 403):
            raise Fail(f"/health sin token -> HTTP {status} (esperado 403: el servicio debe ser privado)")
        return f"sin token -> {status}"

    def s_health(self):
        body, dt = self.get("/health")
        if body.get("status") != "ok":
            raise Fail(f"/health -> {body}")
        if body.get("project") != PROJECT:
            raise Fail(f"/health project={body.get('project')!r} (esperado {PROJECT!r}: revisar GCP_PROJECT_ID)")
        return f"ok ({body.get('project')}) en {dt:.2f}s (puede incluir arranque en frio)"

    def s_supply(self):
        # 3 llamadas en caliente; el SLO se mide con la mediana (una sola llamada puede caer en un arranque en frio)
        times = []
        for _ in range(3):
            body, dt = self.get("/supply/analytics?tenant=mineria&days=14")
            times.append(dt)
        dt = median(times)
        o = body["orders"]
        if body["tenant"] != "mineria" or body["days"] != 14:
            raise Fail(f"tenant/days = {body['tenant']}/{body['days']}")
        if set(o) != {"total", "by_status", "in_flight", "received", "downtime_avoided_usd", "cost_usd"}:
            raise Fail(f"orders con claves {sorted(o)}")
        if o["total"] != sum(o["by_status"].values()):
            raise Fail(f"total {o['total']} != suma de by_status {o['by_status']}")
        for s in body["stockouts_daily"]:
            if set(s) != {"day", "part_id", "zone_id", "shortages", "resolved", "avg_resolution_hours"}:
                raise Fail(f"stockouts_daily con claves {sorted(s)}")
        if dt >= SUPPLY_SLO_S:
            raise Fail(f"mediana {dt:.2f}s >= {SUPPLY_SLO_S}s (tiempos {[round(t, 2) for t in times]})")
        if o["total"] < 1 or o["downtime_avoided_usd"] <= 0:
            return (f"{dt:.2f}s pero sin ordenes con parada evitada: {o}", "WARN")
        return (f"mediana {dt:.2f}s; {o['total']} ordenes {o['by_status']}, parada evitada USD {o['downtime_avoided_usd']}, "
                f"{len(body['stockouts_daily'])} filas de quiebres")

    def s_supply_all_and_bad(self):
        body, _ = self.get("/supply/analytics")
        if body["tenant"] != "all" or body["days"] != 14:
            raise Fail(f"sin tenant -> tenant={body['tenant']} days={body['days']} (esperado all/14)")
        self.get("/supply/analytics?tenant=marte", expect=400)
        self.get("/supply/analytics?days=91", expect=422)
        return f"sin tenant -> all ({body['orders']['total']} ordenes); tenant=marte -> 400; days=91 -> 422"

    def s_anomalies(self):
        body, dt = self.get("/anomalies/fuel?days=7")
        if body["threshold"] != 0.95 or body["days"] != 7 or not isinstance(body["anomalies"], list):
            raise Fail(f"threshold/days/anomalies = {body.get('threshold')}/{body.get('days')}/{type(body.get('anomalies'))}")
        for a in body["anomalies"]:
            lo, hi = a["expected"]
            if not (a["anomaly_probability"] >= 0.95 and lo <= hi):
                raise Fail(f"anomalia invalida {a}")
        return f"{dt:.2f}s; dia {body['day']}, {len(body['anomalies'])} anomalias"

    def s_forecast(self):
        body, dt = self.get("/forecast/parts")
        if body["horizon_days"] != 14:
            raise Fail(f"horizon_days = {body['horizon_days']}")
        for p in body["parts"]:
            if not (isinstance(p["demand_14d"], int) and p["demand_14d_upper"] >= p["demand_14d"] >= 0):
                raise Fail(f"fila invalida {p}")
        if not body["parts"]:
            msg = "0 repuestos (m_parts_demand sin series?)"
            if self.strict_forecast:
                raise Fail(msg)
            return (msg, "WARN")
        return f"{dt:.2f}s; {len(body['parts'])} series: " + ", ".join(
            f"{p['part_id']}/{p['zone_id']}={p['demand_14d']}" for p in body["parts"][:5])

    def s_kpis(self):
        body, dt = self.get("/kpis/daily")
        ms = body["machines"]
        if not 1 <= len(ms) <= 100:
            raise Fail(f"{len(ms)} maquinas")
        bad = [m.get("machine_id") for m in ms if not KPI_REQUIRED <= set(m) or not MID_RE.match(m["machine_id"])
               or m["quadrant"] not in QUADRANTS or not 0 <= m["operating_h"] <= 24 or not 0 <= m["utilization"] <= 1
               or not 0 <= m["idle_ratio"] <= 1 or m["fuel_lph"] < 0 or m["cost_per_h"] < 0]
        if bad:
            raise Fail(f"fuera de contrato: {bad[:5]}")
        with_cluster = sum(1 for m in ms if "cluster_id" in m)
        if not with_cluster:
            return (f"{dt:.2f}s; {len(ms)} maquinas del {body['day']} pero ninguna con cluster_id", "WARN")
        return f"{dt:.2f}s; {len(ms)} maquinas del {body['day']}, {with_cluster} con cluster_id"

    def _status(self, ids):
        body, _ = self.get("/jobs/status?ids=" + ",".join(ids))
        if [j["job_id"] for j in body["jobs"]] != list(ids):
            raise Fail(f"jobs distintos a los pedidos: {body['jobs']}")
        return body

    def s_status(self):
        body = self._status(self.job_ids)
        if body["failed"]:
            raise Fail(f"jobs fallidos {body['failed']}: {[j['error'] for j in body['jobs'] if j.get('error')]}")
        self.get("/jobs/status?ids=bad%20id", expect=400)
        return f"done={body['done']}, " + ", ".join(f"{j['job_id']}={j['state']}" for j in body["jobs"])

    def s_refresh(self):
        status, body, _ = self.call("POST", "/jobs/refresh", True)
        if status != 200:
            raise Fail(f"POST /jobs/refresh: HTTP {status} {body.get('detail', '')}")
        if body["errors"]:
            raise Fail(f"errores al lanzar: {body['errors']}")
        ids = [j["job_id"] for j in body["jobs"]]
        if sorted(j["model"] for j in body["jobs"]) != ["m_fuel_trend", "m_parts_demand", "m_peer_clusters"]:
            raise Fail(f"modelos lanzados: {body['jobs']}")
        deadline = time.monotonic() + POLL_S
        while True:
            st = self._status(ids)
            if st["done"]:
                break
            if time.monotonic() > deadline:
                raise Fail(f"tras {POLL_S}s: " + ", ".join(f"{j['job_id']}={j['state']}" for j in st["jobs"]))
            self.sleep(POLL_EVERY_S)
        if st["failed"]:
            raise Fail(f"fallaron {st['failed']}: {[j['error'] for j in st['jobs'] if j.get('error')]}")
        return "3 modelos reentrenados: " + ", ".join(f"{j['model']}={j['job_id']}" for j in body["jobs"])

    def run(self) -> int:
        self.step("1 privado (sin token)", self.s_private)
        if self.step("2 health con token", self.s_health):
            self.step("3 /supply/analytics?tenant=mineria", self.s_supply)
            self.step("4 /supply/analytics sin tenant y errores", self.s_supply_all_and_bad)
            self.step("5 /anomalies/fuel", self.s_anomalies)
            self.step("6 /forecast/parts", self.s_forecast)
            if self.job_ids:
                self.step("7 /jobs/status", self.s_status)
            if self.kpis:
                self.step("8 /kpis/daily", self.s_kpis)
            if self.refresh:
                self.step("9 /jobs/refresh + /jobs/status", self.s_refresh)
        fails = [s for s in self.steps if s[0] == "FAIL"]
        self.out(f"\n{len(self.steps) - len(fails)}/{len(self.steps)} pasos sin FAIL; "
                 f"total {sum(s[2] for s in self.steps):.1f}s")
        return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--impersonate", help="service account a impersonar (requiere TokenCreator)")
    ap.add_argument("--job-ids", default="")
    ap.add_argument("--kpis", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--strict-forecast", action="store_true")
    a = ap.parse_args()
    token = gcloud_token(a.base, a.impersonate)
    ids = [j for j in a.job_ids.split(",") if j]
    return Smoke(http_caller(a.base, token), ids, a.kpis, a.refresh, a.strict_forecast).run()


if __name__ == "__main__":
    sys.exit(main())
