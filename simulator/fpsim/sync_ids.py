"""Vendoriza los catalogos en simulator/fpsim/*.json (la Lambda no trae PyYAML):
- ids.json: union de los parques de contracts/tenants.yaml (ids.yaml de mineria + tenants/*.yaml), cada maquina,
  zona y tecnico con su `tenant_id` (fplib.ids.merge_catalogs, el mismo que usa la layer fp-shared).
- oem.json: contracts/oem_specs.yaml.

    python simulator/fpsim/sync_ids.py
Los tests test_ids_in_sync y test_oem_specs_valid_and_in_sync fallan si alguien cambia los YAML y no corre esto.
La consola usa una copia: cp simulator/fpsim/ids.json gcp/console/app/fleet.json (y oem.json).
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONTRACTS = os.path.join(HERE, "..", "..", "contracts")
JSON_PATH = os.path.join(HERE, "ids.json")
OEM_YAML = os.path.join(CONTRACTS, "oem_specs.yaml")
OEM_JSON = os.path.join(HERE, "oem.json")


def merged() -> dict:
    sys.path.append(os.path.join(HERE, "..", "..", "aws", "shared", "python"))
    from fplib.ids import merge_catalogs
    return merge_catalogs(CONTRACTS)


def dump(data: dict, dst: str) -> None:
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(f"escrito {dst}")


def write(src: str, dst: str) -> None:
    import yaml
    with open(src, encoding="utf-8") as f:
        dump(yaml.safe_load(f), dst)


if __name__ == "__main__":
    dump(merged(), JSON_PATH)
    write(OEM_YAML, OEM_JSON)
