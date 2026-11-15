"""Reportes del operario y administracion en modo de prueba (MOCK=1), con la misma forma y las mismas reglas de rol
que aws/reports (T32, PR #15) y las rutas de administracion de T35 (backlog #33).

Usuarios de prueba: el token es "mock.<rol>"; no hay contrasenas. En modo real el BFF reenvia el ID token de Cognito.
"""
from __future__ import annotations

from datetime import datetime, timezone

URGENCIES = ("observe", "schedule", "immediate")

USERS = {
    "operator": {"email": "operador@fleetpulse.test", "role": "operator", "name": "Operador (Norte)",
                 "tenant_id": "mineria"},
    "technician": {"email": "tecnico@fleetpulse.test", "role": "technician", "tech_id": "TEC-01", "name": "Tecnico TEC-01",
                   "tenant_id": "mineria"},
    "supervisor": {"email": "supervisor@fleetpulse.test", "role": "supervisor", "name": "Supervisor de mantenimiento",
                   "tenant_id": "mineria"},
    "management": {"email": "direccion@fleetpulse.test", "role": "management", "name": "Direccion",
                   "tenant_id": "mineria"},
    "admin": {"email": "admin@fleetpulse.test", "role": "admin", "name": "Administrador", "tenant_id": "mineria"},
    # Multi-tenancy: un admin de otro parque y el jefe de mantenimiento (ve todos los parques)
    "admin_agro": {"email": "admin.agro@fleetpulse.test", "role": "admin", "name": "Administrador (agro)",
                   "tenant_id": "agro"},
    "maintenance_head": {"email": "jefe.mantenimiento@fleetpulse.test", "role": "maintenance_head",
                         "name": "Jefe de mantenimiento", "tenant_id": None},
}
CREATE_ROLES = ("operator", "supervisor", "admin", "maintenance_head")
CLOSE_ROLES = ("technician", "supervisor", "admin", "maintenance_head")


def _c(step, tool, expected, if_fails):
    return {"step": step, "tool": tool, "expected": expected, "if_fails": if_fails}


# Plantillas de prueba (resumen de las de aws/reports/lib/recommend.py)
TEMPLATES = {
    "high_engine_temp": {"label_es": "Temperatura alta de motor", "urgency": "immediate", "mode": "head_gasket",
                         "causes": [("Junta de culata con fuga", "head_gasket", 0.4), ("Termostato pegado", "thermostat", 0.3)],
                         "checks": [_c("Verificar nivel y color del refrigerante con el motor frio", "visual",
                                       "nivel en la marca, sin aceite ni espuma", "sospechar junta de culata"),
                                    _c("Prueba de presion del sistema de refrigeracion", "bomba manual 1,2 bar",
                                       "mantiene la presion 10 min", "fuga interna: revisar junta de culata")],
                         "safety": ["No abrir la tapa del radiador con el motor caliente"]},
    "high_hydraulic_temp": {"label_es": "Temperatura alta de aceite hidraulico", "urgency": "schedule",
                            "mode": "hydraulic_overheat",
                            "causes": [("Enfriador de aceite obstruido", "oil_cooler", 0.5)],
                            "checks": [_c("Limpiar y revisar el enfriador de aceite", "visual", "aletas limpias",
                                          "lavar o reemplazar el enfriador")],
                            "safety": ["Despresurizar el circuito antes de abrir conexiones"]},
    "abnormal_vibration": {"label_es": "Vibracion anormal", "urgency": "schedule", "mode": "final_drive",
                           "causes": [("Rodamiento del mando final", "final_drive", 0.5)],
                           "checks": [_c("Medir vibracion en el mando final", "vibrometro", "bajo 7,5 mm/s",
                                         "analizar aceite del mando final")],
                           "safety": ["Bloquear la maquina antes de trabajar debajo del bastidor"]},
    "low_hydraulic_pressure": {"label_es": "Presion hidraulica baja", "urgency": "schedule", "mode": "hydraulic_pump",
                               "causes": [("Bomba hidraulica gastada", "hydraulic_pump", 0.5)],
                               "checks": [_c("Medir presion y caudal de la bomba", "manometro", "dentro de la ficha",
                                             "reemplazar bomba y sellos")],
                               "safety": ["Bajar implementos al suelo y despresurizar antes de intervenir"]},
    "smoke": {"label_es": "Humo", "urgency": "immediate", "mode": "fuel_contamination",
              "causes": [("Combustible contaminado", "fuel", 0.4)],
              "checks": [_c("Revisar filtro y muestra de combustible", "visual", "sin agua ni sedimento",
                            "cambiar filtro y drenar tanque")],
              "safety": ["Si hay olor a quemado o llama, detener el motor y usar el extintor"]},
    "other": {"label_es": "Otro", "urgency": "observe", "mode": None,
              "causes": [("Por determinar en la inspeccion", "unknown", 0.2)],
              "checks": [_c("Inspeccion visual general", "visual", "sin danos visibles", "escalar al supervisor")],
              "safety": ["Detener la maquina si la anormalidad aumenta"]},
}
TYPE_SEED = ["high_engine_temp", "high_hydraulic_temp", "abnormal_vibration", "low_hydraulic_pressure", "smoke",
             "leak", "noise", "warning_light", "power_loss", "other"]
EXTRA_LABELS = {"leak": "Fuga", "noise": "Ruido anormal", "warning_light": "Luz de advertencia",
                "power_loss": "Perdida de potencia"}
SEVERITY_FOR_URGENCY = {"observe": "observe", "schedule": "schedule", "immediate": "immediate"}


class Forbidden(Exception):
    pass


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class BadRequest(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def user_of(token: str | None) -> dict:
    """'Bearer mock.<rol>' -> usuario de prueba."""
    if not token:
        raise PermissionError("unauthorized")
    raw = token.removeprefix("Bearer ").strip()
    user = USERS.get(raw.removeprefix("mock.")) if raw.startswith("mock.") else None
    if not user:
        raise PermissionError("unauthorized")
    return user


def require(user: dict, roles: tuple[str, ...]) -> None:
    if user["role"] not in roles:
        raise Forbidden(f"requiere rol: {', '.join(roles)}")


class MockReports:
    def __init__(self, registry):
        self.registry = registry  # MockRegistry: maquinas, incidentes y stock
        self.reports: dict[str, dict] = {}
        self.types = {code: {"code": code, "label_es": TEMPLATES.get(code, {}).get("label_es") or EXTRA_LABELS[code],
                             "default_checks": [c["step"] for c in TEMPLATES.get(code, TEMPLATES["other"])["checks"]]}
                      for code in TYPE_SEED}
        techs = registry.ids["technicians"]
        self.techs = {t["tech_id"]: {**t, "available": t["tech_id"] != "TEC-03"} for t in techs}

    # ---------- reportes (T32) ----------
    def recommend(self, kind: str, model: str, severity: str) -> dict:
        tpl = TEMPLATES.get(kind, TEMPLATES["other"])
        urgency = tpl["urgency"]
        if severity == "high" and URGENCIES.index(urgency) < 1:
            urgency = "schedule"
        parts = [b["part_id"] for b in self.registry.ids["bom"] if tpl["mode"] and b["model_id"] == model
                 and b["failure_mode"] == tpl["mode"]]
        return {"probable_causes": [{"cause": c, "component": comp, "confidence": conf} for c, comp, conf in tpl["causes"]],
                "checks": [{"order": i, **c} for i, c in enumerate(tpl["checks"], 1)], "urgency": urgency,
                "suggested_parts": parts, "safety_notes": list(tpl["safety"]), "source": "template", "cached": False}

    def create(self, user: dict, body: dict) -> dict:
        require(user, CREATE_ROLES)
        mid, kind = body.get("machine_id"), body.get("abnormality_type")
        if mid not in self.registry.machines:
            raise BadRequest("unknown machine_id")
        if kind not in self.types:
            raise BadRequest("unknown abnormality_type")
        if body.get("severity_perceived") not in ("low", "medium", "high") or not body.get("description"):
            raise BadRequest("description y severity_perceived son obligatorios")
        m = self.registry.machines[mid]
        now = datetime.now(timezone.utc)
        rid = f"RPT-{mid}-{now:%Y%m%dT%H%M%S}"
        rec = self.recommend(kind, m["model"], body["severity_perceived"])
        inc_id, link, tech = m.get("open_incident_id"), "linked", None
        if inc_id:
            tech = self.registry.incidents[inc_id].get("tech_id")
        elif rec["urgency"] in ("schedule", "immediate"):
            tpl = TEMPLATES.get(kind, TEMPLATES["other"])
            inc = self.registry._incident(mid, now, SEVERITY_FOR_URGENCY[rec["urgency"]], tpl["mode"] or "sensor_fault",
                                          tech="TEC-01", score=2.0)
            inc_id, link, tech = inc["incident_id"], "started", "TEC-01"
        else:
            link = "none"
        item = {"report_id": rid, "machine_id": mid, "model": m["model"], "zone_id": m["zone_id"],
                "abnormality_type": kind, "description": body["description"][:1000],
                "severity_perceived": body["severity_perceived"], "reported_by": user["email"], "reported_at": _now(),
                "status": "in_review" if tech else "open", "recommendation": rec, "incident_id": inc_id,
                "incident_link": link, "assigned_tech_id": tech,
                "timeline": [{"type": "report.created", "source": "aws.reports-api", "ts": _now(),
                              "payload": {"urgency": rec["urgency"], "incident_id": inc_id}}]}
        self.reports[rid] = item
        if inc_id:
            self.registry.incidents[inc_id]["timeline"].append(
                {"type": "report.linked", "source": "aws.reports-api", "ts": _now(), "payload": {"report_id": rid}})
        return {k: v for k, v in item.items() if k != "timeline"}

    def _mine(self, item: dict, user: dict) -> bool:
        return item["reported_by"] == user["email"] or (user.get("tech_id") and item.get("assigned_tech_id") == user["tech_id"])

    def list(self, user: dict, status=None, machine_id=None, incident_id=None, mine=False) -> list[dict]:
        rows = [r for r in self.reports.values()
                if (not status or r["status"] == status) and (not machine_id or r["machine_id"] == machine_id)
                and (not incident_id or r["incident_id"] == incident_id) and (not mine or self._mine(r, user))]
        return [{k: v for k, v in r.items() if k != "timeline"} for r in sorted(rows, key=lambda r: r["reported_at"], reverse=True)]

    def get(self, user: dict, rid: str) -> dict:
        r = self.reports.get(rid)
        if not r:
            raise NotFound("report not found")
        return {"report": {k: v for k, v in r.items() if k != "timeline"}, "timeline": list(r["timeline"])}

    def close(self, user: dict, rid: str, body: dict) -> dict:
        require(user, CLOSE_ROLES)
        r = self.reports.get(rid)
        if not r:
            raise NotFound("report not found")
        if r["status"] == "closed":
            raise Conflict("report already closed")
        if user["role"] == "technician" and r.get("assigned_tech_id") not in (None, user.get("tech_id")):
            raise Forbidden("el reporte esta asignado a otro tecnico")
        if not body.get("findings") or not body.get("confirmed_cause"):
            raise BadRequest("findings y confirmed_cause son obligatorios")
        r.update(status="closed", closed_at=_now(), closed_by=user.get("tech_id") or user["email"],
                 findings=body["findings"][:2000], confirmed_cause=body["confirmed_cause"][:300],
                 parts_used=body.get("parts_used") or [])
        r["timeline"].append({"type": "report.closed", "source": "aws.reports-api", "ts": _now(),
                              "payload": {"confirmed_cause": r["confirmed_cause"]}})
        if r.get("incident_id") in self.registry.incidents:
            self.registry.incidents[r["incident_id"]]["timeline"].append(
                {"type": "report.closed", "source": "aws.reports-api", "ts": _now(), "payload": {"report_id": rid}})
        return {"report": {k: v for k, v in r.items() if k != "timeline"}, "incident_event": bool(r.get("incident_id"))}

    def list_types(self, user: dict) -> list[dict]:
        return list(self.types.values())

    # ---------- administracion (T35, misma forma que aws/reports/api/admin.py) ----------
    def list_techs(self, user: dict) -> dict:
        require(user, ("admin",))
        park = user.get("tenant_id") or "mineria"
        zones = sorted(z["zone_id"] for z in self.registry.ids["zones"] if z.get("tenant_id", "mineria") == park)
        return {"technicians": [t for t in self.techs.values() if t.get("tenant_id", "mineria") == park],
                "tenant_id": park, "allowed": {"zone_id": zones,
                                               "specialty": ["electrical", "engine", "hydraulics", "transmission"]}}

    def get_tech(self, user: dict, tech_id: str) -> dict:
        require(user, ("admin",))
        if tech_id not in self.techs:
            raise NotFound("technician not found")
        return self.techs[tech_id]

    def put_tech(self, user: dict, tech_id: str, body: dict) -> dict:
        t = self.get_tech(user, tech_id)
        if "available" in body:
            t["available"] = bool(body["available"])
        if body.get("zone_id") in ("NORTE", "CENTRO", "SUR"):
            t["zone_id"] = body["zone_id"]
        if body.get("specialty") in ("hydraulics", "engine", "electrical", "transmission"):
            t["specialty"] = body["specialty"]
        return {"technician": t}

    def add_type(self, user: dict, body: dict) -> dict:
        require(user, ("admin",))
        code = str(body.get("code") or "").strip()
        if not code.replace("_", "").isalnum() or not code[:1].isalpha() or not body.get("label_es"):
            raise BadRequest("code (minusculas y _) y label_es son obligatorios")
        created = code not in self.types  # como admin.py: crea (201) o reemplaza (200)
        self.types[code] = {"code": code, "label_es": body["label_es"][:80],
                            "default_checks": [s[:200] for s in body.get("default_checks") or []][:6]}
        return created, self.types[code]

    def delete_type(self, user: dict, code: str) -> None:
        require(user, ("admin",))
        if code not in self.types:
            raise NotFound("abnormality type not found")
        del self.types[code]
