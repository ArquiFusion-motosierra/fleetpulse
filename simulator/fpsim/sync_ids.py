"""Copia contracts/ids.yaml a simulator/fpsim/ids.json (la Lambda no trae PyYAML).

    python simulator/fpsim/sync_ids.py
El test test_ids_in_sync falla si alguien cambia ids.yaml y no corre esto.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
YAML_PATH = os.path.join(HERE, "..", "..", "contracts", "ids.yaml")
JSON_PATH = os.path.join(HERE, "ids.json")


def load_yaml() -> dict:
    import yaml
    with open(YAML_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


if __name__ == "__main__":
    with open(JSON_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(load_yaml(), f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(f"escrito {JSON_PATH}")
