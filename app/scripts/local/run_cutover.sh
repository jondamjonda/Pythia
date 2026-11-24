#!/usr/bin/env bash
# run_cutover.sh — launch Polarius in B3 CUTOVER mode: the cascade serves CURRENT law from the Nexus engine
# instead of the frozen 2024 regsuper corpus. This is the LAUNCHED B3 (2026-06-29): B3-06a lawyer sample reviewed
# faithful + B3-06b refusal fallback accepted by the operator → cutover_approved=True admits the live nexus run.
#
# DIFFERENCE from run_local.sh: this sets LAW_SOURCE=nexus + CUTOVER_APPROVED=1 + PYTHIA_* (the engine sidecar).
# Everything else (RLS/tenancy enforced, the green-gated features) matches run_local.sh.
#
# COVERAGE CEILING (Option-A, reviewed): a current-law finding citing an act OUTSIDE the regsuper↔nexus crosswalk
# (currently 11 acts / 7895 provisions) has no app document to cite and is DROPPED. So cutover serves current law
# for the crosswalked acts; broadening coverage is a future corpus/crosswalk expansion.
#
# PRECONDITION: the Pythia engine must be running (this script checks + refuses to launch if it is down). Start it:
#   cd ../engine && PYTHIA_DATABASE_URL=<...> ./.venv/bin/uvicorn pythia.service:app --port 8099
set -euo pipefail
cd "$(dirname "$0")/../.."

PYTHIA_URL="${PYTHIA_URL:-http://127.0.0.1:8099}"
echo "Checking the Pythia engine at ${PYTHIA_URL} …"
# LEDGER-L-runtime-02: accept "degraded" as well as "ok". "degraded" (still HTTP
# 200) means only Ollama is down; the LLM-free nexus retrieval path the cascade
# uses under law_source=nexus still works. Only an unreachable/non-200 engine refuses.
_health="$(curl -s -m 5 "${PYTHIA_URL}/health" || true)"
if ! printf '%s' "${_health}" | grep -Eq '"status":"(ok|degraded)"'; then
  echo "✋ REFUSING TO LAUNCH: the Pythia engine is not reachable/healthy at ${PYTHIA_URL}." >&2
  echo "   Under law_source=nexus the cascade reads current law from the engine; without it every analysis fails." >&2
  echo "   Start it first (see the header), then re-run this script." >&2
  exit 1
fi
echo "Engine healthy (${_health})."

# Green-gated features (mirror run_local.sh)
export AUDIT_EXPORT_ENABLED=1
export AUDIT_EVIDENCE_ENABLED=1
export PROFILES_ENABLED=1
export DIGEST_ENABLED=1
# LEDGER-M-flags / M28: L4_MODE is a DEAD flag (read NOWHERE). L4 is ALWAYS the
# deterministic keyword stub ("rule:l4-keyword-stub-v1"); =llm does NOT enable a
# live LLM at L4. SPEND CAP (M28): data/limits.json ships daily_usd=1.0 +
# block_when_over=true; the L2/L3 cascade spend (NOT L4) can trip it and abort a
# cutover run mid-analysis — raise daily_usd there for sustained runs.
export L4_MODE=llm                 # DEAD flag (no consumer) — L4 is always the keyword stub; see above
export ENTITY_SCOPING_ENABLED=1

# RLS / tenancy ENFORCED (as regsuper_app) — see run_local.sh
export DATABASE_URL="postgresql+asyncpg://regsuper_app:regsuper_app_local@localhost:5432/regsuper"
export RLS_ENFORCED=1
export TENANCY_ENABLED=1

# B3 CUTOVER — serve current law from the engine (the launched state)
export LAW_SOURCE=nexus
export CUTOVER_APPROVED=1          # B3-06: operator attestation that B3-06a + B3-06b passed
export PYTHIA_ENABLED=1
export PYTHIA_URL="${PYTHIA_URL}"

echo "Polarius backend — B3 CUTOVER (current law via Nexus). ON: all green-gates · entity-scoping · RLS enforced · law_source=NEXUS."
echo "Coverage ceiling: findings citing acts outside the 11-act crosswalk are dropped (Option-A, reviewed)."
exec .venv/bin/uvicorn regsuper.api.main:app --reload --host 127.0.0.1 --port 8000 "$@"
