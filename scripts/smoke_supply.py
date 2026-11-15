#!/usr/bin/env python3
"""Smoke del modulo de abastecimiento (T43): la historia de la demo (diseno, seccion 2) por la consola (BFF /api/supply/*),
con el tiempo de cada paso para el guion.

    scripts/smoke_supply.py --mock                          # contra la consola local con MOCK=1 (uvicorn :8080)
    scripts/smoke_supply.py --base https://console-...run.app # contra Cloud Run con los ID tokens de abajo
    --read-only     solo lee (no aprueba ni recibe): para mirar antes de grabar

Modo real: no usa contrasenas ni credenciales de AWS. Toma el ID token de cada rol de variables de entorno (se copian
de la consola: sessionStorage `fp.session` -> `token`, una pestana por usuario):
    FP_TOKEN_HEAD (jefe.mantenimiento), FP_TOKEN_ADMIN (admin de mineria), FP_TOKEN_ADMIN_AGRO (admin.agro)
Los tokens no se imprimen nunca.

Recorrido: 1) PRT-04 en quiebre en mina y con stock en agro; 2) sugerencia de transferencia con opciones y parada
evitada; 3) faltante en la linea de tiempo del incidente; 4) el jefe aprueba -> in_transit; 5) el admin de agro ve solo
su parque y la salida, y no puede recibir; 6) el admin de mina confirma -> received y la bomba queda reservada;
7) KPIs. El cruce con BigQuery (`v_supply_orders`) se imprime como comando para correrlo a mano. Owner: Alex (T43).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

PART, DEST_PARK, DONOR_PARK = "PRT-04", "mineria", "agro"
POLL_S, POLL_EVERY_S = 60, 3  # supply-flow es asincrono en AWS: el estado puede tardar unos segundos


class Fail(Exception):
    pass


def http_caller(base: str, timeout: float = 15.0):
    """call(method, path, token, body) -> (status, json) contra la consola."""
    def call(method, path, token, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                                     headers={"content-type": "application/json", "authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
    return call


class Story:
    def __init__(self, call, tokens: dict, read_only=False, out=print, sleep=time.sleep):
        self.call, self.t, self.read_only, self.out, self.sleep = call, tokens, read_only, out, sleep
        self.steps: list[tuple[str, str, float, str]] = []
        self.trf = None

    def get(self, who, path, expect=200):
        status, body = self.call("GET", path, self.t[who])
        if status != expect:
            raise Fail(f"GET {path} como {who}: HTTP {status} {body.get('error', '')}")
        return body

    def post(self, who, path):
        return self.call("POST", path, self.t[who], {})

    def step(self, name, fn):
        t0 = time.monotonic()
        try:
            detail, level = fn() or "", "OK"
        except Fail as e:
            detail, level = str(e), "FAIL"
        dt = time.monotonic() - t0
        if level == "OK" and isinstance(detail, tuple):
            detail, level = detail
        self.steps.append((level, name, dt, detail))
        self.out(f"[{level:4}] {dt:5.1f}s  {name}: {detail}")
        return level != "FAIL"

    def _wait_status(self, who, want):
        deadline = time.monotonic() + POLL_S
        while True:
            d = self.get(who, f"/api/supply/transfers/{self.trf['id']}")
            if d["status"] == want:
                return d
            if time.monotonic() > deadline:
                raise Fail(f"{self.trf['id']} sigue en {d['status']} (esperaba {want})")
            self.sleep(POLL_EVERY_S)

    def _incident_reserved(self, incident_id) -> int:
        if not incident_id:
            return 0
        status, inc = self.call("GET", f"/api/incidents/{incident_id}", self.t["admin"])
        events = inc.get("timeline") or [] if status == 200 else []
        return sum(1 for e in events if (e.get("type") or e.get("event_type")) == "part.reserved")

    def _wait_reserved(self, d, before: int) -> bool:
        """AllocateBackorders (T39) agrega `part.reserved` a la linea de tiempo del incidente; el mock, a la del documento."""
        deadline = time.monotonic() + POLL_S
        while True:
            if "part.reserved" in [e.get("type") for e in d.get("timeline", [])]:
                return True
            if self._incident_reserved(d.get("incident_id")) > before:
                return True
            if time.monotonic() > deadline:
                return False
            self.sleep(POLL_EVERY_S)
            d = self.get("admin", f"/api/supply/transfers/{d['id']}")

    # ---- pasos ----
    def s1_stock(self):
        rows = self.get("head", f"/api/supply/stock?part={PART}")["stock"]
        mine = [r for r in rows if r["tenant_id"] == DEST_PARK]
        donor = [r for r in rows if r["tenant_id"] == DONOR_PARK and r["available"] > r.get("safety_stock", 0)]
        if not mine or any(r["available"] > 0 for r in mine):
            raise Fail(f"{PART} no esta en quiebre en {DEST_PARK} (correr seed-registry.py --demo-supply)")
        if not donor:
            raise Fail(f"{DONOR_PARK} no tiene {PART} sobre su stock de seguridad")
        return f"{PART}: 0 en {', '.join(r['zone_id'] for r in mine)}; {DONOR_PARK} {donor[0]['zone_id']}={donor[0]['available']}"

    def s2_suggestion(self):
        sug = self.get("head", "/api/supply/suggestions")["suggestions"]
        trf = next((s for s in sug if s["kind"] == "transfer" and s["part_id"] == PART), None)
        if not trf:
            raise Fail(f"no hay transferencia sugerida de {PART} (supply-sourcing no corrio?)")
        self.trf = trf
        opts = " vs ".join(f"{o['source_kind']} {o['eta_h']} h USD {o['cost_usd']}" for o in trf.get("options", []))
        if len(trf.get("options", [])) < 2:
            return (f"{trf['id']}: solo {len(trf.get('options', []))} opcion", "WARN")
        return f"{trf['id']} ({opts}); evita {trf['downtime_avoided_h']} h = USD {trf['downtime_avoided_usd']}"

    def s3_incident(self):
        inc = self.trf.get("incident_id")
        if not inc:
            return ("la sugerencia no trae incident_id", "WARN")
        status, d = self.call("GET", f"/api/incidents/{inc}", self.t["head"])
        types = [e.get("type") or e.get("event_type") for e in (d.get("timeline") or [])] if status == 200 else []
        if "part.shortage" not in types:
            return (f"{inc}: sin part.shortage en la linea de tiempo (HTTP {status})", "WARN")
        return f"{inc}: part.shortage en la linea de tiempo"

    def s4_approve(self):
        if self.read_only:
            return ("--read-only: no aprueba", "WARN")
        status, body = self.post("head", f"/api/supply/transfers/{self.trf['id']}/approve")
        if status != 200:
            raise Fail(f"aprobar: HTTP {status} {body.get('error', '')}")
        d = self._wait_status("head", "in_transit")
        dest = next(r for r in self.get("head", f"/api/supply/stock?part={PART}")["stock"] if r["zone_id"] == d["zone_id"])
        if dest.get("in_transit", 0) < d["qty"]:
            raise Fail(f"{d['zone_id']} no muestra in_transit")
        return f"in_transit; {d['zone_id']} in_transit={dest['in_transit']}"

    def s5_agro(self):
        rows = self.get("admin_agro", "/api/supply/stock")["stock"]
        parks = {r["tenant_id"] for r in rows}
        if parks != {DONOR_PARK}:
            raise Fail(f"el admin de agro ve {sorted(parks)}")
        self.get("admin_agro", f"/api/supply/stock?tenant={DEST_PARK}", expect=403)
        ids = [t["id"] for t in self.get("admin_agro", "/api/supply/transfers")["transfers"]]
        if self.trf["id"] not in ids:
            raise Fail("el admin de agro no ve la transferencia saliente")
        if not self.read_only:
            status, _ = self.post("admin_agro", f"/api/supply/transfers/{self.trf['id']}/receive")
            if status not in (403, 404):
                raise Fail(f"el admin de agro pudo recibir (HTTP {status})")
        return f"solo {DONOR_PARK}; ve la salida; ?tenant={DEST_PARK} -> 403"

    def s6_receive(self):
        if self.read_only:
            return ("--read-only: no recibe", "WARN")
        before = self._incident_reserved(self.trf.get("incident_id"))
        status, body = self.post("admin", f"/api/supply/transfers/{self.trf['id']}/receive")
        if status != 200:
            raise Fail(f"recibir: HTTP {status} {body.get('error', '')}")
        d = self._wait_status("admin", "received")
        reserved = self._wait_reserved(d, before)
        dest = next(r for r in self.get("admin", f"/api/supply/stock?part={PART}")["stock"] if r["zone_id"] == d["zone_id"])
        if dest["reserved"] < d["qty"] or not reserved:
            return (f"received, pero sin reserva para {d.get('incident_id')} (reserved={dest['reserved']})", "WARN")
        return f"received; {d['zone_id']} reservada para {d.get('incident_id')}"

    def s7_kpis(self):
        k = self.get("head", "/api/supply/kpis")
        if not self.read_only and not k.get("downtime_avoided_usd"):
            return ("downtime_avoided_usd = 0", "WARN")
        return f"parada evitada {k.get('downtime_avoided_h')} h / USD {k.get('downtime_avoided_usd')}; quiebres {k.get('open_stockouts')}"

    def run(self) -> int:
        ok = self.step("1 stock PRT-04 por parque", self.s1_stock)
        ok = ok and self.step("2 sugerencia de transferencia", self.s2_suggestion)
        if ok:
            self.step("3 faltante en el incidente", self.s3_incident)
            ok = self.step("4 jefe aprueba", self.s4_approve)
            self.step("5 admin de agro", self.s5_agro)
            ok = ok and self.step("6 admin de mina recibe", self.s6_receive)
            self.step("7 KPIs", self.s7_kpis)
        if self.trf and self.trf.get("incident_id"):
            self.out("\nBigQuery (a mano): bq query --use_legacy_sql=false --maximum_bytes_billed=100000000 "
                     f"\"SELECT * FROM fleetpulse.v_supply_orders WHERE incident_id = '{self.trf['incident_id']}'\"")
        fails = [s for s in self.steps if s[0] == "FAIL"]
        self.out(f"\n{len(self.steps) - len(fails)}/{len(self.steps)} pasos sin FAIL; "
                 f"total {sum(s[2] for s in self.steps):.1f}s")
        return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--mock", action="store_true", help="usuarios de prueba mock.<rol> (consola con MOCK=1)")
    ap.add_argument("--read-only", action="store_true")
    a = ap.parse_args()
    if a.mock:
        tokens = {"head": "mock.maintenance_head", "admin": "mock.admin", "admin_agro": "mock.admin_agro"}
    else:
        env = {"head": "FP_TOKEN_HEAD", "admin": "FP_TOKEN_ADMIN", "admin_agro": "FP_TOKEN_ADMIN_AGRO"}
        missing = [v for v in env.values() if not os.environ.get(v)]
        if missing:
            print(f"faltan {', '.join(missing)} (o usa --mock)", file=sys.stderr)
            return 2
        tokens = {k: os.environ[v] for k, v in env.items()}
    return Story(http_caller(a.base), tokens, read_only=a.read_only).run()


if __name__ == "__main__":
    sys.exit(main())
