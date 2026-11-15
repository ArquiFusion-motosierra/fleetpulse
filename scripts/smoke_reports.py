#!/usr/bin/env python3
"""Smoke en vivo de los reportes del operario (T32), /recommend-checks (T33) y la administracion (T35) contra la HTTP
API fp-api del Learner Lab o, con --via-bff, contra el BFF de la consola (mismas rutas con prefijo /api).

    read -rs FP_TEST_PASSWORD && export FP_TEST_PASSWORD   # clave comun de los usuarios de prueba (solo en la shell)
    AWS_CONFIG_FILE=/dev/null AWS_REGION=us-east-1 .venv/bin/python scripts/smoke_reports.py --close-incident
    ... --via-bff                # por https://console-.../api/* (reenvia el ID token a AWS)
    ... --machine EX-019         # otra maquina de mineria (por defecto EX-017); una distinta por corrida
    ... --close-incident         # al final cierra el incidente que haya iniciado el reporte (usarlo siempre)
    ... --tech TEC-02            # otro tecnico de mineria para el paso 4 (por defecto TEC-01, el del issue)

Tokens: ID token de Cognito por USER_PASSWORD_AUTH (boto3 cognito-idp sin firmar: no usa credenciales de AWS) o, si
estan, FP_TOKEN_<ROL> (OPERADOR, TECNICO, SUPERVISOR, ADMIN, OPERADOR_AGRO, ADMIN_AGRO, HEAD). Nunca se imprimen.

Recorrido (cada paso dice la ruta, el metodo y lo que se espera; sale de aws/reports/api/*.py):
 1 catalogo sembrado (GET /abnormality-types)            9 incidente del reporte visible en la consola (linea de tiempo)
 2 sin token -> 401                                      10 lecturas por rol: tecnico 200, operador ajeno 403, mine
 3 admin por rol y parque: operador 403, admin.agro 404  11 parques cruzados: admin.agro 404 / 403, jefe 200
 4 PUT /admin/technicians/TEC-01 cambia available        12 cierre por el tecnico asignado (o supervisor) y 409 al repetir
 5 catalogo admin: alta de un tipo de prueba             13 EV# report.closed en el incidente y en el reporte
 6 operador de agro no reporta una maquina de mineria    14 cierre del segundo reporte (limpieza)
 7 POST /reports del operador -> 201 con checks e incidente  15 borrado del tipo de prueba (200, luego 404)
 8 la recomendacion vino de Gemini (source)              16 (--close-incident) cierre del incidente iniciado
Los reportes no se borran (no hay ruta): quedan cerrados y con "[smoke_reports]" en la descripcion.
Owner: Benjamin (T32/T35) y Fabricio (T33).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

AWS_API = "https://w71oiihgvb.execute-api.us-east-1.amazonaws.com"
CONSOLE = "https://console-qgbfzxdq2a-ue.a.run.app"
CLIENT_ID = "ekbd3losd6961grf2emp3e9mi"
REGION = "us-east-1"
USERS = {"operador": "operador", "tecnico": "tecnico", "supervisor": "supervisor", "admin": "admin",
         "operador_agro": "operador.agro", "admin_agro": "admin.agro", "head": "jefe.mantenimiento"}
TECH = "TEC-01"  # el del tecnico de prueba (custom:tech_id de tecnico@); el paso 4 puede usar otro con --tech
SEED = {"high_engine_temp", "high_hydraulic_temp", "abnormal_vibration", "low_hydraulic_pressure", "smoke", "leak",
        "noise", "warning_light", "power_loss", "other"}
TMP_TYPE = f"smoke_tmp_{int(time.time())}"  # unico por corrida: no choca con otra corrida u otro agente
TAG = "[smoke_reports]"
RPT_RE = re.compile(r"^RPT-(EX|CF|VQ|MN|TO|TA|CS|MC|CP|GE)-[0-9]{3}-[0-9]{8}T[0-9]{6}$")
INC_RE = re.compile(r"^INC-(EX|CF|VQ|MN|TO|TA|CS|MC|CP|GE)-[0-9]{3}-[0-9]{8}T[0-9]{4}$")
POLL_S, POLL_EVERY_S = 90, 3  # CreateIncident de incident-flow tarda unos segundos (Classify + Correlate + Create)


class Fail(Exception):
    pass


def cognito_tokens(password: str, domain: str = "example.com") -> dict:
    """ID token por usuario con USER_PASSWORD_AUTH; initiate_auth es una operacion sin firma (no requiere credenciales)."""
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    idp = boto3.client("cognito-idp", region_name=REGION, config=Config(signature_version=UNSIGNED))
    out = {}
    for key, name in USERS.items():
        env = os.environ.get(f"FP_TOKEN_{key.upper()}")
        if env:
            out[key] = env
            continue
        r = idp.initiate_auth(AuthFlow="USER_PASSWORD_AUTH", ClientId=CLIENT_ID,
                              AuthParameters={"USERNAME": f"{name}@{domain}", "PASSWORD": password})
        out[key] = r["AuthenticationResult"]["IdToken"]
    return out


class Api:
    """call(method, path, who, body, query) -> (status, json, segundos). `who` None = sin token."""

    def __init__(self, base: str, prefix: str, tokens: dict, timeout: float = 30.0):
        self.base, self.prefix, self.t, self.timeout = base.rstrip("/"), prefix, tokens, timeout

    def call(self, method, path, who=None, body=None, query="", raw_base=None):
        url = (raw_base or self.base + self.prefix) + path + (f"?{query}" if query else "")
        headers = {"content-type": "application/json"}
        if who:
            headers["authorization"] = f"Bearer {self.t[who]}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                text = r.read()
                return r.status, (json.loads(text) if text else {}), time.monotonic() - t0
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}"), time.monotonic() - t0
            except ValueError:
                return e.code, {}, time.monotonic() - t0
        except (urllib.error.URLError, TimeoutError) as e:
            raise Fail(f"{method} {path}: sin respuesta ({type(e).__name__})") from e

    def console(self, method, path, who, body=None):
        """Siempre contra el BFF (incidentes: la HTTP API los sirve con x-api-key, que no tenemos aqui)."""
        return self.call(method, path, who, body, raw_base=CONSOLE)


class Story:
    def __init__(self, api: Api, machine: str, via_bff: bool, close_incident: bool, out=print, sleep=time.sleep,
                 tech: str = TECH):
        self.api, self.machine, self.bff, self.close_inc, self.out, self.sleep = api, machine, via_bff, close_incident, out, sleep
        self.tech = tech  # tecnico del paso 4 (PUT available)
        self.steps: list[tuple[str, str, float, str]] = []
        self.a: dict = {}   # reporte del operador
        self.b: dict = {}   # reporte del admin (para el 403 del operador ajeno)
        self.inc: dict = {}  # incidente de A visto por la consola

    # ---- ayudas ----
    def expect(self, method, path, who, code, body=None, query=""):
        status, data, dt = self.api.call(method, path, who, body, query)
        if status != code:
            raise Fail(f"{method} {path} como {who or 'anonimo'}: HTTP {status} (esperaba {code}) "
                       f"{json.dumps(data)[:160]}")
        return data, dt

    def step(self, name, fn):
        t0 = time.monotonic()
        try:
            detail, level = fn() or "", "OK"
        except Fail as e:
            detail, level = str(e), "FAIL"
        except Exception as e:  # KeyError/TypeError por una respuesta inesperada: FAIL y se sigue (la limpieza corre)
            detail, level = f"{type(e).__name__}: {e}"[:200], "FAIL"
        dt = time.monotonic() - t0
        if level == "OK" and isinstance(detail, tuple):
            detail, level = detail
        self.steps.append((level, name, dt, detail))
        self.out(f"[{level:4}] {dt:5.1f}s  {name}: {detail}")
        return level != "FAIL"

    def _types(self, who="operador") -> dict:
        data, _ = self.expect("GET", "/abnormality-types", who, 200)
        return {t["code"]: t for t in data["abnormality_types"]}

    def _tech(self) -> dict:
        """El tecnico del paso 4 visto por el admin: ruta por id en AWS; por el BFF solo existe la lista."""
        tech = self.tech
        if self.bff:
            data, _ = self.expect("GET", "/admin/technicians", "admin", 200)
            row = next((t for t in data["technicians"] if t["tech_id"] == tech), None)
            if row is None or not row.get("seeded", True):
                raise Fail(f"{tech} sin item TECH# en fp-state (avisar al lider: falta la semilla)")
            return row
        status, data, _ = self.api.call("GET", f"/admin/technicians/{tech}", "admin")
        if status == 404:
            raise Fail(f"{tech} sin item TECH# en fp-state (avisar al lider: falta la semilla): {data}")
        if status != 200:
            raise Fail(f"GET /admin/technicians/{tech}: HTTP {status} {data}")
        return data["technician"]

    def _report(self, report_id, who="supervisor") -> dict:
        data, _ = self.expect("GET", f"/reports/{report_id}", who, 200)
        return data

    # ---- pasos ----
    def s1_catalog(self):
        types = self._types()
        missing = SEED - set(types)
        if missing:
            raise Fail(f"faltan en el catalogo: {sorted(missing)}")
        return f"{len(types)} tipos, los 10 de la semilla presentes"

    def s2_no_token(self):
        for method, path in (("GET", "/reports"), ("GET", "/admin/technicians"), ("GET", "/abnormality-types")):
            self.expect(method, path, None, 401)
        return "GET /reports, /admin/technicians, /abnormality-types sin token -> 401"

    def s3_admin_roles(self):
        self.expect("PUT", f"/admin/technicians/{TECH}", "operador", 403, {"available": False})
        self.expect("PUT", f"/admin/technicians/{TECH}", "admin_agro", 404, {"available": False})
        data, _ = self.expect("GET", "/admin/technicians", "admin_agro", 200)
        ids = sorted(t["tech_id"] for t in data["technicians"])
        foreign = [t for t in ids if not t.startswith("TEC-1")]
        if foreign or data.get("tenant_id") != "agro":
            raise Fail(f"admin.agro ve {ids} (tenant_id={data.get('tenant_id')})")
        return f"operador -> 403; admin.agro sobre {TECH} -> 404; admin.agro lista solo {ids}"

    def s4_put_available(self):
        tech = self.tech
        before = self._tech()
        orig = bool(before.get("available", True))
        status, data, _ = self.api.call("PUT", f"/admin/technicians/{tech}", "admin", {"available": not orig})
        if status == 409:
            return (f"{tech} ocupado con {data.get('current_incident')}: PUT available -> 409 (no cumple el checkbox: "
                    f"ver plan seccion 5, #33)", "WARN")
        try:
            if status != 200 or (data.get("technician") or {}).get("available") is orig:
                raise Fail(f"PUT available={not orig}: HTTP {status} {json.dumps(data)[:160]}")
            changed = data["technician"]
        finally:  # restaurar SIEMPRE si el primer PUT pudo haber escrito, aunque la verificacion falle
            s2, data2, _ = self.api.call("PUT", f"/admin/technicians/{tech}", "admin", {"available": orig})
        if s2 != 200 or (data2.get("technician") or {}).get("available") is not orig:
            raise Fail(f"NO SE RESTAURO {tech} available={orig}: HTTP {s2} {json.dumps(data2)[:160]} "
                       f"(avisar al lider: el tecnico quedo con available={not orig})")
        if before.get("current_incident") != data2["technician"].get("current_incident"):
            raise Fail("el PUT toco current_incident")
        return (f"{tech} available {orig} -> {changed.get('available')} -> {orig}; "
                f"updated_by={data2['technician'].get('updated_by')}")

    def s5_admin_catalog(self):
        if self.bff:
            types = self._types("admin")
        else:
            data, _ = self.expect("GET", "/admin/abnormality-types", "admin", 200)
            types = {t["code"]: t for t in data["abnormality_types"]}
        if not SEED <= set(types):
            raise Fail(f"catalogo admin sin la semilla completa: {sorted(set(types))}")
        body = {"code": TMP_TYPE, "label_es": "Prueba smoke (borrar)", "default_checks": ["Paso de prueba 1", "Paso 2"]}
        status, data, _ = self.api.call("POST", "/admin/abnormality-types", "admin", body)
        if status not in (200, 201):
            raise Fail(f"POST /admin/abnormality-types: HTTP {status} {json.dumps(data)[:160]}")
        seen = self._types().get(TMP_TYPE)
        if not seen or seen.get("default_checks") != body["default_checks"]:
            raise Fail(f"el operador no ve {TMP_TYPE} con sus default_checks: {seen}")
        note = "" if status == 201 else " (ya existia de una corrida anterior: 200)"
        return (f"{TMP_TYPE} creado ({status}) y visible en GET /abnormality-types{note}", "WARN" if note else "OK")

    def s6_cross_create(self):
        body = {"machine_id": self.machine, "abnormality_type": "noise", "description": f"{TAG} cruce de parque",
                "severity_perceived": "low"}
        data, _ = self.expect("POST", "/reports", "operador_agro", 403, body)
        return f"operador.agro sobre {self.machine} -> 403 ({data.get('detail', '')[:60]})"

    def s7_create(self):
        body = {"machine_id": self.machine, "abnormality_type": "high_engine_temp",
                "description": f"{TAG} Sale vapor del motor y el indicador de temperatura esta en rojo",
                "severity_perceived": "high"}
        data, dt = self.expect("POST", "/reports", "operador", 201, body)
        rep = data["report"]
        self.a = rep  # antes de validar: si algo falla abajo, los pasos de limpieza igual saben que existe
        if not RPT_RE.match(rep.get("report_id", "")):
            raise Fail(f"report_id invalido: {rep.get('report_id')}")
        rec = rep.get("recommendation") or {}
        if not rec.get("checks"):
            raise Fail("recommendation.checks vacio")
        if rep.get("incident_link") not in ("linked", "started") or not INC_RE.match(rep.get("incident_id") or ""):
            raise Fail(f"sin incidente: incident_link={rep.get('incident_link')} incident_id={rep.get('incident_id')} "
                       f"urgency={rec.get('urgency')} (ver log reporte_sin_incidente / start_execution_fallo)")
        slow = " (LENTO: > 12 s, cerca del timeout de 15 s)" if dt > 12 else ""
        return (f"{rep['report_id']} status={rep['status']} {len(rec['checks'])} checks urgency={rec['urgency']} "
                f"incidente {rep['incident_id']} ({rep['incident_link']}) en {dt:.1f}s{slow}", "WARN" if slow else "OK")

    def s8_gemini(self):
        rec = self.a["recommendation"]
        cause = (rec.get("probable_causes") or [{}])[0].get("cause", "")
        if rec.get("source") != "gemini":
            raise Fail(f"source={rec.get('source')} (plantilla). Ver recommend_fallback en /aws/lambda/fp-reports-dev-reports "
                       f"y gemini_fallback en insights para report_id={self.a['report_id']}")
        return f"source=gemini cached={rec.get('cached')} causa='{cause[:60]}' repuestos={rec.get('suggested_parts')}"

    def s9_incident(self):
        inc_id, deadline = self.a["incident_id"], time.monotonic() + POLL_S
        while True:
            status, data, _ = self.api.console("GET", f"/api/incidents/{inc_id}", "supervisor")
            if status == 200:
                break
            if time.monotonic() > deadline:
                raise Fail(f"{inc_id} no aparece en la consola tras {POLL_S}s (HTTP {status}); revisar la ejecucion "
                           f"de incident-flow con ese nombre")
            self.sleep(POLL_EVERY_S)
        self.inc = data
        types = [e.get("type") or e.get("event_type") for e in data.get("timeline") or []]
        st = data.get("status") or (data.get("incident") or {}).get("status")
        if st != "open":  # ExecutionAlreadyExists: el reporte apunto a un incidente viejo de la misma ventana de 15 min
            raise Fail(f"{inc_id} esta {st}: el reporte se vinculo a un incidente ya cerrado (misma maquina en la misma "
                       f"ventana de 15 min). Repetir con otra --machine")
        if self.a["incident_link"] == "linked" and "report.linked" not in types:
            return (f"{inc_id} status={st}, pero sin report.linked en la linea de tiempo: {types}", "WARN")
        return f"{inc_id} status={st} tech={data.get('tech_id') or (data.get('incident') or {}).get('tech_id')} eventos={types}"

    def s10_roles_read(self):
        rid = self.a["report_id"]
        self._report(rid, "tecnico")
        body = {"machine_id": self.machine, "abnormality_type": "noise", "description": f"{TAG} reporte del admin",
                "severity_perceived": "low"}
        data, _ = self.expect("POST", "/reports", "admin", 201, body)
        self.b = data["report"]
        self.expect("GET", f"/reports/{self.b['report_id']}", "operador", 403)
        mine = self._report(rid, "operador")
        if [e["type"] for e in mine["timeline"]] != ["report.created"]:
            raise Fail(f"linea de tiempo de {rid}: {[e['type'] for e in mine['timeline']]}")
        lst, _ = self.expect("GET", "/reports", "operador", 200)
        ids = [r["report_id"] for r in lst["reports"]]
        if rid not in ids or self.b["report_id"] in ids:
            raise Fail(f"GET /reports del operador: {ids[:5]} (debe tener {rid} y no {self.b['report_id']})")
        return (f"tecnico GET -> 200; operador sobre el del admin {self.b['report_id']} -> 403; "
                f"operador ve {len(ids)} suyos")

    def s11_cross_read(self):
        rid = self.a["report_id"]
        self.expect("GET", f"/reports/{rid}", "admin_agro", 404)
        self.expect("GET", "/reports", "admin_agro", 403, query="tenant=mineria")
        self._report(rid, "head")
        lst, _ = self.expect("GET", "/reports", "head", 200, query=f"tenant=mineria&machine_id={self.machine}")
        if rid not in [r["report_id"] for r in lst["reports"]]:
            raise Fail("el jefe de mantenimiento no ve el reporte con ?tenant=mineria")
        return "admin.agro -> 404 y ?tenant=mineria -> 403; jefe.mantenimiento -> 200 y lista por parque"

    def s12_close(self):
        rid = self.a["report_id"]
        current = self._report(rid)["report"]
        assigned = current.get("assigned_tech_id")
        body = {"findings": f"{TAG} refrigerante bajo, sin aceite; termostato pegado", "confirmed_cause": "thermostat"}
        if assigned in (None, TECH):
            who = "tecnico"
        else:  # incident-flow asigno a otro tecnico: el tecnico de prueba debe recibir 403 y cierra el supervisor
            data, _ = self.expect("POST", f"/reports/{rid}/close", "tecnico", 403, body)
            if "otro tecnico" not in data.get("detail", ""):
                raise Fail(f"403 sin el detalle esperado: {data}")
            who = "supervisor"
        data, _ = self.expect("POST", f"/reports/{rid}/close", who, 200, body)
        rep = data["report"]
        if rep.get("status") != "closed" or not rep.get("closed_at"):
            raise Fail(f"cierre sin status closed: {json.dumps(rep)[:160]}")
        expected_by = TECH if who == "tecnico" else None
        if expected_by and rep.get("closed_by") != expected_by:
            raise Fail(f"closed_by={rep.get('closed_by')} (esperaba {expected_by})")
        self.a = {**self.a, **rep, "incident_event": data.get("incident_event")}
        again, _ = self.expect("POST", f"/reports/{rid}/close", "supervisor", 409, body)
        ev = "incident_event=true" if data.get("incident_event") else "incident_event=FALSE (incidente ya cerrado?)"
        return (f"cerrado por {who} (asignado={assigned}) closed_by={rep.get('closed_by')}; {ev}; segundo cierre -> 409",
                "OK" if data.get("incident_event") else "WARN")

    def s13_incident_event(self):
        rid, inc_id = self.a["report_id"], self.a["incident_id"]
        rep = self._report(rid)
        types = [e["type"] for e in rep["timeline"]]
        if types != ["report.created", "report.closed"]:
            raise Fail(f"linea de tiempo del reporte: {types}")
        status, data, _ = self.api.console("GET", f"/api/incidents/{inc_id}", "supervisor")
        if status != 200:
            raise Fail(f"GET /api/incidents/{inc_id}: HTTP {status}")
        inc_types = [e.get("type") or e.get("event_type") for e in data.get("timeline") or []]
        if self.a.get("incident_event") and "report.closed" not in inc_types:
            raise Fail(f"el incidente {inc_id} no muestra report.closed: {inc_types}")
        if not self.a.get("incident_event"):
            return ("reporte con [report.created, report.closed]; el incidente no recibio EV# (estaba cerrado)", "WARN")
        return f"reporte con {types}; incidente {inc_id} con report.closed en su linea de tiempo"

    def s14_close_b(self):
        if not self.b:
            return ("no hay reporte B", "WARN")
        data, _ = self.expect("POST", f"/reports/{self.b['report_id']}/close", "supervisor", 200,
                              {"findings": f"{TAG} cierre de limpieza"})
        return f"{self.b['report_id']} cerrado por supervisor (closed_by={data['report'].get('closed_by')})"

    def s15_cleanup_type(self):
        data, _ = self.expect("DELETE", f"/admin/abnormality-types/{TMP_TYPE}", "admin", 200)
        self.expect("DELETE", f"/admin/abnormality-types/{TMP_TYPE}", "admin", 404)
        if TMP_TYPE in self._types():
            raise Fail(f"{TMP_TYPE} sigue en GET /abnormality-types tras borrarlo")
        return f"deleted={data.get('deleted')}; segundo DELETE -> 404; ya no esta en el catalogo"

    def s16_close_incident(self):
        if not self.close_inc:
            return ("sin --close-incident: el incidente queda abierto (lo cierra el supervisor desde la consola)", "WARN")
        if self.a.get("incident_link") != "started":
            return (f"el incidente {self.a.get('incident_id')} ya existia (linked): no se cierra aqui", "WARN")
        status, data, _ = self.api.console("POST", f"/api/incidents/{self.a['incident_id']}/close", "supervisor",
                                           {"cause_summary": f"{TAG} cierre tras el smoke"})
        if status != 200:
            raise Fail(f"cerrar {self.a['incident_id']}: HTTP {status} {json.dumps(data)[:160]}")
        return f"{self.a['incident_id']} cerrado"

    def run(self) -> int:
        self.step("1 catalogo sembrado", self.s1_catalog)
        self.step("2 sin token -> 401", self.s2_no_token)
        self.step("3 admin por rol y parque", self.s3_admin_roles)
        self.step(f"4 PUT /admin/technicians/{self.tech} available", self.s4_put_available)
        self.step("5 catalogo admin + tipo de prueba", self.s5_admin_catalog)
        self.step("6 operador de agro no reporta en mineria", self.s6_cross_create)
        ok = self.step("7 POST /reports del operador", self.s7_create)
        if ok:
            self.step("8 recomendacion de Gemini", self.s8_gemini)
            self.step("9 incidente en la consola", self.s9_incident)
            self.step("10 lecturas por rol", self.s10_roles_read)
            self.step("11 parques cruzados", self.s11_cross_read)
            if self.step("12 cierre del reporte", self.s12_close):
                self.step("13 EV# en incidente y reporte", self.s13_incident_event)
            self.step("14 cierre del reporte del admin", self.s14_close_b)
        self.step("15 borrado del tipo de prueba", self.s15_cleanup_type)
        if self.a:  # tambien si el paso 7 fallo despues de crear el reporte: cerrar el incidente que haya iniciado
            self.step("16 cierre del incidente", self.s16_close_incident)
        fails = [s for s in self.steps if s[0] == "FAIL"]
        self.out(f"\n{len(self.steps) - len(fails)}/{len(self.steps)} pasos sin FAIL; total {sum(s[2] for s in self.steps):.1f}s")
        if self.a:
            rid = self.a["report_id"]
            self.out(f"reporte A: {rid}  incidente: {self.a.get('incident_id')}  reporte B: {self.b.get('report_id')}  "
                     f"tipo de prueba: {TMP_TYPE}")
            self.out("logs (a mano):\n"
                     f"  aws logs filter-log-events --log-group-name /aws/lambda/fp-reports-dev-reports --filter-pattern '\"{rid}\"' --start-time $(( ($(date +%s) - 1800) * 1000 )) --query 'events[].message' --output text\n"
                     f"  gcloud run services logs read insights --project fleetpulse-g19-2026 --region us-east1 --limit 200 | grep '{rid}'")
        return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=AWS_API, help="HTTP API de AWS (o la consola con --via-bff)")
    ap.add_argument("--via-bff", action="store_true", help="rutas /api/* del BFF de la consola")
    ap.add_argument("--machine", default="EX-017", help="maquina de mineria que reporta el operador")
    ap.add_argument("--close-incident", action="store_true", help="cerrar al final el incidente iniciado por el reporte")
    ap.add_argument("--tech", default=TECH, help="tecnico de mineria para el PUT del paso 4 (por defecto TEC-01)")
    a = ap.parse_args()
    if not re.match(r"^(EX|CF|VQ|MN|TO)-[0-9]{3}$", a.machine):
        print("--machine debe ser una maquina de mineria (EX/CF/VQ/MN/TO-nnn)", file=sys.stderr)
        return 2
    if not re.match(r"^TEC-0[0-9]$", a.tech):
        print("--tech debe ser un tecnico de mineria (TEC-01..TEC-06)", file=sys.stderr)
        return 2
    password = os.environ.get("FP_TEST_PASSWORD", "")
    have_env = all(os.environ.get(f"FP_TOKEN_{k.upper()}") for k in USERS)
    if not password and not have_env:
        print("falta FP_TEST_PASSWORD (o los FP_TOKEN_<ROL>)", file=sys.stderr)
        return 2
    try:
        tokens = cognito_tokens(password)
    except KeyError:  # sin AuthenticationResult: Cognito pidio un challenge (p. ej. NEW_PASSWORD_REQUIRED)
        print("Cognito devolvio un challenge en vez de tokens (usuario sin clave permanente): avisar al lider",
              file=sys.stderr)
        return 2
    except Exception as exc:  # NotAuthorizedException, UserNotFoundException, red
        print(f"no se pudieron obtener los tokens de Cognito: {type(exc).__name__}", file=sys.stderr)
        return 2
    base, prefix = (CONSOLE, "/api") if a.via_bff else (a.base, "")
    if a.via_bff and a.base != AWS_API:
        base = a.base
    return Story(Api(base, prefix, tokens), a.machine, a.via_bff, a.close_incident, tech=a.tech).run()


if __name__ == "__main__":
    sys.exit(main())
