#!/usr/bin/env bash
# docker/initdb.sh — controlled first-init migration apply for the Postgres container.
#
# WHY THIS EXISTS (LEDGER-C5):
#   The Postgres entrypoint auto-runs EVERY top-level *.sql in /docker-entrypoint-initdb.d
#   in C-collation sort order on first init. The repo's ./migrations dir historically held,
#   side by side, the applied additive chain (001..014), the *_rollback.sql files, AND the
#   STAGED migrations 015/016. A naive mount therefore made a clean `docker compose up`:
#     1. create the app role + RLS (008),
#     2. immediately DROP/DISABLE them again (008_rollback sorts right after 008),
#     3. then auto-apply 015/016 — whose banner says "DO NOT APPLY before the GUC is wired
#        or it locks every read."
#   i.e. the container self-undid its own schema and then applied a lockout-risk migration.
#
# THE FIX:
#   Instead of mounting ./migrations directly into /docker-entrypoint-initdb.d, the compose
#   files mount THIS script there and mount ./migrations read-only at /opt/migrations. The
#   entrypoint runs only this one script, which applies — in strict order — ONLY the applied
#   additive chain 001..014. It explicitly never touches *_rollback.sql, 015, or 016 (those
#   are staged behind correctness gates: 015→AC-RLS-4, 016→the Conduit RLS seam; see
#   scripts/apply_migrations.py STAGED / migrations/rollback + migrations/staged).
#
# This mirrors scripts/apply_migrations.py's APPLIED_CHAIN (006..014) plus the 001..005
# baseline that creates the schema the container starts from. Idempotency comes from the
# migrations themselves (CREATE ... IF NOT EXISTS), so re-init on a fresh volume is clean.
#
# This script runs as the postgres superuser inside the container during initdb, against the
# already-created POSTGRES_DB, so `psql` needs no extra connection flags.
set -euo pipefail

MIGRATIONS_DIR="${MIGRATIONS_DIR:-/opt/migrations}"

# The applied chain, in strict order. 001..005 = baseline schema; 006..014 = the v5 additive
# spine (matches APPLIED_CHAIN in scripts/apply_migrations.py). 015/016 + every *_rollback.sql
# are DELIBERATELY excluded — flipping them is a gated, hand-applied step, never an init step.
APPLIED_CHAIN=(
  001_initial_schema.sql
  002_api_call.sql
  003_eval_query_group.sql
  004_api_call_key_caps.sql
  005_system_sample.sql
  006_tenancy.sql
  007_tenancy_backfill.sql
  008_rls_roles.sql
  009_plans_entitlements.sql
  010_profiles_advisor.sql
  011_runs_metering_billing.sql
  012_compliance.sql
  013_trust_audit_export.sql
  014_corpus_cutover.sql
)

echo "[initdb.sh] applying the additive migration chain (001..014) from ${MIGRATIONS_DIR}"
for f in "${APPLIED_CHAIN[@]}"; do
  path="${MIGRATIONS_DIR}/${f}"
  if [[ ! -f "${path}" ]]; then
    echo "[initdb.sh] FATAL: expected migration ${f} not found at ${path}" >&2
    exit 1
  fi
  echo "[initdb.sh]   -> ${f}"
  psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" -f "${path}"
done
echo "[initdb.sh] chain applied (015/016 + *_rollback.sql intentionally NOT applied — gated, hand-applied)."
