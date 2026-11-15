#!/usr/bin/env bash
# Aplica oci/sql/*.sql en orden (01_schema, 02_fleet_events_pkg, 03_ords, 04_oauth, 05_seed) y el export de APEX.
# Owner: Alex (T06/T10). Usa SQLcl o la consola Database Actions; nunca el driver oracledb con wallet desde Lambda.
set -euo pipefail
echo "TODO T10: aplicar oci/sql en orden con SQLcl (sql -cloudconfig wallet.zip admin@fpdb_low @oci/sql/01_schema.sql ...)"
exit 1
