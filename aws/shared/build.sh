#!/usr/bin/env bash
# Construye el contenido de la layer fp-shared en python/: dependencias para Lambda arm64 / Python 3.12,
# los contratos y ids.yaml (como JSON). fplib/ ya vive en python/fplib y esta versionado.
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
"$PY" -c 'import yaml,json,sys; json.dump(yaml.safe_load(open(sys.argv[1])), open(sys.argv[2],"w"))' \
  ../../contracts/ids.yaml python/fplib/data/ids.json
find python -name __pycache__ -type d -prune -exec rm -rf {} +

echo "layer size: $(du -sh python | cut -f1)"
