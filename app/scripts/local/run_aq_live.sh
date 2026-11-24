#!/usr/bin/env bash
# run_aq_live.sh — turn ON the answer-QUALITY levers locally (engine :8099 + backend :8002 + FE),
# so the LOCAL product actually uses the improvements. NOT public. Reversible (stop + use run_app_local.sh).
#
# WHAT IS ON (the answer-quality improvement steps that were built):
#   engine: intake_v2=ACTIVE (recall lever) · structured_answer (7-slot) · ctx_window (deadline+amount
#           visibility, incl. the new AQ-B1 `amount` type) · evidence_bundle · term_norm (with the extended
#           AQ-B2 DRAFT lexicon) · softlaw_quota · draft_planner + stage_latency + quality_tripwires (observe)
#           · claim_verify=SHADOW (attaches the verify ledger; output byte-identical)
#   app:    answer surface + practical_block (surfaces the 7-slot) · understanding panel · profile_context
#   synth:  OLLAMA (qwen2.5:14b) — OpenAI billing is exhausted (429), so gpt-5.5 is unavailable.
#
# WHAT IS HELD (NOT flipped — legal / gated; see ACTIVATION_RUNBOOK.md §4):
#   metagate=ENFORCE (AC-METAGATE RED) · compliance_guard=ENFORCE (G14 lawyer sign-off) · claim_verify=ENFORCE
#   (VL-G gate) · temporal downweight/current-only (lawyer+B3) · gap-suppression (lawyer) · the PUBLIC/paid
#   serving cutover + spend caps (operator) · EU-AI-Act determination. Activating any of those for real users
#   without those sign-offs is NOT permitted.
#
#   ./scripts/local/run_aq_live.sh          → stop the current stack, relaunch with the levers ON, smoke-test
#   ./scripts/local/run_aq_live.sh stop     → stop everything
set -u
cd "$(dirname "$0")/../.."; APP="$(pwd)"; ENG="$APP/../engine"; LOG="$APP/logs"; mkdir -p "$LOG"
UI_PORT="${POLARIUS_UI_PORT:-3001}"

if [ "${1:-}" = "stop" ]; then ./scripts/local/run_app_local.sh stop; exit 0; fi

echo "Stopping the current stack …"; ./scripts/local/run_app_local.sh stop >/dev/null 2>&1 || true; sleep 2

echo "Starting engine :8099 with the answer-quality levers ON (ollama synth) …"
( cd "$ENG" && \
  PYTHIA_DATABASE_URL="postgresql+asyncpg://regsuper:regsuper@localhost:5432/pythia" \
  PYTHIA_SYNTHESIZER_PROVIDER=ollama \
  PYTHIA_METAGATE_ENABLED=1        PYTHIA_METAGATE_MODE=shadow \
  PYTHIA_INTAKE_V2_ENABLED=1       PYTHIA_INTAKE_V2_MODE=active \
  PYTHIA_STRUCTURED_ANSWER_ENABLED=1 \
  PYTHIA_CTX_WINDOW_ENABLED=1 \
  PYTHIA_EVIDENCE_BUNDLE_ENABLED=1 \
  PYTHIA_TERM_NORM_ENABLED=1 \
  PYTHIA_SOFTLAW_QUOTA_ENABLED=1 \
  PYTHIA_DRAFT_PLANNER_ENABLED=1 \
  PYTHIA_STAGE_LATENCY_ENABLED=1   PYTHIA_QUALITY_TRIPWIRES_ENABLED=1 \
  PYTHIA_CLAIM_VERIFY_ENABLED=1    PYTHIA_CLAIM_VERIFY_MODE=shadow \
  nohup "$ENG/.venv/bin/uvicorn" pythia.service:app --host 127.0.0.1 --port 8099 \
    > "$LOG/engine.log" 2>&1 < /dev/null & )
for i in $(seq 1 60); do curl -s -m 3 http://127.0.0.1:8099/health | grep -q '"status"' && { echo "engine up (${i}s)"; break; }; sleep 1; done

# App-side answer-quality flags run_everything.sh does NOT set — exported so the backend inherits them.
# (run_everything.sh keeps the operator's existing posture for everything else.)
export ANSWER_PRACTICAL_BLOCK_ENABLED=1
export ANSWER_INTAKE_PANEL_ENABLED=1
export ANSWER_PROFILE_CONTEXT_ENABLED=1
export NEXT_PUBLIC_ANSWER_VIEW_ENABLED=1

echo "Starting backend :8002 + FE :$UI_PORT (engine already up) …"
./scripts/local/run_app_local.sh

echo
echo "Answer-quality levers are LIVE locally on ollama. Smoke-test:"
echo "  curl -s localhost:8002/api/answer -H 'content-type: application/json' \\"
echo "    -d '{\"query\":\"Mám malou restauraci v Plzni a chci poprvé zaměstnat člověka. Co musím udělat?\"}' | python3 -m json.tool"
echo "NOTE: answers synthesize on ollama (weaker than gpt-5.5); restore OpenAI billing for production quality."
