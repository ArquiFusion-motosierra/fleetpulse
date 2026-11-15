#!/usr/bin/env python3
"""Genera oci/sql/05_seed.sql desde contracts/ids.yaml (fuente unica de IDs).

    python oci/sql/gen_seed.py
"""
import os

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
IDS = os.path.join(HERE, "..", "..", "contracts", "ids.yaml")
OUT = os.path.join(HERE, "05_seed.sql")


def q(v) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def main() -> None:
    with open(IDS, encoding="utf-8") as f:
        ids = yaml.safe_load(f)
    zones = {z["zone_id"]: z for z in ids["zones"]}
    L = [
        "-- FleetPulse OCI - 05_seed.sql. GENERADO por oci/sql/gen_seed.py desde contracts/ids.yaml: no editar a mano.",
        "-- Ejecutar como FLEET (o ADMIN) despues de 01_schema.sql y ANTES de cargar incidentes (las FK lo impiden despues).",
        "-- Recargable: borra y vuelve a cargar los catalogos.",
        "WHENEVER SQLERROR EXIT SQL.SQLCODE",
        "ALTER SESSION SET CURRENT_SCHEMA = fleet;",
    ]
    for table in ("part_stock", "bom", "machine_status", "technicians", "parts", "machines", "machine_models",
                  "zones"):
        L.append(f"DELETE FROM {table};")
    for z in ids["zones"]:
        L.append(f"INSERT INTO zones VALUES ({q(z['zone_id'])}, {q(z['name'])}, {z['lat']}, {z['lon']}, "
                 f"{z['altitude_m']});")
    for m in ids["models"]:
        L.append(f"INSERT INTO machine_models VALUES ({q(m['model_id'])}, {q(m['name'])}, {m['service_interval_h']});")
    for m in ids["machines"]:
        z = zones[m["zone_id"]]
        L.append(f"INSERT INTO machines VALUES ({q(m['machine_id'])}, {q(m['model_id'])}, {q(m['serial'])}, "
                 f"{q(m['zone_id'])}, DATE {q(m['commissioned_at'])});")
        L.append(f"INSERT INTO machine_status (machine_id, status, severity, lat, lon, zone_id, hours_total, updated_at) "
                 f"VALUES ({q(m['machine_id'])}, 'off', 'ok', {z['lat']}, {z['lon']}, {q(m['zone_id'])}, "
                 f"{m['hours_total']}, SYSTIMESTAMP);")
    for t in ids["technicians"]:
        L.append(f"INSERT INTO technicians (tech_id, name, specialty, zone_id) VALUES ({q(t['tech_id'])}, "
                 f"{q(t['name'])}, {q(t['specialty'])}, {q(t['zone_id'])});")
    for p in ids["parts"]:
        L.append(f"INSERT INTO parts VALUES ({q(p['part_id'])}, {q(p['sku'])}, {q(p['name'])}, {p['unit_cost_usd']});")
    for b in ids["bom"]:
        L.append(f"INSERT INTO bom VALUES ({q(b['model_id'])}, {q(b['part_id'])}, {q(b['failure_mode'])}, "
                 f"{b['qty']}, {b['replace_every_h']});")
    for s in ids["part_stock"]:
        for z in ids["zones"]:
            L.append(f"INSERT INTO part_stock (part_id, zone_id, on_hand, reserved, reorder_point) VALUES "
                     f"({q(s['part_id'])}, {q(z['zone_id'])}, {s['on_hand_per_zone']}, 0, {s['reorder_point']});")
    L.append("COMMIT;")
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")
    print(f"{OUT}: {len(L)} lineas")


if __name__ == "__main__":
    main()
