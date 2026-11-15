"""Lambda `classify` (ClassifySeverity): severidad del incidente a partir de `score` y si requiere aprobacion.

Umbrales de contracts/README.md (iguales a aws/scoring): observe >= 1.2, schedule >= 2.0, immediate >= 3.5. Una
ejecucion solo existe con score >= 2.0; por debajo de 1.2 se clasifica `observe` para no salir del contrato.
`tenant_id`: parque de la maquina (entrada de `score`, o el catalogo si la entrada es anterior al multi-tenancy). Lo
usan los avisos SNS (atributo `tenant_id` de `$.classified.tenant_id`) para que cada parque reciba solo lo suyo.
Idempotencia: funcion pura del input.
"""
from __future__ import annotations

from fplib import ids, log

THRESHOLDS = (("immediate", 3.5), ("schedule", 2.0), ("observe", 1.2))


def severity(score: float) -> str:
    return next((name for name, low in THRESHOLDS if score >= low), "observe")


def handler(event, context=None):
    sev = severity(float(event["score"]))
    out = {"severity": sev, "needs_approval": sev == "schedule",
           "tenant_id": event.get("tenant_id") or ids.tenant_of_machine(event["machine_id"])}
    log.event("clasificado", incident_id=event["incident_id"], machine_id=event["machine_id"],
              score=event["score"], **out)
    return out
