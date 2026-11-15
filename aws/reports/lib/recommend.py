"""Chequeos recomendados al tecnico para un reporte del operario: Cloud Run `insights` `POST /recommend-checks`
(Gemini, T33) o una plantilla local por `abnormality_type` si la IA no responde bien.

Llamada: secreto `INVOKER_SECRET` (`fleetpulse/gcp/sa-invoker` = `{key, audience}`), ID token de la service account
con fplib.gcp_auth (cache 50 min) y `POST <audience>/recommend-checks` con timeout `TIMEOUT_S`. Cuerpo (contrato de
T33): report_id, machine_id, model, abnormality_type, description, severity_perceived, telemetry_snapshot{means,
fault_codes, hours_total, hours_since_service}, oem_specs{ranges, service_intervals, typical_failures} y
open_incident{incident_id, failure_mode, severity} si la maquina tiene un incidente abierto.
Respuesta aceptada: probable_causes[{cause, component, confidence}] y checks[{order, step, tool, expected,
if_fails}] no vacios, urgency en observe|schedule|immediate; suggested_parts se filtra a los repuestos del BOM del
modelo; safety_notes, source (gemini|template) y cached se conservan. Textos recortados a topes fijos.
La IA nunca bloquea el reporte: cualquier excepcion, estado distinto de 200, JSON invalido o forma incorrecta ->
`fallback(...)` (source `template`) y el log `recommend_fallback` con el motivo (nunca la llave ni el token).
Idempotencia: no escribe nada; el reporte guarda la recomendacion que salga.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from fplib import gcp_auth, ids, log
from fplib import secrets as fp_secrets

TIMEOUT_S = 8
URGENCIES = ("observe", "schedule", "immediate")
MAX_CAUSES, MAX_CHECKS, MAX_NOTES = 5, 10, 5
CAP = {"cause": 200, "component": 60, "step": 300, "tool": 100, "expected": 200, "if_fails": 200, "note": 200}


class BadResponse(ValueError):
    """Respuesta de Cloud Run inutilizable; el mensaje es el motivo que se registra."""


def _c(step, tool, expected, if_fails):
    return {"step": step, "tool": tool, "expected": expected, "if_fails": if_fails}


# Plantilla por tipo: componentes con nombres en ingles (como los devuelve T33) para mapearlos a un failure_mode en
# link_incident; `modes` da los repuestos sugeridos (BOM del modelo). Orden = semilla del contrato.
TEMPLATES = {
    "high_engine_temp": {
        "label_es": "Temperatura alta de motor", "urgency": "immediate", "modes": ["head_gasket", "engine_overheat"],
        "causes": [("Junta de culata con fuga", "head_gasket", 0.4), ("Termostato pegado", "thermostat", 0.3),
                   ("Bomba de agua con falla", "water_pump", 0.2)],
        "checks": [
            _c("Verificar nivel y color del refrigerante con el motor frio", "visual",
               "nivel en la marca, sin aceite ni espuma", "sospechar junta de culata; no seguir operando"),
            _c("Prueba de presion del sistema de refrigeracion", "bomba manual 1,2 bar", "mantiene la presion 10 min",
               "fuga interna o externa: revisar junta de culata y mangueras"),
            _c("Verificar apertura del termostato y caudal de la bomba de agua", "termometro infrarrojo",
               "manguera superior a 85-95 C con el motor en marcha", "cambiar termostato o bomba de agua"),
        ],
        "safety": ["No abrir la tapa del radiador con el motor caliente"],
    },
    "high_hydraulic_temp": {
        "label_es": "Temperatura alta de aceite hidraulico", "urgency": "schedule",
        "modes": ["hydraulic_overheat", "hydraulic_contamination"],
        "causes": [("Enfriador de aceite obstruido", "oil_cooler", 0.5),
                   ("Aceite hidraulico bajo o degradado", "hydraulic_oil", 0.3),
                   ("Valvula de alivio abierta", "relief_valve", 0.2)],
        "checks": [
            _c("Limpiar e inspeccionar el enfriador de aceite hidraulico", "visual / aire comprimido",
               "panal limpio y ventilador girando", "limpiar o reemplazar el enfriador"),
            _c("Verificar nivel y estado del aceite hidraulico", "varilla / visual", "nivel en la marca, aceite claro",
               "completar o cambiar el aceite hidraulico"),
            _c("Medir temperatura de entrada y salida del enfriador", "termometro infrarrojo", "caida de 8 a 12 C",
               "enfriador obstruido por dentro"),
        ],
        "safety": ["Despresurizar el circuito antes de abrir conexiones"],
    },
    "abnormal_vibration": {
        "label_es": "Vibracion anormal", "urgency": "schedule", "modes": ["final_drive"],
        "causes": [("Rodamiento del mando final gastado", "final_drive_bearing", 0.45),
                   ("Desbalance o pernos flojos", "unbalance", 0.3), ("Soporte de motor danado", "engine_mount", 0.25)],
        "checks": [
            _c("Inspeccionar pernos y soportes de motor y bastidor", "llave de torque", "torque segun manual, sin fisuras",
               "reapretar o cambiar soportes"),
            _c("Medir vibracion en el mando final y comparar con el limite del fabricante", "vibrometro",
               "por debajo del limite de advertencia", "sospechar rodamiento del mando final"),
            _c("Revisar el aceite del mando final por particulas metalicas", "tapon magnetico", "sin limalla",
               "desmontar y cambiar el rodamiento"),
        ],
        "safety": ["Bloquear la maquina antes de trabajar debajo del bastidor"],
    },
    "low_hydraulic_pressure": {
        "label_es": "Presion hidraulica baja", "urgency": "schedule",
        "modes": ["hydraulic_pump", "hydraulic_contamination"],
        "causes": [("Bomba hidraulica gastada", "hydraulic_pump", 0.5),
                   ("Sellos de cilindro con fuga interna", "cylinder_seals", 0.3),
                   ("Filtro hidraulico obstruido", "hydraulic_filter", 0.2)],
        "checks": [
            _c("Medir la presion de la bomba en la toma de prueba", "manometro 0-400 bar",
               "dentro del rango del fabricante", "medir caudal; sospechar bomba hidraulica"),
            _c("Inspeccionar cilindros y sellos por fugas o deriva", "visual", "sin fugas ni deriva de cilindros",
               "cambiar kit de sellos"),
            _c("Revisar el indicador de restriccion del filtro hidraulico", "visual", "indicador en verde",
               "cambiar filtro hidraulico"),
        ],
        "safety": ["Bajar implementos al suelo y despresurizar antes de intervenir"],
    },
    "smoke": {
        "label_es": "Humo", "urgency": "immediate", "modes": ["fuel_contamination", "engine_overheat"],
        "causes": [("Inyectores con falla", "injector", 0.45), ("Turbocompresor con fuga de aceite", "turbo", 0.3),
                   ("Filtro de aire obstruido", "air_filter", 0.25)],
        "checks": [
            _c("Identificar el color del humo", "visual", "sin humo visible con carga normal",
               "negro: aire o inyeccion; azul: aceite o turbo; blanco: refrigerante"),
            _c("Revisar filtro de aire e indicador de restriccion", "visual", "filtro limpio", "cambiar filtro de aire"),
            _c("Revisar juego del eje del turbo y fugas de aceite", "visual / manual", "sin juego radial ni aceite",
               "cambiar turbocompresor"),
        ],
        "safety": ["Si hay olor a quemado o llama, detener el motor y usar el extintor"],
    },
    "leak": {
        "label_es": "Fuga", "urgency": "schedule", "modes": ["hydraulic_contamination", "hydraulic_pump"],
        "causes": [("Sellos de cilindro danados", "seals", 0.5), ("Manguera fisurada", "hose", 0.4),
                   ("Conexion floja", "fittings", 0.1)],
        "checks": [
            _c("Limpiar la zona e identificar el fluido (aceite, refrigerante, combustible)", "visual",
               "sin goteo tras 10 min en ralenti", "ubicar la fuente de la fuga"),
            _c("Inspeccionar mangueras y conexiones hidraulicas", "visual / carton bajo la maquina",
               "sin fisuras ni conexiones flojas", "cambiar manguera o reapretar"),
            _c("Inspeccionar sellos de cilindros", "visual", "vastagos secos", "cambiar kit de sellos"),
        ],
        "safety": ["No buscar fugas hidraulicas con la mano: usar un carton"],
    },
    "noise": {
        "label_es": "Ruido anormal", "urgency": "observe", "modes": ["electrical_fault", "final_drive"],
        "causes": [("Correa del alternador floja o gastada", "belt", 0.4), ("Rodamiento con desgaste", "bearing", 0.4),
                   ("Escape suelto", "exhaust", 0.2)],
        "checks": [
            _c("Ubicar el origen del ruido con el motor en ralenti", "estetoscopio mecanico",
               "sin golpeteo ni chirrido", "registrar la zona del ruido"),
            _c("Revisar tension y estado de la correa del alternador", "visual / tensiometro",
               "tension segun manual, sin grietas", "cambiar la correa"),
            _c("Revisar rodamientos accesibles por juego y temperatura", "termometro infrarrojo",
               "sin juego ni sobretemperatura", "programar el cambio del rodamiento"),
        ],
        "safety": ["Mantener manos y ropa lejos de correas y poleas en movimiento"],
    },
    "warning_light": {
        "label_es": "Luz de advertencia en el tablero", "urgency": "schedule",
        "modes": ["sensor_fault", "electrical_fault"],
        "causes": [("Sensor defectuoso o desconectado", "sensor", 0.5), ("Falla electrica", "electrical", 0.3),
                   ("Condicion real fuera de rango", "operating_condition", 0.2)],
        "checks": [
            _c("Leer los codigos activos (SPN/FMI) en el tablero o con el escaner", "escaner J1939",
               "sin codigos activos", "seguir el procedimiento del SPN en el manual"),
            _c("Verificar conectores y cableado del sensor indicado", "multimetro", "continuidad y voltaje correctos",
               "reparar cableado o cambiar el sensor"),
            _c("Confirmar la lectura con un instrumento externo", "manometro / termometro", "coincide con el tablero",
               "sensor defectuoso"),
        ],
        "safety": ["Desconectar la bateria antes de manipular conectores"],
    },
    "power_loss": {
        "label_es": "Perdida de potencia", "urgency": "schedule", "modes": ["fuel_contamination", "engine_overheat"],
        "causes": [("Filtro de combustible obstruido o con agua", "fuel_filter", 0.45),
                   ("Turbocompresor con baja presion", "turbo", 0.3), ("Filtro de aire obstruido", "air_filter", 0.25)],
        "checks": [
            _c("Revisar filtro de combustible y separador de agua", "visual", "sin agua ni sedimento",
               "cambiar filtro de combustible"),
            _c("Revisar filtro de aire e indicador de restriccion", "visual", "indicador en verde",
               "cambiar filtro de aire"),
            _c("Verificar la presion de sobrealimentacion del turbo", "manometro", "segun manual",
               "revisar el turbocompresor"),
        ],
        "safety": ["Trabajar con el motor detenido y frio"],
    },
    "other": {
        "label_es": "Otro", "urgency": "observe", "modes": [],
        "causes": [("Causa no determinada: requiere inspeccion", "general_inspection", 0.3)],
        "checks": [
            _c("Inspeccion visual general (fugas, pernos, mangueras, cableado)", "visual", "sin hallazgos",
               "documentar y fotografiar"),
            _c("Leer los codigos activos en el tablero", "escaner J1939", "sin codigos activos",
               "seguir el procedimiento del SPN en el manual"),
            _c("Comparar la telemetria reciente con los rangos del fabricante", "consola FleetPulse", "dentro de rango",
               "escalar al supervisor"),
        ],
        "safety": [],
    },
}
_RANK = {u: i for i, u in enumerate(URGENCIES)}


def seed_catalog() -> list[dict]:
    """Semilla de `CATALOG#abnormality_type`: code, label_es y default_checks (pasos de la plantilla)."""
    return [{"code": code, "label_es": t["label_es"], "default_checks": [c["step"] for c in t["checks"]]}
            for code, t in TEMPLATES.items()]


def model_parts(model: str) -> set[str]:
    """Repuestos del BOM del modelo, en todos sus modos de falla."""
    return {b["part_id"] for mode in ids.load()["failure_modes"] for b in ids.bom_for(model, mode)}


def fallback(abnormality_type: str, model: str, severity_perceived: str | None = None,
             default_checks: list[str] | None = None) -> dict:
    """Plantilla local. Un tipo fuera de la tabla (agregado por el admin) usa sus `default_checks` del catalogo, o
    la plantilla `other`. Si el operario percibe severidad alta, la urgencia es al menos `schedule`."""
    tpl = TEMPLATES.get(abnormality_type)
    checks = None
    if tpl is None:
        tpl = TEMPLATES["other"]
        if default_checks:
            checks = [{"order": i, "step": s[:CAP["step"]], "tool": "", "expected": "", "if_fails": ""}
                      for i, s in enumerate(default_checks[:MAX_CHECKS], 1)]
    urgency = tpl["urgency"]
    if severity_perceived == "high" and _RANK[urgency] < _RANK["schedule"]:
        urgency = "schedule"
    parts = [p for mode in tpl["modes"] if mode in ids.load()["failure_modes"]
             for p in (b["part_id"] for b in ids.bom_for(model, mode))]
    return {
        "probable_causes": [{"cause": c, "component": comp, "confidence": conf} for c, comp, conf in tpl["causes"]],
        "checks": checks or [{"order": i, **c} for i, c in enumerate(tpl["checks"], 1)],
        "urgency": urgency,
        "suggested_parts": list(dict.fromkeys(parts)),
        "safety_notes": list(tpl["safety"]),
        "source": "template",
        "cached": False,
    }


def _text(v, cap: int) -> str:
    return v.strip()[:cap] if isinstance(v, str) else ""


def normalize(body, model: str) -> dict:
    """Respuesta de `/recommend-checks` validada y recortada; `BadResponse` si no sirve."""
    if not isinstance(body, dict):
        raise BadResponse("respuesta no es un objeto")
    causes_in, checks_in = body.get("probable_causes"), body.get("checks")
    if not isinstance(causes_in, list) or not causes_in or not isinstance(checks_in, list) or not checks_in:
        raise BadResponse("probable_causes y checks deben ser listas no vacias")
    if body.get("urgency") not in URGENCIES:
        raise BadResponse("urgency invalida")
    causes = []
    for c in causes_in[:MAX_CAUSES]:
        if isinstance(c, dict) and _text(c.get("cause"), CAP["cause"]):
            conf = c.get("confidence")
            conf = min(max(float(conf), 0.0), 1.0) if isinstance(conf, (int, float)) and not isinstance(conf, bool) \
                else None
            causes.append({"cause": _text(c["cause"], CAP["cause"]),
                           "component": _text(c.get("component"), CAP["component"]), "confidence": conf})
    checks = []
    for i, c in enumerate(checks_in, 1):
        if isinstance(c, dict) and _text(c.get("step"), CAP["step"]):
            order = c.get("order")
            checks.append({"order": order if isinstance(order, int) and not isinstance(order, bool) else i,
                           **{k: _text(c.get(k), CAP[k]) for k in ("step", "tool", "expected", "if_fails")}})
    if not causes or not checks:
        raise BadResponse("sin causas o chequeos con texto")
    checks = sorted(checks, key=lambda c: c["order"])[:MAX_CHECKS]
    bom = model_parts(model)
    parts = body.get("suggested_parts") if isinstance(body.get("suggested_parts"), list) else []
    notes = body.get("safety_notes") if isinstance(body.get("safety_notes"), list) else []
    return {
        "probable_causes": causes,
        "checks": checks,
        "urgency": body["urgency"],
        "suggested_parts": [p for p in dict.fromkeys(p for p in parts if isinstance(p, str)) if p in bom],
        "safety_notes": [t for t in (_text(n, CAP["note"]) for n in notes[:MAX_NOTES]) if t],
        "source": body.get("source") if body.get("source") in ("gemini", "template") else "gemini",
        "cached": bool(body.get("cached")),
    }


def _post(url: str, body: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def recommend(payload: dict, *, secrets=None, http=None, now=None, default_checks: list[str] | None = None) -> dict:
    """`payload` es el cuerpo de `/recommend-checks` (ver el docstring del modulo). `secrets`: cliente de Secrets
    Manager; `http(url, body, headers, timeout) -> (status, bytes)`; `default_checks`: del catalogo, para la
    plantilla de un tipo agregado por el admin."""
    ctx = {"report_id": payload.get("report_id"), "machine_id": payload.get("machine_id"),
           "incident_id": (payload.get("open_incident") or {}).get("incident_id")}
    try:
        cfg = fp_secrets.get_json(os.environ["INVOKER_SECRET"], client=secrets)
        audience = cfg["audience"].rstrip("/")
        headers = {"Authorization": f"Bearer {gcp_auth.id_token(cfg['key'], audience, now=now)}",
                   "Content-Type": "application/json"}
        status, raw = (http or _post)(f"{audience}/recommend-checks", json.dumps(payload).encode(), headers,
                                      TIMEOUT_S)
        if status != 200:
            raise BadResponse(f"http {status}")
        try:
            body = json.loads(raw)
        except ValueError:
            raise BadResponse("json invalido") from None
        out = normalize(body, payload["model"])
    except Exception as exc:  # la IA nunca bloquea el reporte
        reason = str(exc) if isinstance(exc, BadResponse) else type(exc).__name__
        log.event("recommend_fallback", reason=reason, **ctx)
        return fallback(payload["abnormality_type"], payload["model"], payload.get("severity_perceived"),
                        default_checks)
    log.event("recomendacion_ia", source=out["source"], urgency=out["urgency"], checks=len(out["checks"]),
              suggested_parts=out["suggested_parts"], cached=out["cached"], **ctx)
    return out
