"""Validacion de objetos contra los contratos `<nombre>.schema.json` (JSON Schema 2020-12)."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent


class ContractError(ValueError):
    def __init__(self, message: str, path: list):
        super().__init__(message)
        self.message = message
        self.path = path


@lru_cache(maxsize=1)
def contracts_dir() -> Path:
    bundled = HERE / "contracts"
    if bundled.is_dir():
        return bundled
    for parent in HERE.parents:
        if (parent / "contracts").is_dir():
            return parent / "contracts"
    raise FileNotFoundError("contracts/ no encontrado (ejecutar build.sh)")


@lru_cache(maxsize=None)
def _validator(contract: str):
    import jsonschema
    schema = json.loads((contracts_dir() / f"{contract}.schema.json").read_text())
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


def validate(contract: str, obj: dict) -> None:
    from jsonschema.exceptions import best_match
    err = best_match(_validator(contract).iter_errors(obj))
    if err is not None:
        raise ContractError(f"{contract}: {err.message}", list(err.absolute_path))
