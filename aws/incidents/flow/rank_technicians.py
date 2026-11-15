"""Lambda `rank_technicians` (RankTechnicians): los mejores tecnicos para el incidente en `$.ranking.top`.

Idempotencia: solo lee fp-state; mismo estado, mismo ranking.
"""
from __future__ import annotations

from fplib import log
from lib import registry as fp_registry
from lib import technicians

TOP_N = 3


def handler(event, context=None, state=None):
    state = state or fp_registry.table_from_env("STATE_TABLE")
    cands = technicians.candidates(state, event["incident"]["zone_id"])
    top = technicians.rank(cands, float(event["lat"]), float(event["lon"]), event["classified"]["severity"],
                           event["failure_mode"])[:TOP_N]
    log.event("tecnicos_rankeados", incident_id=event["incident_id"], machine_id=event["machine_id"],
              candidates=len(cands), top=[t["tech_id"] for t in top])
    return {"top": top}
