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
                       reserve=[("PRT-04", 1), ("PRT-03", 2)], score=2.31)
        self._incident("EX-009", now - timedelta(minutes=25), "observe", "hydraulic_contamination", recurrence=True,
                       score=1.4)
        for mid in ("EX-003", "VQ-011", "VQ-014", "TO-002"):
            self._incident(mid, now - timedelta(minutes=12), "schedule", "fuel_contamination", tech="TEC-03",
                           group="GRP-6mc5ns-20261115T14", reserve=[("PRT-01", 1)], score=2.05)

    def _incident(self, mid, opened, sev, mode, tech=None, closed=None, parts=(), reserve=(), group=None,
                  recurrence=False, score=2.2):
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
            inc.update(status="assigned", tech_id=tech, assigned_at=_iso(at), approval="approved",
                       explanation=("Caida sostenida de presion con aceite y vibracion en alza: desgaste interno de "
                                    "la bomba hidraulica." if mode == "hydraulic_pump" else
                                    "Cuatro maquinas de la zona norte con la misma firma tras cargar el lote "
                                    "L-2311 en la estacion norte: combustible contaminado." if group else
                                    f"Firma de {mode}."),
                       explanation_src="gemini")
            timeline.append({"type": "incident.assigned", "source": "aws.incident-flow", "ts": _iso(at),
                             "payload": {"tech_id": tech}})
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

    def list_reorders(self):
        return {"reorders": list(self.reorders)}

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
