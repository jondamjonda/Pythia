#!/usr/bin/env bash
# AC-RLS-4 runner — provisions an ISOLATED scratch DB on the local Docker Postgres, runs the cross-tenant leak
# test (scripts/rls_leak_test.sql) against the real migration-015 predicate, and tears the scratch DB down.
# Never touches the live regsuper/pythia data. Exits non-zero on any leak (ON_ERROR_STOP in the SQL).
set -euo pipefail
CONTAINER="${RLS_PG_CONTAINER:-regsuper-postgres}"
PGUSER="${RLS_PG_USER:-regsuper}"
DB="rls_leak_test_$$"
HERE="$(cd "$(dirname "$0")" && pwd)"

cleanup() { docker exec "$CONTAINER" psql -U "$PGUSER" -d postgres -qc "DROP DATABASE IF EXISTS ${DB};" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "AC-RLS-4: provisioning isolated scratch DB '${DB}' on ${CONTAINER}…"
docker exec "$CONTAINER" psql -U "$PGUSER" -d postgres -qc "DROP DATABASE IF EXISTS ${DB};" >/dev/null
docker exec "$CONTAINER" psql -U "$PGUSER" -d postgres -qc "CREATE DATABASE ${DB};" >/dev/null

echo "AC-RLS-4: running the cross-tenant leak test…"
docker exec -i "$CONTAINER" psql -U "$PGUSER" -d "${DB}" -v ON_ERROR_STOP=1 < "${HERE}/rls_leak_test.sql"
