#!/usr/bin/env bash
# run_aq_local.sh — EVERYTHING ON, LOCAL, on the FREE local model (Ollama qwen2.5:14b). A $0 test posture.
# Same full AQ flag set as run_aq_all.sh, with two changes so it costs nothing and actually answers while the
# OpenAI account is quota-dead:
#   1. the ENGINE synthesizer is 'ollama' → every engine LLM call (GENERATE / decompose / verify / claim-repair /
#      entailment / metagate judges) hits the local qwen2.5:14b, no OpenAI spend.
#   2. the app compliance guard runs in SHADOW → its Tier-2 cheap check is hardcoded to the (dead) OpenAI client;
#      in shadow it logs-but-does-not-block, so a normal test question is not falsely refused as needs_review.
# metagate stays SHADOW; claim_verify stays enforce (authorized LOCAL only). Committed config defaults are
# UNCHANGED (this is a launch-env script). Answers are rougher + slower than gpt-5.5 (small local model, many
# sequential local calls per answer). NOT a public/paid serving authorization.
#   ./scripts/local/run_aq_local.sh        → stop, relaunch EVERYTHING ON (local model), print the URL to open
#   ./scripts/local/run_aq_local.sh stop   → stop
set -u
cd "$(dirname "$0")/../.."; APP="$(pwd)"; ENG="$APP/../engine"; LOG="$APP/logs"; mkdir -p "$LOG"
[ "${1:-}" = "stop" ] && { ./scripts/local/run_app_local.sh stop; exit 0; }

export AQ_ENV_MANIFEST_DIR="$APP/manifests"; mkdir -p "$AQ_ENV_MANIFEST_DIR"

echo "Stopping current stack …"; ./scripts/local/run_app_local.sh stop >/dev/null 2>&1 || true; sleep 2
# hard-kill any stray engine (e.g. the quota-dead openai one) so our OLLAMA engine owns :8099
pkill -f "uvicorn pythia.service" 2>/dev/null || true
lsof -ti:8099 2>/dev/null | xargs kill 2>/dev/null || true
sleep 2

echo "Starting engine :8099 — ALL engine flags ON, synthesizer=OLLAMA (local qwen2.5:14b, no OpenAI spend; metagate SHADOW) …"
( cd "$ENG" && env \
  PYTHIA_DATABASE_URL="postgresql+asyncpg://regsuper:regsuper@localhost:5432/pythia" \
  PYTHIA_SYNTHESIZER_PROVIDER=ollama PYTHIA_OLLAMA_KEEP_ALIVE=10m \
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
for i in $(seq 1 90); do curl -s -m 3 http://127.0.0.1:8099/health | grep -q '"status"' && { echo "engine up (${i}s)"; break; }; sleep 1; done

# App-side flags. COMPLIANCE_GUARD_MODE=shadow is consumed by run_everything.sh's overridable line.
export COMPLIANCE_GUARD_MODE=shadow
export MODEL_CHEAP=gpt-4o-mini
export ANSWER_PRACTICAL_BLOCK_ENABLED=1 ANSWER_INTAKE_PANEL_ENABLED=1 ANSWER_PROFILE_CONTEXT_ENABLED=1
export ANSWER_CLARIFY_ENABLED=1 SME_ROLES_ENABLED=1 AUTHORITY_REGISTRY_ENABLED=1 CUTOVER_APPROVED=1
export NEXT_PUBLIC_ANSWER_VIEW_ENABLED=1
export ANSWER_BUDGET_PRECHECK_ENABLED=1 CLAIM_LEDGER_EXPORT_ENABLED=1 PYTHIA_BOUNDARY_SPEND_GATE_ENABLED=1
export AUTHORITY_SEED_PATH="$APP/tests/fixtures/authority/authority_registry.sample.json"
export AUTHORITY_TOPIC_MAP_PATH="$APP/tests/fixtures/authority/topic_authority.sample.json"
export AUTHORITY_GAZETTEER_PATH="$APP/data/geo/municipality_gazetteer.json"

echo "Starting backend :8002 + FE (compliance=SHADOW, full posture) …"
./scripts/local/run_app_local.sh
echo "EVERYTHING ON, locally, on the FREE local model (Ollama qwen2.5:14b). \$0 test — rougher + slower than gpt-5.5. NOT cleared for public/paid."
