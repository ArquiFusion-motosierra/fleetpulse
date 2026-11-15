#!/usr/bin/env bash
# Construye el contenido de la layer fp-shared en python/: dependencias para Lambda arm64 / Python 3.12,
# los contratos, el catalogo de todos los parques (tenants.yaml + ids.yaml + tenants/*.yaml, unidos por
# fplib.ids.merge_catalogs), oem_specs.yaml (T26) y supply.yaml (T37) si existen, como JSON. fplib/ ya vive en python/fplib y esta
# versionado.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-../../.venv/bin/python}"
[ -x "$PY" ] || PY=python3

pip_install() {
  "$PY" -m pip install -r requirements.txt \
    --platform manylinux2014_aarch64 --python-version 3.12 --implementation cp \
    --only-binary=:all: --target python/ --upgrade --quiet
}
pip_install

rm -rf python/fplib/contracts python/fplib/data
mkdir -p python/fplib/contracts python/fplib/data
cp ../../contracts/*.schema.json python/fplib/contracts/
# Un solo ids.json con los tres parques: cada maquina, zona y tecnico lleva su tenant_id.
"$PY" -c 'import json,sys; sys.path.append("python"); from fplib.ids import merge_catalogs; json.dump(merge_catalogs(sys.argv[1]), open(sys.argv[2],"w"))' \
  ../../contracts python/fplib/data/ids.json
if [ -f ../../contracts/oem_specs.yaml ]; then
  "$PY" -c 'import yaml,json,sys; json.dump(yaml.safe_load(open(sys.argv[1])), open(sys.argv[2],"w"))' \
    ../../contracts/oem_specs.yaml python/fplib/data/oem_specs.json
fi
if [ -f ../../contracts/supply.yaml ]; then  # T37: catalogo de abastecimiento
  "$PY" -c 'import yaml,json,sys; json.dump(yaml.safe_load(open(sys.argv[1], encoding="utf-8")), open(sys.argv[2],"w"))' \
    ../../contracts/supply.yaml python/fplib/data/supply.json
fi
find python -name __pycache__ -type d -prune -exec rm -rf {} +

echo "layer size: $(du -sh python | cut -f1)"
