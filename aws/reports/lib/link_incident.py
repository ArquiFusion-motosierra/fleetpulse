"""Vinculo del reporte con un incidente: el incidente abierto de la maquina, o una ejecucion nueva de `incident-flow`.

1. Si la maquina tiene un incidente abierto (`store.open_incident_for`, GSI1): el reporte se vincula a el y se
   devuelve el `EV#` `report.linked` del incidente como elemento de TransactWriteItems, para que vaya en la misma
   transaccion que el META del reporte (si el reporte no se escribe, el incidente no queda con un vinculo huerfano).
   `report.linked` es solo linea de tiempo: no sale por el outbox, por eso no se valida contra el sobre.
2. Si no hay y la recomendacion pide `schedule` o `immediate`: `start_execution` de `incident-flow` con nombre
   `fplib.ids.incident_id(machine_id, now)` (ventana de 15 min) y una entrada como la de aws/scoring: `score` 2.1 o
   3.6, `severity`, `z` y `deviated_vars` de las medias del snapshot contra la linea base del META (|z| >= 1) o, si
   no hay ninguna, una sola entrada sintetica para el sintoma reportado (`value` = la z marcador cuando no hay
   media, igual que CreateIncident), `failure_mode` del primer `component` de la recomendacion (tabla de palabras
   clave; si no, el del tipo de anormalidad; si no, `sensor_fault`), `batch_id` = report_id y
   `source: operator_report`. `ExecutionAlreadyExists` (misma ventana) se ignora: el incidente es el mismo.
   Cualquier otro error al iniciar se registra y el reporte se guarda sin incidente (el reporte no se pierde).
3. Si no: sin incidente.
Idempotencia: el nombre de la ejecucion es el incident_id, asi que reintentar en la misma ventana no duplica nada.
"""
from __future__ import annotations

import json
import os
import unicodedata
import uuid

from fplib import clock, geohash, ids, log
from lib import store

SCORE = {"schedule": 2.1, "immediate": 3.6}
SIGMA_FLOOR = 1e-6
DEVIATED_Z = 1.0
# variable del score que mejor representa el sintoma reportado y su sentido (+1 sube, -1 baja)
SYMPTOM_VAR = {
    "high_engine_temp": ("coolant_temp_c", 1), "high_hydraulic_temp": ("hydraulic_oil_temp_c", 1),
    "abnormal_vibration": ("vibration_mm_s", 1), "low_hydraulic_pressure": ("hydraulic_pressure_bar", -1),
    "smoke": ("fuel_rate_lph", 1), "leak": ("hydraulic_pressure_bar", -1), "noise": ("vibration_mm_s", 1),
    "warning_light": ("coolant_temp_c", 1), "power_loss": ("engine_rpm", -1), "other": ("vibration_mm_s", 1),
}
# failure_mode por defecto de cada tipo cuando el componente no se reconoce
TYPE_MODE = {
    "high_engine_temp": "engine_overheat", "high_hydraulic_temp": "hydraulic_overheat",
    "abnormal_vibration": "final_drive", "low_hydraulic_pressure": "hydraulic_pump", "smoke": "fuel_contamination",
    "leak": "hydraulic_contamination", "noise": "final_drive", "warning_light": "sensor_fault",
    "power_loss": "fuel_contamination", "other": "sensor_fault",
}
# palabras clave del componente (ingles de T33 o espanol del LLM) -> failure_mode; gana la primera que aparece
COMPONENT_MODE = (
    ("head_gasket", "head_gasket"), ("culata", "head_gasket"),
    ("oil_cooler", "hydraulic_overheat"), ("enfriador", "hydraulic_overheat"), ("relief_valve", "hydraulic_overheat"),
    ("thermostat", "engine_overheat"), ("termostato", "engine_overheat"), ("water_pump", "engine_overheat"),
    ("bomba de agua", "engine_overheat"), ("radiat", "engine_overheat"), ("radiador", "engine_overheat"),
    ("coolant", "engine_overheat"), ("refrigera", "engine_overheat"), ("turbo", "engine_overheat"),
    ("hydraulic_pump", "hydraulic_pump"), ("bomba hidraulica", "hydraulic_pump"), ("seal", "hydraulic_pump"),
    ("sello", "hydraulic_pump"),
    ("hydraulic_filter", "hydraulic_contamination"), ("filtro hidraulico", "hydraulic_contamination"),
    ("hydraulic_oil", "hydraulic_contamination"), ("aceite hidraulico", "hydraulic_contamination"),
    ("hose", "hydraulic_contamination"), ("manguera", "hydraulic_contamination"),
    ("injector", "fuel_contamination"), ("inyector", "fuel_contamination"), ("fuel", "fuel_contamination"),
    ("combustible", "fuel_contamination"),
    ("final_drive", "final_drive"), ("mando final", "final_drive"), ("bearing", "final_drive"),
    ("rodamiento", "final_drive"),
    ("brake", "brake_wear"), ("freno", "brake_wear"),
    ("alternator", "electrical_fault"), ("alternador", "electrical_fault"), ("belt", "electrical_fault"),
    ("correa", "electrical_fault"), ("electric", "electrical_fault"),
    ("sensor", "sensor_fault"),
)
# modos de T26 que pueden no estar aun en ids.yaml / incident.schema.json
MODE_FALLBACK = {"head_gasket": "engine_overheat"}
_sfn = None


def _client():
    global _sfn
    if _sfn is None:
        import boto3
        from botocore.config import Config
        _sfn = boto3.client("stepfunctions", config=Config(connect_timeout=3, read_timeout=5,
                                                           retries={"max_attempts": 2}))
    return _sfn


def _known_modes() -> set[str]:
    cat = ids.load()
    return set(cat["failure_modes"]) & set(cat["specialty_by_failure_mode"])


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return text.lower().replace("-", "_")


def failure_mode_for(component: str | None, abnormality_type: str) -> str:
    """failure_mode del contrato para el componente de la recomendacion (puro)."""
    known = _known_modes()

    def ok(mode: str | None) -> str | None:
        if mode in known:
            return mode
        alt = MODE_FALLBACK.get(mode or "")
        return alt if alt in known else None

    text = _fold(component or "")
    if ok(text):
        return ok(text)
    for kw, mode in COMPONENT_MODE:
        if kw in text or kw.replace("_", " ") in text:
            found = ok(mode)
            if found:
                return found
    return ok(TYPE_MODE.get(abnormality_type)) or "sensor_fault"


def deviations(means: dict, baseline: dict | None) -> tuple[dict, list[dict]]:
    """(`z` {var: {self, peer}}, `deviated_vars` con |z| >= 1, mayor |z| primero) de las medias contra la linea base
    propia del META (puro)."""
    z_map, deviated = {}, []
    for var, mean in (means or {}).items():
        b = (baseline or {}).get(var)
        if not b:
            continue
        z = (float(mean) - float(b["mu"])) / max(float(b["sigma"]), SIGMA_FLOOR)
        z_map[var] = {"self": round(z, 3), "peer": None}
        if abs(z) >= DEVIATED_Z:
            deviated.append({"var": var, "value": round(float(mean), 3), "z": round(z, 3)})
    return z_map, sorted(deviated, key=lambda d: abs(d["z"]), reverse=True)


def symptom_entry(abnormality_type: str, means: dict) -> dict:
    """Entrada sintetica de `deviated_vars` para el sintoma reportado (puro)."""
    var, sign = SYMPTOM_VAR.get(abnormality_type, SYMPTOM_VAR["other"])
    z = DEVIATED_Z * sign
    mean = (means or {}).get(var)
    return {"var": var, "value": round(float(mean), 3) if mean is not None else z, "z": z}


def _num(x):
    return None if x is None else float(x)


def execution_input(report: dict, snapshot: dict, recommendation: dict, meta: dict, incident_id: str) -> dict:
    """Entrada de `incident-flow` equivalente a la de aws/scoring (pura)."""
    machine_id, urgency = report["machine_id"], recommendation["urgency"]
    zone_id = snapshot.get("zone_id") or meta.get("zone_id") or ids.machines().get(machine_id, {}).get("zone_id")
    centroid = ids.zones().get(zone_id) or {}
    lat, lon = _num(meta.get("lat")), _num(meta.get("lon"))
    if lat is None or lon is None:
        lat, lon = centroid.get("lat"), centroid.get("lon")
    z_map, deviated = deviations(snapshot.get("means") or {}, meta.get("baseline"))
    causes = recommendation.get("probable_causes") or [{}]
    return {
        "incident_id": incident_id, "machine_id": machine_id, "model": snapshot.get("model"),
        "serial": meta.get("serial") or ids.machines().get(machine_id, {}).get("serial", ""),
        "zone_id": zone_id or "", "lat": lat, "lon": lon,
        "geohash6": meta.get("geohash6") or geohash.encode(lat, lon, 6), "ts": report["reported_at"],
        "batch_id": report["report_id"], "score": SCORE[urgency], "severity": urgency, "z": z_map,
        "deviated_vars": deviated or [symptom_entry(report["abnormality_type"], snapshot.get("means") or {})],
        "failure_mode": failure_mode_for(causes[0].get("component"), report["abnormality_type"]),
        "hours_total": snapshot.get("hours_total") or 0, "source": "operator_report",
        "report_id": report["report_id"], "abnormality_type": report["abnormality_type"],
    }


def linked_envelope(report: dict, recommendation: dict, incident: dict, now) -> dict:
    """Sobre `report.linked` para el EV# del incidente (forma de envelope.schema.json, tipo fuera del enum)."""
    causes = recommendation.get("probable_causes") or []
    return {
        "event_id": str(uuid.uuid4()), "event_type": "report.linked", "schema_version": 1,
        "source": "aws.reports-api", "aggregate_type": "incident", "aggregate_id": incident["incident_id"],
        "occurred_at": clock.iso(now), "incident_id": incident["incident_id"], "machine_id": report["machine_id"],
        "payload": {
            "report_id": report["report_id"], "abnormality_type": report["abnormality_type"],
            "severity_perceived": report["severity_perceived"], "description": report["description"],
            "reported_by": report["reported_by"], "urgency": recommendation["urgency"],
            "recommendation_source": recommendation["source"],
            "probable_cause": causes[0]["cause"] if causes else None,
            "suggested_parts": recommendation.get("suggested_parts") or [],
        },
    }


def link_or_start(registry_table, state_table, sfn, report: dict, snapshot: dict, recommendation: dict, now, *,
                  open_incident: dict | None = None, lookup: bool = True) -> dict:
    """Decide el incidente del reporte. Devuelve `{incident_id, link (linked|started|none), linked, started,
    tech_id (del incidente abierto, si ya tiene), tx_items}`; `tx_items` van en la transaccion del reporte.

    `open_incident`: el ya consultado por el handler (para `/recommend-checks`); con `lookup=False` no se vuelve a
    consultar. `sfn` None crea el cliente de Step Functions al usarse.
    """
    ctx = {"report_id": report["report_id"], "machine_id": report["machine_id"]}
    if open_incident is None and lookup:
        open_incident = store.open_incident_for(registry_table, report["machine_id"])
    if open_incident:
        inc_id = open_incident["incident_id"]
        env = linked_envelope(report, recommendation, open_incident, now)
        log.event("reporte_vinculado", incident_id=inc_id, **ctx)
        return {"incident_id": inc_id, "link": "linked", "linked": True, "started": False,
                "tech_id": open_incident.get("tech_id"),
                "tx_items": [{"Put": {"TableName": registry_table.name,
                                      "Item": store.event_item(f"INCIDENT#{inc_id}", env)}}]}
    none = {"incident_id": None, "link": "none", "linked": False, "started": False, "tech_id": None, "tx_items": []}
    if recommendation["urgency"] not in SCORE:
        log.event("reporte_sin_incidente", urgency=recommendation["urgency"], **ctx)
        return none
    inc_id = ids.incident_id(report["machine_id"], now)
    ctx["incident_id"] = inc_id
    meta = state_table.get_item(Key={"PK": f"MACHINE#{report['machine_id']}", "SK": "META"}).get("Item") or {}
    payload = execution_input(report, snapshot, recommendation, meta, inc_id)
    sfn = sfn or _client()
    try:
        sfn.start_execution(stateMachineArn=os.environ["STATE_MACHINE_ARN"], name=inc_id,
                            input=json.dumps(payload, default=_num))
    except sfn.exceptions.ExecutionAlreadyExists:
        log.event("ejecucion_existente", **ctx)
    except Exception as exc:  # el reporte se guarda igual; el supervisor lo ve en GET /reports
        log.error("start_execution_fallo", error=type(exc).__name__, **ctx)
        return none
    else:
        log.event("ejecucion_iniciada", failure_mode=payload["failure_mode"], severity=payload["severity"],
                  deviated_vars=[d["var"] for d in payload["deviated_vars"]], **ctx)
    return {"incident_id": inc_id, "link": "started", "linked": False, "started": True, "tech_id": None,
            "tx_items": []}
