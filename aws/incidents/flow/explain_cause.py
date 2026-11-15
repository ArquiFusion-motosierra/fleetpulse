"""Lambda `explain_cause` (ExplainCause): explicacion del incidente desde Cloud Run `insights` `POST /explain`
(Gemini), o la plantilla si Cloud Run no responde bien.

Llamada: secreto `INVOKER_SECRET` (`fleetpulse/gcp/sa-invoker` = `{key, audience}`), ID token de la service
account con fplib.gcp_auth (cache 50 min) y `POST <audience>/explain` con timeout `EXPLAIN_TIMEOUT_S`. Cuerpo:
incident_id, machine_id, model, severity, deviated_vars, failure_mode, group_id, context (de CorrelateZone) y
recent_incidents. Respuesta esperada: hypotheses (texto o `{text}`, la mas probable primero), probable_component,
suggested_parts, confidence, common_cause, role_messages.
Salida: el objeto `explanation` de `incident.assigned` (source `gemini`, summary = hasta tres hipotesis,
probable_component, suggested_parts solo con repuestos del BOM del modelo y sin repetir, common_cause solo si el
incidente esta agrupado). `role_messages` no se usa: los textos por rol se arman despues de asignar (nombran al
tecnico asignado) y el contrato de `explanation` no admite campos extra.
La IA nunca bloquea la asignacion: cualquier excepcion, estado distinto de 200, JSON invalido o campos faltantes
-> `lib.templates.explanation(...)` (source `template`) y el log `explain_fallback` con el motivo (nunca la llave,
el token ni el cuerpo). Si la Lambda misma falla, el Catch de la ASL sigue a RankTechnicians sin explicacion.
Idempotencia: solo lee; AssignTechnician guarda la explicacion en el META.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from fplib import gcp_auth, ids, log
from fplib import secrets as fp_secrets
from lib import templates

EXPLAIN_TIMEOUT_S = 8
REQUIRED = ("hypotheses", "probable_component", "suggested_parts")
MAX_SUMMARY, MAX_COMPONENT, MAX_COMMON = 1000, 200, 300  # topes del contrato


class BadResponse(ValueError):
    """Respuesta de Cloud Run inutilizable; el mensaje es el motivo que se registra."""


def _post(url: str, body: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _text(hypothesis) -> str:
    if isinstance(hypothesis, dict):
        hypothesis = hypothesis.get("text")
    return hypothesis.strip() if isinstance(hypothesis, str) else ""


def _map(body, model: str, grouped: bool) -> dict:
    if not isinstance(body, dict):
        raise BadResponse("respuesta no es un objeto")
    missing = [k for k in REQUIRED if k not in body]
    if missing:
        raise BadResponse("faltan campos: " + ",".join(missing))
    hypotheses, component = body["hypotheses"], body["probable_component"]
    hypotheses = [hypotheses] if isinstance(hypotheses, str) else hypotheses
    if not isinstance(hypotheses, list) or not isinstance(body["suggested_parts"], list) or not isinstance(
            component, str) or not component.strip():
        raise BadResponse("campos con tipo invalido")
    texts = [t for t in map(_text, hypotheses) if t]
    if not texts:
        raise BadResponse("sin hipotesis")
    bom = {b["part_id"] for b in ids.load()["bom"] if b["model_id"] == model}
    parts = [p for p in dict.fromkeys(p for p in body["suggested_parts"] if isinstance(p, str)) if p in bom]
    common = body.get("common_cause")
    return {
        "source": "gemini",
        "summary": "; ".join(texts[:3])[:MAX_SUMMARY],
        "probable_component": component.strip()[:MAX_COMPONENT],
        "suggested_parts": parts,
        "common_cause": str(common)[:MAX_COMMON] if grouped and common else None,
    }


def handler(event, context=None, secrets=None, http=None):
    incident_id, machine_id, mode = event["incident_id"], event["machine_id"], event["failure_mode"]
    ctx = {"incident_id": incident_id, "machine_id": machine_id}
    inc = event.get("incident") or {}
    deviated = inc.get("deviated_vars") or event.get("deviated_vars") or []
    model = event.get("model") or templates.model_of(machine_id)
    group_id = inc.get("group_id")
    request = {
        "incident_id": incident_id, "machine_id": machine_id, "model": model,
        "severity": event["classified"]["severity"], "deviated_vars": deviated, "failure_mode": mode,
        "group_id": group_id, "context": (event.get("correlation") or {}).get("context") or {},
        "recent_incidents": [],
    }
    try:
        cfg = fp_secrets.get_json(os.environ["INVOKER_SECRET"], client=secrets)
        audience = cfg["audience"].rstrip("/")
        headers = {"Authorization": f"Bearer {gcp_auth.id_token(cfg['key'], audience)}",
                   "Content-Type": "application/json"}
        status, raw = (http or _post)(f"{audience}/explain", json.dumps(request).encode(), headers,
                                      EXPLAIN_TIMEOUT_S)
        if status != 200:
            raise BadResponse(f"http {status}")
        try:
            body = json.loads(raw)
        except ValueError:
            raise BadResponse("json invalido") from None
        out = _map(body, model, grouped=bool(group_id))
    except Exception as exc:  # la IA nunca bloquea la asignacion
        reason = str(exc) if isinstance(exc, BadResponse) else type(exc).__name__
        log.event("explain_fallback", reason=reason, **ctx)
        return templates.explanation(deviated, mode, machine_id)
    log.event("explicacion_gemini", confidence=body.get("confidence"), suggested_parts=out["suggested_parts"],
              common_cause=bool(out["common_cause"]), **ctx)
    return out
