#!/usr/bin/env bash
# run_aq_all.sh — EVERYTHING ON, LOCAL, for error-surfacing testing. NOT a public-serving authorization.
# Flips the full flag set (answer-quality levers + enforce modes + serving path) on the local stack, gpt-5.5,
# so integration errors that only appear at full activation surface. Public/paid use still needs G14 + EU-AI-Act.
#   ./scripts/local/run_aq_all.sh          → stop, relaunch EVERYTHING ON, then the caller smoke-tests
#   ./scripts/local/run_aq_all.sh stop     → stop
# T-LOCAL POSTURE (WS10 step 81): the flags below encode the FINAL T-LOCAL activation posture. The authoritative
# flag list + per-flag flip gates live in `docs/FLAG_GATES.md` (step-71 registry) in BOTH repos, and
# the fully-resolved EXPECTED posture is committed at `manifests/expected_tlocal_manifest.md` (step 81). metagate
# STAYS SHADOW here (rule 5). claim_verify enforce is authorized for LOCAL runs only (R5 recorded breach).
set -u
cd "$(dirname "$0")/../.."; APP="$(pwd)"; ENG="$APP/../engine"; LOG="$APP/logs"; mkdir -p "$LOG"
[ "${1:-}" = "stop" ] && { ./scripts/local/run_app_local.sh stop; exit 0; }

# AQ-06 (WS0 step 7): both the engine and the backend dump their fully-resolved settings (secrets redacted)
# here at boot, so the exact posture this run booted with is an on-disk artifact — inherited by the engine
# subshell (env preserves it) and by run_everything.sh/backend (exported).
export AQ_ENV_MANIFEST_DIR="$APP/manifests"; mkdir -p "$AQ_ENV_MANIFEST_DIR"
KEY="$(cd "$APP" && "$APP/.venv/bin/python" -c "from regsuper.config import get_settings; print(get_settings().openai_api_key or '')")"
[ -n "$KEY" ] || { echo "no OpenAI key"; exit 1; }

echo "Stopping current stack …"; ./scripts/local/run_app_local.sh stop >/dev/null 2>&1 || true; sleep 2

echo "Starting engine :8099 — ALL engine flags ON (gpt-5.5; metagate SHADOW per contract rule 5) …"
( cd "$ENG" && env \
  PYTHIA_DATABASE_URL="postgresql+asyncpg://regsuper:regsuper@localhost:5432/pythia" \
  PYTHIA_OPENAI_API_KEY="$KEY" PYTHIA_SYNTHESIZER_PROVIDER=openai \
  PYTHIA_METAGATE_ENABLED=1 PYTHIA_METAGATE_MODE=shadow PYTHIA_METAGATE_TRUE_COSINE_ENABLED=1 \
  PYTHIA_INTAKE_V2_ENABLED=1 PYTHIA_INTAKE_V2_MODE=active PYTHIA_INTAKE_CLARIFY_ENABLED=1 \
  PYTHIA_STRUCTURED_ANSWER_ENABLED=1 PYTHIA_STRUCTURED_PLAN_LLM_ENABLED=1 \
  PYTHIA_CTX_WINDOW_ENABLED=1 PYTHIA_CTX_CLAUSE_SNAP_ENABLED=1 PYTHIA_EVIDENCE_BUNDLE_ENABLED=1 \
  PYTHIA_TERM_NORM_ENABLED=1 PYTHIA_SOFTLAW_QUOTA_ENABLED=1 \
  PYTHIA_FUSION_ENABLED=1 PYTHIA_ANSWER_PATH_FUSION_ENABLED=1 \
  PYTHIA_SEED_TOKEN_IDF_ENABLED=1 PYTHIA_SEED_POOL_SCALE_ENABLED=1 \
  PYTHIA_RETRIEVAL_QUOTAS_ENABLED=1 PYTHIA_RETRIEVAL_CONCURRENT_LEGS_ENABLED=1 PYTHIA_ANSWER_LEG_CONCURRENCY=4 \
  PYTHIA_DRAFT_PLANNER_ENABLED=1 PYTHIA_STAGE_LATENCY_ENABLED=1 PYTHIA_QUALITY_TRIPWIRES_ENABLED=1 \
  PYTHIA_CLAIM_VERIFY_ENABLED=1 PYTHIA_CLAIM_VERIFY_MODE=enforce PYTHIA_CLAIM_VERIFY_GAP_PROBE_ENABLED=1 \
  PYTHIA_CLAIM_ENTAILMENT_LLM_ENABLED=1 PYTHIA_CLAIM_REPAIR_LLM_ENABLED=1 \
  PYTHIA_TEMPORAL_CONSUMER_ENABLED=1 PYTHIA_TEMPORAL_PROSE_GUARD_ENABLED=1 \
  PYTHIA_DISCOVERY_ENABLED=1 \
  nohup "$ENG/.venv/bin/uvicorn" pythia.service:app --host 127.0.0.1 --port 8099 \
    > "$LOG/engine.log" 2>&1 < /dev/null & )
for i in $(seq 1 60); do curl -s -m 3 http://127.0.0.1:8099/health | grep -q '"status"' && { echo "engine up (${i}s)"; break; }; sleep 1; done

# App-side flags run_everything.sh does NOT set (it already does compliance=enforce, RLS, nexus, WF, gap-suppression, entitlements).
# MODEL_CHEAP: the compliance-guard input gate (ModelRole.CHEAP) defaults to an Anthropic model but is called
# via the OpenAI client → 404 model_not_found → the enforce-mode gate fails closed on EVERY request. Point it at
# an OpenAI cheap model so the gate actually classifies (surfaced by the everything-on test, 2026-07-05).
export MODEL_CHEAP=gpt-4o-mini
export ANSWER_PRACTICAL_BLOCK_ENABLED=1 ANSWER_INTAKE_PANEL_ENABLED=1 ANSWER_PROFILE_CONTEXT_ENABLED=1
export ANSWER_CLARIFY_ENABLED=1 SME_ROLES_ENABLED=1 AUTHORITY_REGISTRY_ENABLED=1 CUTOVER_APPROVED=1
export NEXT_PUBLIC_ANSWER_VIEW_ENABLED=1
# WS7-WS9 app-side spend/ledger levers (step 81). budget_precheck + boundary_spend gate the sidecar call behind
# the killswitch + enforce_spend_limit(openai); ledger_export fires the canonical claim-ledger bundle after verify.
export ANSWER_BUDGET_PRECHECK_ENABLED=1 CLAIM_LEDGER_EXPORT_ENABLED=1 PYTHIA_BOUNDARY_SPEND_GATE_ENABLED=1
# W4 authority-resolver seed/map/gazetteer paths (step 76). AUTHORITY_REGISTRY_ENABLED (above) only MOUNTS the
# resolver; without these it builds an EMPTY index and resolves nothing. The topic map is config-not-DB, so the
# serving resolver needs AUTHORITY_TOPIC_MAP_PATH set. Point at the committed DRAFT fixtures (advocate-review pending).
export AUTHORITY_SEED_PATH="$APP/tests/fixtures/authority/authority_registry.sample.json"
export AUTHORITY_TOPIC_MAP_PATH="$APP/tests/fixtures/authority/topic_authority.sample.json"
export AUTHORITY_GAZETTEER_PATH="$APP/data/geo/municipality_gazetteer.json"

echo "Starting backend :8002 + FE (run_everything full posture + the above) …"
./scripts/local/run_app_local.sh
echo "EVERYTHING ON, locally, on gpt-5.5. This is a TEST config — NOT cleared for public/paid users."
