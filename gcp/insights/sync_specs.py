"""Copia contracts/oem_specs.yaml y el BOM de contracts/ids.yaml a app/*.json (la imagen solo lleva app/).

    python gcp/insights/sync_specs.py
tests/test_report.py falla si alguien cambia los YAML y no corre esto.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CONTRACTS = os.path.join(HERE, "..", "..", "contracts")


def load(name: str) -> dict:
    import yaml
    with open(os.path.join(CONTRACTS, name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def specs() -> dict:
    ids = load("ids.yaml")
    return {"oem": load("oem_specs.yaml"), "bom": ids["bom"], "failure_modes": ids["failure_modes"]}


if __name__ == "__main__":
    dst = os.path.join(HERE, "app", "specs.json")
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        json.dump(specs(), f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(f"escrito {dst}")
