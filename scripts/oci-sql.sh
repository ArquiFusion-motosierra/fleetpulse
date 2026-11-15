#!/usr/bin/env bash
# Aplica oci/sql en orden con SQLcl: 01 como ADMIN; 02, 03, 04 y 05 como FLEET.
# Owner: Alex (T06/T10). Nunca el driver oracledb con wallet desde Lambda: la escritura desde AWS es por ORDS.
#
#   export OCI_WALLET=~/Wallet_fpdb.zip OCI_ADMIN_PWD=... OCI_FLEET_PWD=...
#   scripts/oci-sql.sh            # todo
#   scripts/oci-sql.sh 02 03      # solo esos pasos
#
# Sin SQLcl: abrir Database Actions > SQL y ejecutar los archivos en el mismo orden
# (01 conectado como ADMIN con &1 = clave de FLEET; el resto conectado como FLEET).
set -euo pipefail
cd "$(dirname "$0")/.."

: "${OCI_WALLET:?ruta al wallet .zip de fpdb}"
: "${OCI_ADMIN_PWD:?clave de ADMIN}"
: "${OCI_FLEET_PWD:?clave de FLEET}"
SERVICE="${OCI_SERVICE:-fpdb_low}"
command -v sql >/dev/null || { echo "Falta SQLcl (sql). https://www.oracle.com/database/sqldeveloper/technologies/sqlcl/"; exit 1; }

steps=("$@")
[ ${#steps[@]} -eq 0 ] && steps=(01 02 03 04 05)

for s in "${steps[@]}"; do
  file=$(ls oci/sql/${s}_*.sql)
  echo "== $file"
  if [ "$s" = "01" ]; then
    sql -S -cloudconfig "$OCI_WALLET" "admin/${OCI_ADMIN_PWD}@${SERVICE}" @"$file" "$OCI_FLEET_PWD" <<< "exit"
  else
    sql -S -cloudconfig "$OCI_WALLET" "fleet/${OCI_FLEET_PWD}@${SERVICE}" @"$file" <<< "exit"
  fi
done
