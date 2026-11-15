"""incident-flow multi-tenant: el incidente, sus eventos, el ranking y los avisos quedan en el parque de la maquina.

CS-003 es una cosechadora del parque `agro` (zona PACKING); EX-017 es de `mineria`.
"""
import json

from incidents_support import created, get_meta, outbox_events, seed_tech

from flow import rank_technicians

CS003 = {"incident_id": "INC-CS-003-20261115T1430", "machine_id": "CS-003", "model": "CS", "zone_id": "PACKING",
         "lat": -14.08, "lon": -75.705, "tenant_id": "agro"}


def _agro(ex017):
    return {**ex017, **CS003, "failure_mode": "engine_overheat"}


def test_incident_meta_and_events_carry_the_park(ex017, ddb):
    ev = created(_agro(ex017), ddb)
    meta = get_meta(ddb.registry, CS003["incident_id"])
    assert meta["tenant_id"] == "agro" and ev["classified"]["tenant_id"] == "agro"
    assert meta["GSI3PK"] == "TENANT#agro" and meta["GSI3SK"] == f"INC#{meta['opened_at']}"
    (env,) = outbox_events(ddb.outbox, "incident.opened")
    assert env["tenant_id"] == "agro"


def test_legacy_input_without_tenant_is_mineria(ex017, ddb):
    created(ex017, ddb)
    assert get_meta(ddb.registry, ex017["incident_id"])["GSI3PK"] == "TENANT#mineria"


def test_ranking_never_crosses_parks(ex017, ddb):
    # una tecnica de mineria a 0 km de la maquina de agro no compite: solo cuentan los tecnicos de agro
    seed_tech(ddb.state, "TEC-03", "engine", CS003["lat"], CS003["lon"], zone="NORTE")
    top = rank_technicians.handler(created(_agro(ex017), ddb), state=ddb.state)["top"]
    assert top and {t["tech_id"] for t in top} <= {"TEC-11", "TEC-12", "TEC-13", "TEC-14"}
    assert "TEC-03" not in {t["tech_id"] for t in top} and top[0]["tech_id"] == "TEC-14"  # PACKING, a 0 km


def test_every_role_notification_is_tagged_with_the_park():
    from pathlib import Path
    asl = json.loads((Path(__file__).resolve().parents[1] / "statemachine" / "incident-flow.asl.json").read_text())
    found = []

    def walk(states):
        for st in states.values():
            attrs = (st.get("Parameters") or {}).get("MessageAttributes") or {}
            if "role" in attrs:
                found.append(attrs["tenant_id"])
            for b in st.get("Branches", []):
                walk(b["States"])
    walk(asl["States"])
    assert len(found) == 7
    assert all(a == {"DataType": "String", "StringValue.$": "$.classified.tenant_id"} for a in found)
