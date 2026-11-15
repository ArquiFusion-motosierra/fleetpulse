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
def _validator(contract: str, ref: str | None = None):
    import jsonschema
    schema = json.loads((contracts_dir() / f"{contract}.schema.json").read_text())
    if ref is not None:
        if ref not in schema.get("$defs", {}):
            raise KeyError(f"{contract}: no existe $defs/{ref}")
        # misma $id y $defs: los $ref internos ("#/$defs/...") siguen resolviendo igual
        schema = {k: schema[k] for k in ("$schema", "$id", "$defs") if k in schema} | {"$ref": f"#/$defs/{ref}"}
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


def validate(contract: str, obj: dict, ref: str | None = None) -> None:
    """Valida `obj` contra el contrato completo o, con `ref`, contra `$defs/<ref>` (ej. `report`, `create`)."""
    from jsonschema.exceptions import best_match
    err = best_match(_validator(contract, ref).iter_errors(obj))
    if err is not None:
        where = f"{contract}#{ref}" if ref else contract
        raise ContractError(f"{where}: {err.message}", list(err.absolute_path))
