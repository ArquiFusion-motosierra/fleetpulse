"""Copia contracts/ids.yaml y contracts/oem_specs.yaml a simulator/fpsim/*.json (la Lambda no trae PyYAML).

    python simulator/fpsim/sync_ids.py
Los tests test_ids_in_sync y test_oem_specs_valid_and_in_sync fallan si alguien cambia los YAML y no corre esto.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
YAML_PATH = os.path.join(HERE, "..", "..", "contracts", "ids.yaml")
JSON_PATH = os.path.join(HERE, "ids.json")
OEM_YAML = os.path.join(HERE, "..", "..", "contracts", "oem_specs.yaml")
OEM_JSON = os.path.join(HERE, "oem.json")


def load_yaml() -> dict:
    import yaml
    with open(YAML_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def write(src: str, dst: str) -> None:
    import yaml
    with open(src, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(f"escrito {dst}")


if __name__ == "__main__":
    write(YAML_PATH, JSON_PATH)
    write(OEM_YAML, OEM_JSON)
