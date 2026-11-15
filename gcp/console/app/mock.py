"""Datos de prueba para desarrollar la consola sin AWS (MOCK=1). Misma forma que la API de aws/registry
(contracts/secrets.md). Sale del mismo catalogo que el simulador: fleet.json se genera de contracts/ids.yaml.
"""
from __future__ import annotations

import copy
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# Informes tecnicos de ejemplo (explanation.report, T26/T28), contra contracts/oem_specs.yaml (ilustrativa)
REPORT_EX017 = {
    "summary": "Presion hidraulica bajo el minimo del fabricante con aceite y vibracion en alza: bomba gastada.",
    "findings": [
        {"parameter": "hydraulic_pressure_bar", "value": 268.4, "unit": "bar", "status": "warning",
         "oem_limit": "normal 280-345 bar; advertencia bajo 270; critico bajo 250"},
        {"parameter": "hydraulic_oil_temp_c", "value": 71.9, "unit": "C", "status": "normal",
         "oem_limit": "normal 35-75 C; advertencia 82 C", "note": "En alza: +12 C en 15 minutos"},
        {"parameter": "vibration_mm_s", "value": 9.8, "unit": "mm/s", "status": "warning",
         "oem_limit": "normal hasta 9,0 mm/s; advertencia 11,2"}],
    "probable_cause": "Desgaste interno de la bomba hidraulica: pierde caudal y presion, y el aceite se calienta.",
    "failure_mode": "hydraulic_pump",
    "resonance": None,
    "recommended_action": "Detener a la brevedad; medir caudal y presion de la bomba; reemplazar bomba y sellos.",
    "parts": [{"part_id": "PRT-04", "qty": 1}, {"part_id": "PRT-03", "qty": 2}],
    "oem_reference": "Ficha EX v1 (ilustrativa): limites hidraulicos y falla tipica 'bomba hidraulica gastada'",
}
REPORT_VQ022 = {
    "summary": "Refrigerante sobre la advertencia, perdida de refrigerante (SPN 111) y resonancia del chasis por rpm elevadas.",
    "findings": [
        {"parameter": "coolant_temp_c", "value": 104.2, "unit": "C", "status": "warning",
         "oem_limit": "normal 65-97 C; advertencia 103 C; critico 108 C"},
        {"parameter": "engine_rpm", "value": 1868, "unit": "rpm", "status": "warning",
         "oem_limit": "nominal 1650 rpm; maximo 2000 rpm", "note": "Inestable, +13 % sobre la nominal"},
        {"parameter": "vibration_mm_s", "value": 8.6, "unit": "mm/s", "status": "warning",
         "oem_limit": "normal hasta 7,5 mm/s; advertencia 11,2", "note": "Salta cuando el 2x cruza los 62 Hz"}],
    "probable_cause": "Perdida de compresion por la junta de culata: el regulador sube las rpm y el 2x entra en "
                      "resonancia con el segundo modo del chasis.",
    "failure_mode": "head_gasket",
    "resonance": {"harmonic": "2x", "frequency_hz": 62.3, "natural_frequency_hz": 62,
                  "component": "Chasis y tolva (segundo modo)"},
    "recommended_action": "Detener y enfriar; revisar nivel y presion del sistema de refrigeracion; prueba de "
                          "compresion; reemplazar la junta.",
    "parts": [{"part_id": "PRT-13", "qty": 1}],
    "oem_reference": "Ficha VQ v1 (ilustrativa): limites de refrigerante y modo 3 del chasis",
}


class MockRegistry:
    def __init__(self, now: datetime | None = None, clock=time.monotonic):
        with open(os.path.join(HERE, "fleet.json"), encoding="utf-8") as f:
            self.ids = json.load(f)
        self.clock = clock
        self.started = clock()
        now = now or datetime.now(timezone.utc).replace(microsecond=0)
        rng = random.Random(19)
        zones = {z["zone_id"]: z for z in self.ids["zones"]}
        self.machines = {}
        for m in self.ids["machines"]:
            z = zones[m["zone_id"]]
            self.machines[m["machine_id"]] = {
                "machine_id": m["machine_id"], "model": m["model_id"], "zone_id": m["zone_id"],
                "lat": round(z["lat"] + rng.uniform(-0.007, 0.007), 6),
                "lon": round(z["lon"] + rng.uniform(-0.007, 0.007), 6),
                "status": rng.choices(["operating", "idle", "off"], [0.72, 0.12, 0.16])[0],
                "severity": "ok", "score": round(rng.uniform(0.1, 0.9), 2), "hours_total": m["hours_total"],
                "fuel_remaining_ratio": round(rng.uniform(0.2, 0.95), 2), "open_incident_id": None,
                "updated_at": _iso(now), "serial": m["serial"], "commissioned_at": m["commissioned_at"],
            }
        parts = {p["part_id"]: p for p in self.ids["parts"]}
        self.stock = {}
        for s in self.ids["part_stock"]:
            for z in self.ids["zones"]:
                self.stock[(s["part_id"], z["zone_id"])] = {
                    "part_id": s["part_id"], "zone_id": z["zone_id"], "sku": parts[s["part_id"]]["sku"],
                    "name": parts[s["part_id"]]["name"], "on_hand": s["on_hand_per_zone"], "reserved": 0,
                    "reorder_point": s["reorder_point"]}
        self.incidents: dict[str, dict] = {}
        self.reorders: list[dict] = []
        # Historial de EX-009 (S5): incidente hidraulico hace 10 dias
        past = now - timedelta(days=10)
        self._incident("EX-009", past, "schedule", "hydraulic_contamination", tech="TEC-02",
                       closed=past + timedelta(hours=3), parts=[("PRT-03", 2), ("PRT-02", 1)])
        # En vivo: EX-017 (S1), reincidencia de EX-009 (S5) y el grupo de la zona norte (S2)
        self._incident("EX-017", now - timedelta(minutes=6), "schedule", "hydraulic_pump", tech="TEC-01",
                       reserve=[("PRT-04", 1), ("PRT-03", 2)], score=2.31, distance_km=0.4, report=REPORT_EX017)
        # S7 (T27): junta de culata en VQ-022 con resonancia del chasis
        self._incident("VQ-022", now - timedelta(minutes=4), "schedule", "head_gasket", tech="TEC-03",
                       reserve=[("PRT-13", 1)], score=2.6, distance_km=1.1, report=REPORT_VQ022)
        self._incident("EX-009", now - timedelta(minutes=25), "observe", "hydraulic_contamination", recurrence=True,
                       score=1.4)
        for mid in ("EX-003", "VQ-011", "VQ-014", "TO-002"):
            self._incident(mid, now - timedelta(minutes=12), "schedule", "fuel_contamination", tech="TEC-03",
                           group="GRP-6mc5ns-20261115T14", reserve=[("PRT-01", 1)], score=2.05)

    def _incident(self, mid, opened, sev, mode, tech=None, closed=None, parts=(), reserve=(), group=None,
                  recurrence=False, score=2.2, distance_km=None, report=None):
        m = self.machines[mid]
        window = opened.replace(minute=opened.minute - opened.minute % 15, second=0)
        inc_id = f"INC-{mid}-{window.strftime('%Y%m%dT%H%M')}"
        timeline = [{"type": "incident.opened", "source": "aws.incident-flow", "ts": _iso(opened),
                     "payload": {"severity": sev, "score": score, "failure_mode": mode}}]
        inc = {"incident_id": inc_id, "machine_id": mid, "zone_id": m["zone_id"], "group_id": group or "-",
               "severity": sev, "status": "open", "score": score, "failure_mode": mode, "opened_at": _iso(opened),
               "recurrence_flag": recurrence, "lat": m["lat"], "lon": m["lon"], "timeline": timeline,
               "reservations": []}
        if tech:
            at = opened + timedelta(minutes=2)
            inc.update(tech_id=tech, assigned_at=_iso(at), approval="approved",
                       explanation=("Caida sostenida de presion con aceite y vibracion en alza: desgaste interno de "
                                    "la bomba hidraulica." if mode == "hydraulic_pump" else
                                    "Cuatro maquinas de la zona norte con la misma firma tras cargar el lote "
                                    "L-2311 en la estacion norte: combustible contaminado." if group else
                                    report["summary"] if report else f"Firma de {mode}."),
                       explanation_src="gemini")
            # Misma forma que el payload de incident.assigned (contracts/incident.schema.json)
            assigned = {"tech_id": tech, "assigned_at": _iso(at), "approval": "approved",
                        "explanation": {"source": "gemini", "summary": inc["explanation"]}}
            if distance_km is not None:
                assigned["distance_km"] = distance_km
            if report:
                assigned["explanation"]["report"] = report
            timeline.append({"type": "incident.assigned", "source": "aws.incident-flow", "ts": _iso(at),
                             "payload": assigned})
        for i, (part, qty) in enumerate(reserve):
            s = self.stock[(part, m["zone_id"])]
            zone = m["zone_id"]
            if s["on_hand"] - s["reserved"] < qty:
                zone = next(z for (p, z), r in self.stock.items() if p == part and r["on_hand"] - r["reserved"] >= qty)
                s = self.stock[(part, zone)]
            s["reserved"] += qty
            inc["reservations"].append({"reservation_id": f"mock-{inc_id}#{i}", "part_id": part, "zone_id": zone,
                                        "qty": qty, "status": "reserved"})
        if reserve:
            timeline.append({"type": "part.reserved", "source": "aws.registry", "ts": _iso(opened + timedelta(minutes=3)),
                             "payload": {"reservations": inc["reservations"]}})
        if closed:
            inc.update(status="closed", closed_at=_iso(closed),
                       downtime_h=round((closed - opened).total_seconds() / 3600, 2),
                       parts_used=[{"part_id": p, "zone_id": m["zone_id"], "qty": q} for p, q in parts],
                       cause_summary="Sellos de cilindro cambiados y filtro hidraulico reemplazado.")
            timeline.append({"type": "incident.closed", "source": "aws.incidents-api", "ts": _iso(closed),
                             "payload": {"downtime_h": inc["downtime_h"]}})
        else:
            m.update(severity=sev, score=score, open_incident_id=inc_id)
        self.incidents[inc_id] = inc
        return inc

    def _live(self):
        """EX-017 escala sola: 'immediate' a los 3 minutos de levantar el mock (para ver el refresco del mapa)."""
        if self.clock() - self.started > 180:
            m = self.machines["EX-017"]
            if m["open_incident_id"] and m["severity"] == "schedule":
                m.update(severity="immediate", score=3.62)
                inc = self.incidents[m["open_incident_id"]]
                inc.update(severity="immediate", score=3.62)
                inc["timeline"].append({"type": "incident.escalated", "source": "aws.incident-flow",
                                        "ts": _iso(datetime.now(timezone.utc)),
                                        "payload": {"from_severity": "schedule", "to_severity": "immediate"}})

    # ---- misma forma que aws/registry/reg/api.py ----
    def list_machines(self):
        self._live()
        return {"machines": [{k: v for k, v in m.items() if k not in ("serial", "commissioned_at")}
                             for m in sorted(self.machines.values(), key=lambda m: m["machine_id"])]}

    def machine(self, mid):
        self._live()
        m = self.machines.get(mid)
        if not m:
            return None
        incs = sorted((self._brief(i) for i in self.incidents.values() if i["machine_id"] == mid),
                      key=lambda i: i["opened_at"], reverse=True)
        return {**m, "state": {"status": m["status"], "lat": m["lat"], "lon": m["lon"]}, "incidents": incs,
                "recurrences": sum(1 for i in incs if i.get("recurrence_flag"))}

    @staticmethod
    def _brief(i):
        return {k: v for k, v in i.items() if k not in ("timeline", "reservations")}

    def list_incidents(self, status=None, limit=50):
        self._live()
        if status == "assigned":  # como la API real: abierto con tecnico
            rows = [self._brief(i) for i in self.incidents.values() if i["status"] == "open" and i.get("tech_id")]
        else:
            rows = [self._brief(i) for i in self.incidents.values() if not status or i["status"] == status]
        return {"incidents": sorted(rows, key=lambda i: i["opened_at"], reverse=True)[:limit]}

    def incident(self, inc_id):
        self._live()
        i = self.incidents.get(inc_id)
        return copy.deepcopy(i) if i else None

    def list_stock(self):
        rows = []
        for s in self.stock.values():
            avail = s["on_hand"] - s["reserved"]
            rows.append({**s, "available": avail, "below_reorder": avail <= s["reorder_point"]})
        return {"stock": rows}

    # ---- T19: KPIs, ordenes preventivas y pedidos sugeridos ----
    def kpis(self, day=None):
        """KPIs del dia anterior con los rasgos del simulador: S3 (CF-042 en ralenti), S6 (VQ-031..035 2 h, MN-003 20 h)."""
        rng = random.Random(15)
        day = day or (datetime.now(timezone.utc) - timedelta(hours=5) - timedelta(days=1)).strftime("%Y-%m-%d")
        base_fuel = {m["model_id"]: m["fuel_lph_base"] for m in self.ids["models"]}
        rows = []
        for mid, m in sorted(self.machines.items()):
            hours = 2.0 if mid in {"VQ-031", "VQ-032", "VQ-033", "VQ-034", "VQ-035"} else                 20.0 if mid == "MN-003" else rng.uniform(9.0, 13.5)
            idle = 0.45 if mid == "CF-042" else rng.uniform(0.08, 0.2)
            peers = 21.0 if mid == "CF-042" else 9.0 if mid == "MN-005" else rng.gauss(0, 4.5)
            fuel = base_fuel[m["model"]] * 0.75 * (1 + peers / 100)
            rows.append({"machine_id": mid, "operating_h": round(hours * (1 - idle), 2), "idle_ratio": round(idle, 3),
                         "utilization": round(hours * (1 - idle) / 24, 3), "fuel_lph": round(fuel, 2),
                         "fuel_vs_peers_pct": round(peers, 1), "cost_per_h": round(fuel * 1.15 + 18, 2)})
        for r in rows:
            high_use, costly = r["utilization"] >= 0.35, r["fuel_vs_peers_pct"] > 5
            r["quadrant"] = ("workhorse" if costly else "star") if high_use else ("inefficient" if costly else "underused")
        return {"day": day, "machines": rows}

    def list_orders(self):
        interval = {m["model_id"]: m["service_interval_h"] for m in self.ids["models"]}
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        rows = []
        for m in self.ids["machines"]:
            left = interval[m["model_id"]] - m["hours_since_service"]
            if left <= 40:
                due = now + timedelta(hours=max(left, 0) * 2)  # ~12 h de operacion por dia
                rows.append({"order_id": f"ORD-{m['machine_id']}-{due:%Y%m%d}", "machine_id": m["machine_id"],
                             "kind": "preventive", "status": "suggested", "due_at": _iso(due),
                             "hours_since_service": m["hours_since_service"],
                             "service_interval_h": interval[m["model_id"]],
                             "reason": f"{m['hours_since_service']} h desde el ultimo servicio "
                                       f"(intervalo {interval[m['model_id']]} h)"})
        return {"orders": sorted(rows, key=lambda r: r["due_at"])}

    def list_reorders(self):
        now = _iso(datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0))
        base = [{"part_id": "PRT-01", "zone_id": "NORTE", "qty": 8, "source": "forecast", "forecast_14d": 9.4,
                 "created_at": now},
                {"part_id": "PRT-03", "zone_id": "NORTE", "qty": 4, "source": "forecast", "forecast_14d": 5.1,
                 "created_at": now}]
        return {"reorders": list(self.reorders) + base}

    def close(self, inc_id, body):
        i = self.incidents.get(inc_id)
        if not i:
            return None
        if i["status"] == "closed":
            return {"incident_id": inc_id, "status": "closed"}
        now = datetime.now(timezone.utc).replace(microsecond=0)
        opened = datetime.fromisoformat(i["opened_at"].replace("Z", "+00:00"))
        used = body.get("parts_used") or [{"part_id": r["part_id"], "zone_id": r["zone_id"], "qty": r["qty"]}
                                          for r in i["reservations"]]
        for r in i["reservations"]:
            self.stock[(r["part_id"], r["zone_id"])]["reserved"] -= r["qty"]
            r["status"] = "consumed"
        for u in used:
            self.stock[(u["part_id"], u["zone_id"])]["on_hand"] -= u["qty"]
        i.update(status="closed", closed_at=_iso(now), downtime_h=round((now - opened).total_seconds() / 3600, 2),
                 parts_used=used, cause_summary=body.get("cause_summary") or "-")
        i["timeline"].append({"type": "incident.closed", "source": "aws.incidents-api", "ts": _iso(now),
                              "payload": {"closed_by": body.get("closed_by")}})
        m = self.machines[i["machine_id"]]
        if m["open_incident_id"] == inc_id:
            m.update(severity="ok", score=0.3, open_incident_id=None)
        return {"incident_id": inc_id, "status": "closed"}
