#!/usr/bin/env python3
"""Datos de la demo (T47, backlog#51): limpieza de restos de pruebas, casos de soporte tecnico en los 3 parques y
verificacion, contra el Learner Lab.

    AWS_CONFIG_FILE=/dev/null AWS_REGION=us-east-1 .venv/bin/python scripts/seed-demo.py verify
    ... cleanup --dry-run         # lista lo que limpiaria (solo lee)
    ... cleanup                   # BACKORDER# y RSV# de incidentes cerrados, TRF/PO sin ejecucion viva,
                                  #   REORDER# de faltantes cerrados, reportes en revision de incidentes cerrados
    ... cases --out DIR           # por parque: asignado, cerrado con su reporte revisado y un reporte nuevo; en
                                  #   mineria, EX-017 con el faltante de PRT-04 y la TRF sugerida desde agro
    ... pending --out DIR         # por parque: un incidente esperando aprobacion (WaitForApproval vence a los 30 min)
    ... teardown --run DIR/<run>.json   # cierra lo que creo esa corrida (para repetir)

`cleanup`, `cases`, `pending` y `teardown` usan la clave comun de los usuarios de prueba: solo por FP_TEST_PASSWORD en
el entorno (`read -rs FP_TEST_PASSWORD && export FP_TEST_PASSWORD`), nunca en archivos ni en la linea de comandos.
Los tokens no se imprimen. `verify` no la necesita.

Escritura directa en fp-registry solo donde no hay ruta de API (cada una se explica en su funcion): borrar BACKORDER#
y REORDER# huerfanos, liberar RSV# de incidentes cerrados y cerrar una TRF en transito cuya ejecucion de
supply-flow fallo (con la misma transaccion `_advance` de aws/supply/supply/flow.py). Incidentes: StartExecution de
incident-flow con la entrada de aws/scoring (como scripts/smoke_incidents.py) y cierre por POST /incidents/{id}/close.
Reportes: POST /reports y /reports/{id}/close con el ID token del rol. Owner: Benjamin (T47).
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.append(os.path.join(ROOT, "aws", "shared", "python"))

import smoke_incidents as si  # noqa: E402  (Lab, build_input, S1, S2)
from fplib import clock, ids  # noqa: E402

REGISTRY = "fp-registry"
AWS_API = "https://w71oiihgvb.execute-api.us-east-1.amazonaws.com"
CLIENT_ID = "ekbd3losd6961grf2emp3e9mi"
BY = "seed-demo"
USERS = {"operador": "operador", "supervisor": "supervisor", "operador_agro": "operador.agro",
         "supervisor_agro": "supervisor.agro", "operador_industria": "operador.industria",
         "supervisor_industria": "supervisor.industria", "head": "jefe.mantenimiento"}
OPERATOR = {"mineria": "operador", "agro": "operador_agro", "industria": "operador_industria"}
SUPERVISOR = {"mineria": "supervisor", "agro": "supervisor_agro", "industria": "supervisor_industria"}
DONE_DOC = ("received", "cancelled")

# Variables desviadas por modo de falla (entrada de incident-flow como la arma aws/scoring)
DEV = {
    "hydraulic_pump": si.S1,
    "fuel_contamination": si.S2,
    "hydraulic_contamination": [{"var": "hydraulic_oil_temp_c", "value": 64.0, "z": 2.8},
                                {"var": "hydraulic_pressure_bar", "value": 168.0, "z": -1.9}],
    "engine_overheat": [{"var": "coolant_temp_c", "value": 104.0, "z": 3.1}, {"var": "fuel_rate_lph", "value": 46.0, "z": 1.2}],
    "final_drive": [{"var": "vibration_mm_s", "value": 11.8, "z": 2.7}, {"var": "speed_kmh", "value": 3.1, "z": -1.4}],
    "electrical_fault": [{"var": "engine_rpm", "value": 1980.0, "z": 2.2}],
}
# Casos por parque. assigned: severidad immediate (sin aprobacion); closed: immediate + reporte vinculado y revisado +
# cierre; report: reporte del operario en otra maquina; pending: schedule, queda en WaitForApproval.
# Tecnico esperado del assigned = el usuario tecnico de prueba de ese parque (TEC-01, TEC-11, TEC-21).
CASES = {
    "mineria": {
        "assigned": {"machine": "EX-017", "mode": "hydraulic_pump", "tech": "TEC-01"},
        "closed": {"machine": "CF-031", "mode": "fuel_contamination", "type": "smoke", "severity": "medium",
                   "description": "Humo negro al acelerar en la rampa y perdida de fuerza con carga",
                   "findings": "Filtro de combustible saturado y agua en el separador; se cambio el filtro y se drenó el separador",
                   "cause": "fuel_filter"},
        "report": {"machine": "VQ-005", "type": "abnormal_vibration", "severity": "medium",
                   "description": "Vibracion fuerte en la cabina al bajar cargado por la rampa del tajo"},
        "pending": {"machine": "CF-032", "mode": "engine_overheat"},
    },
    "agro": {
        "assigned": {"machine": "TA-004", "mode": "hydraulic_contamination", "tech": "TEC-11"},
        "closed": {"machine": "TA-002", "mode": "fuel_contamination", "type": "power_loss", "severity": "medium",
                   "description": "El tractor pierde fuerza con el arado y se ahoga en la subida del fundo",
                   "findings": "Inyector con depositos y filtro de combustible obstruido; se cambio el filtro y se limpió el inyector",
                   "cause": "injector"},
        "report": {"machine": "CS-002", "type": "noise", "severity": "medium",
                   "description": "Golpeteo metalico en el rotor de trilla al entrar al lote de esparragos"},
        "pending": {"machine": "CS-003", "mode": "final_drive"},
    },
    "industria": {
        "assigned": {"machine": "MC-002", "mode": "hydraulic_pump", "tech": "TEC-21"},
        "closed": {"machine": "GE-002", "mode": "fuel_contamination", "type": "power_loss", "severity": "high",
                   "description": "El grupo electrogeno no toma carga en la prueba semanal y oscila la frecuencia",
                   "findings": "Filtro de combustible colmatado por diesel del tanque de dia; se cambio y se purgó el tanque",
                   "cause": "fuel_filter"},
        "report": {"machine": "CP-002", "type": "high_hydraulic_temp", "severity": "high",
                   "description": "El compresor se apaga por alta temperatura de aceite a media manana"},
        "pending": {"machine": "GE-003", "mode": "electrical_fault"},
    },
}
FAILED = si.FAILED  # la misma lista que llena si.check


def ok(msg):
    print(f"  OK   {msg}")


def warn(msg):
    print(f"  WARN {msg}")


def fail(msg):
    print(f"  FAIL {msg}", file=sys.stderr)
    FAILED.append(msg)


def now_iso() -> str:
    return clock.iso(clock.now())


# --- lectura -------------------------------------------------------------------------------------------------------

def sup():
    """Modulos de aws/supply (store, docs, flow). La ruta se agrega recien aqui: en la sesion unica de pytest de CI
    no debe quedar `aws/supply` (tiene un paquete `api`) en sys.path para los tests de otros servicios."""
    path = os.path.join(ROOT, "aws", "supply")
    if path not in sys.path:
        sys.path.append(path)
    from supply import docs, flow, store
    return store, docs, flow


def scan(ddb) -> list[dict]:
    sstore, _, _ = sup()
    out = []
    for page in ddb.get_paginator("scan").paginate(TableName=REGISTRY):
        out += [sstore.des(i) for i in page["Items"]]
    return out


def execution_status(lab, machine: str, name: str) -> str | None:
    arn = f"arn:aws:states:{si.REGION}:{lab.account}:execution:{machine}:{name}"
    try:
        return lab.sfn.describe_execution(executionArn=arn)["status"]
    except lab.sfn.exceptions.ExecutionDoesNotExist:
        return None


class Snapshot:
    def __init__(self, items: list[dict]):
        self.items = items
        self.incidents = {i["PK"].split("#", 1)[1]: i for i in items if i["PK"].startswith("INCIDENT#") and i["SK"] == "META"}
        self.closed = {k for k, v in self.incidents.items() if v.get("status") == "closed"}
        self.docs = [i for i in items if i["PK"].startswith(("TRF#", "PO#")) and i["SK"] == "META"]
        self.rsv = [i for i in items if i["PK"].startswith("RSV#")]
        self.backorders = [i for i in items if i["PK"].startswith("BACKORDER#")]
        self.reorders = [i for i in items if i["PK"].startswith("REORDER#")]
        self.stock = {i["PK"]: i for i in items if i["PK"].startswith("STOCK#") and i["SK"] == "META"}
        self.reports = [i for i in items if i["PK"].startswith("REPORT#") and i["SK"] == "META"]

    def gone(self, incident_id: str | None) -> bool:
        """Cerrado o sin META (huerfano)."""
        return incident_id in self.closed or incident_id not in self.incidents


def invariants(snap: Snapshot) -> list[str]:
    """Stock coherente con sus documentos: available = on_hand - reserved; reserved = RSV# reserved; in_transit = TRF
    in_transit; on_order = PO ordered. Devuelve los errores."""
    errs = []
    want = {}
    for r in snap.rsv:
        if r.get("status") == "reserved":
            want.setdefault(f"STOCK#{r['part_id']}#{r['zone_id']}", {}).setdefault("reserved", 0)
            want[f"STOCK#{r['part_id']}#{r['zone_id']}"]["reserved"] += int(r["qty"])
    for d in snap.docs:
        field = {"in_transit": "in_transit", "ordered": "on_order"}.get(d.get("status"))
        if field:
            k = f"STOCK#{d['part_id']}#{d['zone_id']}"
            want.setdefault(k, {}).setdefault(field, 0)
            want[k][field] += int(d["qty"])
    for k, s in sorted(snap.stock.items()):
        on_hand, reserved = int(s.get("on_hand", 0)), int(s.get("reserved", 0))
        if int(s.get("available", on_hand - reserved)) != on_hand - reserved:
            errs.append(f"{k}: available {s.get('available')} != on_hand {on_hand} - reserved {reserved}")
        for field in ("reserved", "in_transit", "on_order"):
            got, exp = int(s.get(field, 0) or 0), want.get(k, {}).get(field, 0)
            if got != exp:
                errs.append(f"{k}: {field}={got} pero los documentos suman {exp}")
        if on_hand < 0 or reserved < 0:
            errs.append(f"{k}: negativo (on_hand {on_hand}, reserved {reserved})")
    return errs


# --- limpieza ------------------------------------------------------------------------------------------------------

def plan_cleanup(lab, snap: Snapshot) -> list[tuple[str, dict]]:
    acts = []
    # 1 primero los BACKORDER#: AllocateBackorders reservaria la bomba de la demo para un incidente cerrado (#47)
    acts += [("delete_backorder", b) for b in snap.backorders if snap.gone(b["SK"])]
    for d in snap.docs:
        if d.get("status") in DONE_DOC:
            continue
        exe = execution_status(lab, "supply-flow", d["id"])
        if exe != "RUNNING":
            acts.append(("cancel_doc", {**d, "_execution": exe}))
    acts += [("release_rsv", r) for r in snap.rsv if r.get("status") == "reserved" and snap.gone(r.get("incident_id"))]
    acts += [("delete_reorder", r) for r in snap.reorders
             if r.get("source") == "shortage" and snap.gone(r.get("incident_id"))]
    acts += [("close_report", r) for r in snap.reports
             if r.get("status") in ("open", "in_review") and r.get("incident_id") and snap.gone(r["incident_id"])]
    return acts


def describe(act: str, it: dict) -> str:
    if act == "cancel_doc":
        return (f"{act} {it['id']} status={it['status']} qty={it['qty']} {it.get('from_zone')}->{it['zone_id']} "
                f"ejecucion={it['_execution']}")
    if act == "close_report":
        return f"{act} {it['PK'].split('#', 1)[1]} ({it.get('tenant_id')}, {it['status']}, incidente {it['incident_id']} cerrado)"
    extra = f" qty={it.get('qty')}" if it.get("qty") is not None else ""
    return f"{act} {it['PK']} / {it['SK']}{extra} (incidente {it.get('incident_id') or it['SK']})"


def delete_item(ddb, it: dict) -> None:
    """BACKORDER# / REORDER# de un incidente cerrado: no hay ruta de API que los borre (supply-flow solo borra los de
    su propia orden al cancelar o recibir). Borrado condicionado a que exista: repetirlo no hace nada."""
    sstore, _, _ = sup()
    try:
        ddb.delete_item(TableName=REGISTRY, Key=sstore.key(it["PK"], it["SK"]), ConditionExpression="attribute_exists(PK)")
    except ddb.exceptions.ConditionalCheckFailedException:
        pass


def cancel_doc(ddb, doc: dict) -> None:
    """Orden sin ejecucion viva de supply-flow (fallo o se detuvo). La API no puede: approve/reject/receive necesitan
    un task token que ya no existe, y no hay ruta de cancelacion. Se usa la transicion del propio flujo
    (`supply.flow._advance`: META con condicion de estado + stock + EV# + supply.cancelled en fp-outbox, en una
    transaccion) con las compensaciones de lo que ya habia movido:
    - TRF in_transit: el despacho ya bajo el origen y subio el in_transit del destino -> vuelve al origen
      (on_hand/available += qty, MOVE# transfer_in con ref = TRF) y el destino in_transit -= qty.
    - TRF/PO suggested o approved: lo mismo que la Lambda `flowCancel` (borra sus BACKORDER#).
    - PO ordered: no se toca (on_order con un proveedor real): FAIL para que lo decida una persona."""
    _, docs, flow = sup()
    fresh = docs.load(ddb, doc["id"])
    status = fresh["status"]
    if status in DONE_DOC:
        return
    reason = f"cerrada en la preparacion de la demo: la ejecucion de supply-flow termino en {doc['_execution']}"
    at = now_iso()
    fields = {"cancelled_by": BY, "cancel_reason": reason, "cancelled_at": at}
    if status in ("suggested", "approved"):
        flow.cancel({"id": doc["id"], "cancel": {"Cause": json.dumps({"reason": reason, "by": BY})}}, ddb=ddb)
        return
    if status != "in_transit":
        raise RuntimeError(f"{doc['id']} esta {status}: no se cancela solo (decidir a mano)")
    q = int(fresh["qty"])
    items = [
        docs.stock_item(fresh["part_id"], fresh["zone_id"], "SET in_transit = in_transit - :q",
                        "attribute_exists(PK) AND in_transit >= :q", q=q),
        docs.stock_item(fresh["part_id"], fresh["from_zone"], "SET on_hand = on_hand + :q, available = available + :q", q=q),
        docs.move_item(fresh["part_id"], fresh["from_zone"], "transfer_in", q, fresh["id"], fresh["from_tenant"], at),
    ]
    flow._advance(ddb, fresh, ("in_transit",), "cancelled", items, fields, reason=reason)


def release_rsv(ddb, r: dict) -> None:
    """Reserva viva de un incidente ya cerrado. El cierre (aws/registry consume_items) ya paso, asi que nada la va a
    liberar; se hace lo mismo que hace el cierre con lo no usado: RSV# -> released y reserved -= qty, available += qty,
    en una transaccion condicionada (repetirla no descuenta dos veces)."""
    sstore, docs, _ = sup()
    q = int(r["qty"])
    try:
        sstore.transact(ddb, [
            {"Update": {"TableName": REGISTRY, "Key": sstore.key(r["PK"], r["SK"]),
                        "UpdateExpression": "SET #st = :rel, released_at = :at, released_by = :by",
                        "ConditionExpression": "#st = :res", "ExpressionAttributeNames": {"#st": "status"},
                        "ExpressionAttributeValues": sstore.vals(rel="released", res="reserved", at=now_iso(), by=BY)}},
            docs.stock_item(r["part_id"], r["zone_id"], "SET reserved = reserved - :q, available = available + :q",
                            "attribute_exists(PK) AND reserved >= :q", q=q),
        ])
    except sstore.ConditionFailed as exc:
        if exc.reasons[0] == "ConditionalCheckFailed":  # ya no estaba reserved: otra corrida la libero
            return
        raise


# --- API con token ---------------------------------------------------------------------------------------------------

def tokens(keys) -> dict:
    password = os.environ.get("FP_TEST_PASSWORD", "")
    if not password:
        raise SystemExit("falta FP_TEST_PASSWORD: read -rs FP_TEST_PASSWORD && export FP_TEST_PASSWORD")
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    idp = boto3.client("cognito-idp", region_name=si.REGION, config=Config(signature_version=UNSIGNED))
    out = {}
    for k in keys:
        try:
            r = idp.initiate_auth(AuthFlow="USER_PASSWORD_AUTH", ClientId=CLIENT_ID,
                                  AuthParameters={"USERNAME": f"{USERS[k]}@example.com", "PASSWORD": password})
            out[k] = r["AuthenticationResult"]["IdToken"]
        except Exception as exc:  # sin imprimir la clave ni el token
            raise SystemExit(f"Cognito no dio token para {USERS[k]}: {type(exc).__name__}") from None
    return out


def call(method: str, path: str, token: str, body=None) -> tuple[int, dict]:
    req = urllib.request.Request(AWS_API + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers={"content-type": "application/json",
                                                         "authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            text = r.read()
            return r.status, json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def close_report(tok: dict, rep: dict, findings: str, cause: str | None = None) -> bool:
    rid = rep.get("report_id") or rep["PK"].split("#", 1)[1]
    who = SUPERVISOR[rep.get("tenant_id") or ids.tenant_of_machine(rep["machine_id"])]
    body = {"findings": findings, **({"confirmed_cause": cause} if cause else {})}
    code, data = call("POST", f"/reports/{rid}/close", tok[who], body)
    if code == 409:
        ok(f"{rid} ya estaba cerrado")
        return True
    if code != 200 or (data.get("report") or {}).get("status") != "closed":
        fail(f"cerrar {rid} como {USERS[who]}: HTTP {code} {json.dumps(data)[:160]}")
        return False
    ok(f"{rid} cerrado por {USERS[who]}")
    return True


def cleanup(lab, args) -> None:
    ddb = _ddb()
    snap = Snapshot(scan(ddb))
    acts = plan_cleanup(lab, snap)
    print(f"[cleanup] {len(acts)} acciones")
    for act, it in acts:
        print(f"  - {describe(act, it)}")
    errs = invariants(snap)
    print(f"  invariantes de stock antes: {len(errs)} diferencias")
    for e in errs:
        print(f"    {e}")
    if args.dry_run or not acts:
        return
    sups = {SUPERVISOR[r.get("tenant_id") or "mineria"] for a, r in acts if a == "close_report"}
    tok = tokens(sorted(sups)) if sups else {}
    for act, it in acts:
        try:
            if act in ("delete_backorder", "delete_reorder"):
                delete_item(ddb, it)
            elif act == "cancel_doc":
                cancel_doc(ddb, it)
            elif act == "release_rsv":
                release_rsv(ddb, it)
            elif act == "close_report":
                close_report(tok, it, "Revision cerrada: el incidente vinculado ya fue atendido y cerrado.")
                continue
            ok(describe(act, it))
        except Exception as exc:  # una accion que falla no detiene las demas
            fail(f"{describe(act, it)}: {type(exc).__name__}: {exc}")
    after = Snapshot(scan(ddb))
    left = plan_cleanup(lab, after)
    errs = invariants(after)
    (ok if not left else fail)(f"quedan {len(left)} restos: {[describe(a, i) for a, i in left]}")
    (ok if not errs else fail)(f"invariantes de stock: {errs or 'sin diferencias'}")


def _ddb():
    import boto3
    return boto3.client("dynamodb", region_name=si.REGION)


# --- casos -------------------------------------------------------------------------------------------------------

def free(lab, machine: str, now: datetime) -> str | None:
    """None si la maquina esta libre; si no, el motivo."""
    if lab.open_incidents(machine):
        return f"incidente abierto {lab.open_incidents(machine)}"
    name = ids.incident_id(machine, now)
    if execution_status(lab, "incident-flow", name):
        return f"ya hubo una ejecucion {name} en esta ventana de 15 min"
    return None


def start(lab, machine: str, mode: str, score: float, now: datetime) -> tuple[str, str | None]:
    payload = si.build_input(machine, lab.position(machine), score, mode, DEV[mode], now)
    return payload["incident_id"], lab.start(payload)


def rsv_lines(ddb, incident_id: str, wait_s: int = 45) -> list[dict]:
    sstore, _, _ = sup()
    end = time.monotonic() + wait_s
    while True:
        rows = [r for r in sstore.query_all(ddb, f"RSV#{incident_id}") if r.get("status") == "reserved"]
        if rows or time.monotonic() > end:
            return rows
        time.sleep(3)


def wait_transfer(ddb, incident_id: str, wait_s: int = 150) -> dict | None:
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        docs = [i for i in Snapshot(scan(ddb)).docs if i.get("incident_id") == incident_id]
        trf = next((d for d in docs if d["id"].startswith("TRF-") and d.get("waiting_for") == "approval"), None)
        if trf:
            return trf
        time.sleep(5)
    return None


def record(run: dict, **kw) -> None:
    run["created"].append(kw)
    with open(run["_path"], "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in run.items() if k != "_path"}, f, indent=1)


def new_run(args, phase: str) -> dict:
    os.makedirs(args.out, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return {"phase": phase, "started_at": stamp, "created": [],
            "_path": os.path.join(args.out, f"seed-demo-{phase}-{stamp}.json")}


def report(tok: dict, tenant: str, machine: str, c: dict) -> dict | None:
    body = {"machine_id": machine, "abnormality_type": c["type"], "description": c["description"],
            "severity_perceived": c["severity"]}
    code, data = call("POST", "/reports", tok[OPERATOR[tenant]], body)
    rep = data.get("report") or {}
    if code != 201 or not rep.get("report_id"):
        fail(f"POST /reports {machine} como {USERS[OPERATOR[tenant]]}: HTTP {code} {json.dumps(data)[:160]}")
        return None
    ok(f"{rep['report_id']} status={rep.get('status')} incidente {rep.get('incident_id')} ({rep.get('incident_link')}) "
       f"recomendacion {(rep.get('recommendation') or {}).get('source')}")
    return rep


def close_incident(lab, ddb, incident_id: str, cause: str, use_parts: bool) -> bool:
    meta = lab.meta(incident_id)
    if meta.get("status") == "closed":
        return True
    parts = [{"part_id": r["part_id"], "zone_id": r["zone_id"], "qty": int(r["qty"])}
             for r in rsv_lines(ddb, incident_id, 0)] if use_parts else []
    code, body = lab.http("POST", f"/incidents/{incident_id}/close", key="console-key",
                          body={"closed_by": meta.get("tech_id") or BY, "parts_used": parts, "cause_summary": cause})
    closed = code == 200 and lab.meta(incident_id).get("status") == "closed"
    (ok if closed else fail)(f"cierre de {incident_id} -> {code} {'' if closed else body[:120]} (repuestos {parts})")
    return closed


def case_assigned(lab, ddb, run, tenant: str, c: dict, now: datetime) -> None:
    inc, arn = start(lab, c["machine"], c["mode"], 3.7, now)
    if not arn:
        return
    record(run, kind="incident", id=inc, tenant=tenant, case="assigned", machine=c["machine"])
    status, _ = lab.wait(arn, approval=False)
    if not si.check(status == "SUCCEEDED", f"{inc} {status}"):
        return
    tech = lab.meta(inc).get("tech_id")
    (ok if tech == c["tech"] else warn)(f"{inc} asignado a {tech} (esperado {c['tech']}: el tecnico de prueba del parque)")
    lines = rsv_lines(ddb, inc)
    (ok if lines else warn)(f"{inc} reservas {[(r['part_id'], r['zone_id'], int(r['qty'])) for r in lines]}")
    if c["machine"] != "EX-017":
        return
    # historia de PRT-04: faltante en los 3 almacenes de mina -> supply-sourcing sugiere la TRF desde agro
    end = time.monotonic() + 60
    while "part.shortage" not in (types := lab.ev_types(inc)) and time.monotonic() < end:
        time.sleep(3)
    si.check("part.shortage" in types, f"{inc} part.shortage en la linea de tiempo ({types})")
    trf = wait_transfer(ddb, inc)
    if not si.check(trf, f"TRF sugerida de PRT-04 para {inc} (supply-sourcing)"):
        return
    record(run, kind="transfer", id=trf["id"], tenant="mineria", case="shortage", machine="EX-017")
    (ok if trf.get("from_zone") == "FUNDO-A" else warn)(f"{trf['id']} desde {trf.get('from_zone')} (guion: FUNDO-A)")
    si.check(execution_status(lab, "supply-flow", trf["id"]) == "RUNNING", f"supply-flow {trf['id']} RUNNING")
    opts = [o.get("source_kind") for o in trf.get("options") or []]
    (ok if {"transfer", "purchase"} <= set(opts) else warn)(f"{trf['id']} opciones {opts}")


def case_closed(lab, ddb, tok, run, tenant: str, c: dict, now: datetime) -> None:
    inc, arn = start(lab, c["machine"], c["mode"], 3.7, now)
    if not arn:
        return
    record(run, kind="incident", id=inc, tenant=tenant, case="closed", machine=c["machine"])
    try:
        status, _ = lab.wait(arn, approval=False)
        if not si.check(status == "SUCCEEDED", f"{inc} {status}"):
            return
        rsv_lines(ddb, inc)
        rep = report(tok, tenant, c["machine"], c)
        if rep:
            record(run, kind="report", id=rep["report_id"], tenant=tenant, case="closed", machine=c["machine"])
            si.check(rep.get("incident_id") == inc and rep.get("incident_link") == "linked",
                     f"{rep['report_id']} vinculado a {inc} ({rep.get('incident_link')} {rep.get('incident_id')})")
            close_report(tok, {**rep, "tenant_id": tenant}, c["findings"], c["cause"])
    finally:  # el caso "cerrado" no puede quedar abierto aunque falle un paso del medio
        close_incident(lab, ddb, inc, c["findings"], use_parts=True)


def case_report(tok, run, tenant: str, c: dict) -> None:
    rep = report(tok, tenant, c["machine"], c)
    if rep:
        record(run, kind="report", id=rep["report_id"], tenant=tenant, case="report", machine=c["machine"],
               incident=rep.get("incident_id"))


def cases(lab, args) -> None:
    ddb, now = _ddb(), datetime.now(timezone.utc)
    machines = [(t, k, c["machine"]) for t, cs in CASES.items() for k, c in cs.items() if k != "pending"]
    busy = {m: why for _, _, m in machines if (why := free(lab, m, now))}
    print(f"[cases] maquinas {[m for _, _, m in machines]}")
    if not si.check(not busy, f"maquinas libres ({busy or 'todas'})"):
        return
    snap = Snapshot(scan(ddb))
    left = plan_cleanup(lab, snap)
    if not si.check(not left, f"limpieza hecha ({len(left)} restos; correr antes `cleanup`)"):
        return
    mine = {z: tuple(int(snap.stock.get(f"STOCK#PRT-04#{z}", {}).get(k, -1)) for k in ("on_hand", "available", "in_transit"))
            for z in ("NORTE", "CENTRO", "SUR")}
    if not si.check(all(v == (0, 0, 0) for v in mine.values()),
                    f"PRT-04 (on_hand, available, in_transit) en 0 en mina {mine} (seed-registry.py --demo-supply)"):
        return
    if args.dry_run:
        return
    tok = tokens(sorted({OPERATOR[t] for t in CASES} | {SUPERVISOR[t] for t in CASES}))
    run = new_run(args, "cases")
    print(f"  corrida: {run['_path']}")
    for tenant, cs in CASES.items():
        print(f"[{tenant}]")
        for name, fn in (("assigned", lambda: case_assigned(lab, ddb, run, tenant, cs["assigned"], now)),
                         ("closed", lambda: case_closed(lab, ddb, tok, run, tenant, cs["closed"], now)),
                         ("report", lambda: case_report(tok, run, tenant, cs["report"]))):
            try:
                fn()
            except Exception as exc:  # un caso que falla no impide los demas (y no deja el cerrado abierto)
                fail(f"{tenant}/{name}: {type(exc).__name__}: {exc}")


def pending(lab, args) -> None:
    now = datetime.now(timezone.utc)
    todo = [(t, cs["pending"]) for t, cs in CASES.items()]
    busy = {c["machine"]: why for _, c in todo if (why := free(lab, c["machine"], now))}
    print(f"[pending] {[c['machine'] for _, c in todo]} (WaitForApproval vence en 30 min y asigna solo)")
    if not si.check(not busy, f"maquinas libres ({busy or 'todas'})") or args.dry_run:
        return
    run = new_run(args, "pending")
    for tenant, c in todo:
        try:
            inc, arn = start(lab, c["machine"], c["mode"], 2.4, now)
            if not arn:
                continue
            record(run, kind="incident", id=inc, tenant=tenant, case="pending", machine=c["machine"])
            status, tok = lab.wait(arn, approval=True)
            si.check(status == "WAITING" and tok, f"{inc} esperando aprobacion ({status}); vence {clock.iso(now + timedelta(minutes=30))}")
        except Exception as exc:
            fail(f"{tenant}/pending: {type(exc).__name__}: {exc}")


def teardown(lab, args) -> None:
    with open(args.run, encoding="utf-8") as f:
        run = json.load(f)
    ssm = json.loads(lab.ssm.get_parameter(Name="/fleetpulse/sim/scenarios")["Parameter"]["Value"]) if _has_param(lab) else {}
    live = [k for k, v in ssm.items() if v]
    if live and not args.force:
        raise SystemExit(f"escenarios encendidos {live}: scripts/drift.py off all antes (si no, score reabre incidentes)")
    ddb = _ddb()
    created = run["created"]
    need = {SUPERVISOR[c["tenant"]] for c in created if c["kind"] == "report"} | (
        {"head"} if any(c["kind"] == "transfer" for c in created) else set())
    tok = tokens(sorted(need)) if need else {}
    for c in created:
        try:
            if c["kind"] == "transfer":
                code, data = call("POST", f"/supply/transfers/{c['id']}/reject", tok["head"], {"reason": "repetir la demo"})
                (ok if code in (200, 409) else fail)(f"rechazo {c['id']} -> {code} {json.dumps(data)[:100]}")
            elif c["kind"] == "report":
                close_report(tok, {"report_id": c["id"], "tenant_id": c["tenant"], "machine_id": c["machine"]},
                             "Cerrado al reiniciar los datos de la demo.")
            elif c["kind"] == "incident":
                close_incident(lab, ddb, c["id"], "Cerrado al reiniciar los datos de la demo.", use_parts=False)
        except Exception as exc:
            fail(f"teardown {c}: {type(exc).__name__}: {exc}")


def _has_param(lab) -> bool:
    try:
        lab.ssm.get_parameter(Name="/fleetpulse/sim/scenarios")
        return True
    except lab.ssm.exceptions.ParameterNotFound:
        return False


# --- verificacion -----------------------------------------------------------------------------------------------

def verify(lab, args) -> None:
    ddb = _ddb()
    snap = Snapshot(scan(ddb))
    print("[verify] abastecimiento")
    left = plan_cleanup(lab, snap)
    si.check(not left, f"sin restos de pruebas ({[describe(a, i) for a, i in left] or 'ninguno'})")
    errs = invariants(snap)
    si.check(not errs, f"invariantes de stock ({errs or 'sin diferencias'})")
    prt = {z: (int(s.get("on_hand", 0)), int(s.get("available", 0)), int(s.get("in_transit", 0)))
           for k, s in snap.stock.items() if k.startswith("STOCK#PRT-04#") for z in [k.split("#")[2]]}
    print(f"  PRT-04 (on_hand, available, in_transit): {prt}")
    trf = [d for d in snap.docs if d["part_id"] == "PRT-04" and d.get("status") == "suggested"]
    if trf:
        d = trf[0]
        si.check(d.get("waiting_for") == "approval" and execution_status(lab, "supply-flow", d["id"]) == "RUNNING",
                 f"{d['id']} sugerida desde {d.get('from_zone')} para {d.get('incident_id')}, esperando al jefe")
        si.check(all(prt.get(z, (1, 1, 1))[1] == 0 for z in ("NORTE", "CENTRO", "SUR")), "PRT-04 en 0 en la mina")
        bo = [b for b in snap.backorders if b["SK"] == d.get("incident_id")]
        si.check(bo, f"BACKORDER# de {d.get('incident_id')} esperando la bomba")
    else:
        warn("no hay TRF de PRT-04 sugerida (antes de `cases`, o ya se aprobo en camara)")
    print("[verify] casos por parque")
    tenants = ("mineria", "agro", "industria")
    for t in tenants:
        incs = [i for i in snap.incidents.values() if (i.get("tenant_id") or "mineria") == t]
        states = {"esperando aprobacion": [i for i in incs if i.get("status") == "open" and not i.get("tech_id")],
                  "asignado": [i for i in incs if i.get("status") == "open" and i.get("tech_id")],
                  "cerrado": [i for i in incs if i.get("status") == "closed"]}
        reps = [r for r in snap.reports if (r.get("tenant_id") or "mineria") == t]
        rstates = {s: [r for r in reps if r.get("status") == s] for s in ("open", "in_review", "closed")}
        print(f"  {t}: " + ", ".join(f"{k} {len(v)}" for k, v in states.items()) + " | reportes "
              + ", ".join(f"{k} {len(v)}" for k, v in rstates.items()))
        for k, v in states.items():
            for i in v[-3:]:
                print(f"      {k}: {i['PK'].split('#', 1)[1]} {i.get('severity')} tech={i.get('tech_id')}")
        distinct = sum(1 for v in states.values() if v) + sum(1 for v in rstates.values() if v)
        si.check(distinct >= 2 and sum(map(len, states.values())) >= 2,
                 f"{t}: al menos 2 casos en estados distintos ({distinct} estados)")
    print("[verify] flota en operacion")
    since = clock.iso(datetime.now(timezone.utc) - timedelta(minutes=args.fresh_min))
    metas = [i for i in _scan_state(ddb) if i["PK"].startswith("MACHINE#") and i["SK"] == "META"]
    for t in tenants:
        mine = [m for m in metas if (m.get("tenant_id") or "mineria") == t]
        fresh = [m for m in mine if (m.get("last_ts") or "") >= since]
        sev = {}
        for m in mine:
            sev[m.get("status") or "ok"] = sev.get(m.get("status") or "ok", 0) + 1
        (ok if mine and len(fresh) == len(mine) else warn)(
            f"{t}: {len(fresh)}/{len(mine)} con lectura en los ultimos {args.fresh_min} min; severidad {sev}")


def _scan_state(ddb) -> list[dict]:
    sstore, _, _ = sup()
    out = []
    for page in ddb.get_paginator("scan").paginate(TableName="fp-state", FilterExpression="SK = :m",
                                                   ExpressionAttributeValues={":m": {"S": "META"}}):
        out += [sstore.des(i) for i in page["Items"]]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("cleanup", "cases", "pending", "verify", "teardown"))
    ap.add_argument("--dry-run", action="store_true", help="solo lee y muestra lo que haria")
    ap.add_argument("--out", default=os.path.join(ROOT, "simulator", "out"), help="carpeta del registro de la corrida")
    ap.add_argument("--run", help="teardown: el JSON de la corrida")
    ap.add_argument("--force", action="store_true", help="teardown aunque haya escenarios encendidos")
    ap.add_argument("--fresh-min", type=int, default=3, help="verify: minutos para contar una lectura como reciente")
    args = ap.parse_args(argv)
    lab = si.Lab()
    {"cleanup": cleanup, "cases": cases, "pending": pending, "verify": verify, "teardown": teardown}[args.command](lab, args)
    print("seed-demo: " + ("VERDE" if not FAILED else f"ROJO ({len(FAILED)} fallas)"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
