#!/usr/bin/env bash
# run_aq_staged.sh — STAGED activation of the Answer-Quality dark flags, LOCAL ONLY.
#
# Prepared from ACTIVATION_RUNBOOK.md (Polarius Project/Answer Quality Initiative/Autonomous Session 2026-07-04).
# CARDINAL RULE: OFF → SHADOW → ENFORCE, never skip. This script only ever reaches SHADOW / local-eval.
# It NEVER flips a legal-advice ENFORCE gate (metagate enforce, claim_verify enforce, temporal currency,
# gap-suppression, public hedged/clarify copy) — those are HELD for G14 + EU-AI-Act + operator (see runbook §4).
# Nothing here is public: it runs the engine+app on localhost for a scored OFF-vs-ON eval loop.
#
#   ./scripts/local/run_aq_staged.sh 0      → Stage 0: observe/shadow only (ZERO output change) — always safe
#   ./scripts/local/run_aq_staged.sh 1      → Stage 1: the eval-loop ON-arm (7-slot + intake-active + ctx + bundle)
#                               GATED: refuses unless the structured_answer KeyError fix has landed
#   ./scripts/local/run_aq_staged.sh 2      → Stage 1 + claim_verify SHADOW (also capture the verify ledger)
#   ./scripts/local/run_aq_staged.sh stop   → stop engine+backend+frontend
#
# The scored comparison itself is done at the API level (POST /api/answer), not the FE.
set -u
cd "$(dirname "$0")/../.."
APP="$(pwd)"; ENG="$APP/../engine"; LOG="$APP/logs"; mkdir -p "$LOG"
STAGE="${1:-0}"

if [ "$STAGE" = "stop" ]; then ./scripts/local/run_app_local.sh stop; exit 0; fi

# ── Stage 0 engine flags — OBSERVE-ONLY / SHADOW: output byte-identical, safe even in prod ────────────
ENG_ENV=(
  PYTHIA_DATABASE_URL="postgresql+asyncpg://regsuper:regsuper@localhost:5432/pythia"
  PYTHIA_SYNTHESIZER_PROVIDER=openai
  PYTHIA_METAGATE_ENABLED=1        PYTHIA_METAGATE_MODE=shadow      # HELD at shadow (enforce = AC-METAGATE RED + G14)
  PYTHIA_INTAKE_V2_ENABLED=1       PYTHIA_INTAKE_V2_MODE=shadow     # active is turned on below only for Stage>=1
  PYTHIA_STAGE_LATENCY_ENABLED=1                                    # observe: per-stage latency
  PYTHIA_QUALITY_TRIPWIRES_ENABLED=1                               # observe: quality tripwire events
  PYTHIA_DRAFT_PLANNER_ENABLED=1                                   # observe: lossless DraftPlan audit annotation
)

# ── Stage 1 additions — the LOCAL EVAL ON-ARM (behaviour-changing, local only, NOT public) ────────────
if [ "$STAGE" = "1" ] || [ "$STAGE" = "2" ]; then
  # PRECONDITION GATE — the structured_answer KeyError fix (answer.py ~980/994: {profile}/{facts} unfilled).
  # Refuse to enable the 7-slot path until the fix has landed, else GENERATE crashes with KeyError.
  echo "Checking the structured_answer KeyError fix has landed …"
  if ! "$ENG/.venv/bin/python" - <<'PY'
import re, sys, pathlib
src = pathlib.Path("../engine/pythia/reasoning/answer.py").read_text(encoding="utf-8")
# The fix must pass profile= and facts= into the structured .format() call sites.
ok = ("profile=" in src and "facts=" in src
      and re.search(r"gen_prompt\.format\([^)]*profile=", src) is not None)
sys.exit(0 if ok else 1)
PY
  then
    echo "✋ REFUSING Stage $STAGE: the structured_answer KeyError fix is NOT in answer.py yet"
    echo "   (the .format call sites don't pass profile=/facts=). It is being fixed in a separate session."
    echo "   Run './scripts/local/run_aq_staged.sh 0' for the safe observe/shadow set until the fix lands."
    exit 3
  fi
  echo "  ✓ fix present — enabling the Stage 1 eval ON-arm."
  ENG_ENV+=(
    PYTHIA_INTAKE_V2_MODE=active          # the vague-query RECALL lever (A2 ~3× directional)
    PYTHIA_STRUCTURED_ANSWER_ENABLED=1    # the 7-slot Czech action plan → populates .practical
    PYTHIA_CTX_WINDOW_ENABLED=1           # deadline visibility (fixes 52% of deadlines past char 700)
    PYTHIA_EVIDENCE_BUNDLE_ENABLED=1      # canonical full-text carrier (W3 consumers wired)
  )
fi
if [ "$STAGE" = "2" ]; then
  ENG_ENV+=( PYTHIA_CLAIM_VERIFY_ENABLED=1 PYTHIA_CLAIM_VERIFY_MODE=shadow )  # FIRST-EVER verify ledger; output byte-identical
fi

# ── The matched app-side flag (S5): surface the engine .practical block. MUST pair with engine S3. ────
if [ "$STAGE" = "1" ] || [ "$STAGE" = "2" ]; then
  export ANSWER_PRACTICAL_BLOCK_ENABLED=1     # inherited by run_everything.sh's backend uvicorn
fi

# ── Launch the engine on :8099 with the staged env (skip if already up) ───────────────────────────────
if curl -s -m 3 http://127.0.0.1:8099/health | grep -q '"status"'; then
  echo "Engine already up on :8099 — STOP it first (./scripts/local/run_aq_staged.sh stop) to apply new flags."
else
  KEY="$("$APP/.venv/bin/python" -c "from regsuper.config import get_settings; print(get_settings().openai_api_key or '')")"
  [ -n "$KEY" ] || { echo "⚠ No OpenAI key in config — the 7-slot synthesis needs gpt-5.5; aborting."; exit 4; }
  echo "Starting engine :8099 (Stage $STAGE) …"
  ( cd "$ENG" && env "${ENG_ENV[@]}" PYTHIA_OPENAI_API_KEY="$KEY" \
      nohup "$ENG/.venv/bin/uvicorn" pythia.service:app --host 127.0.0.1 --port 8099 \
      > "$LOG/engine.log" 2>&1 < /dev/null & )
  for _ in $(seq 1 60); do curl -s -m 3 http://127.0.0.1:8099/health | grep -q '"status"' && break; sleep 1; done
fi

# ── Backend + FE via the existing launcher (inherits ANSWER_PRACTICAL_BLOCK_ENABLED); engine is already up ─
NEXT_PUBLIC_ANSWER_VIEW_ENABLED=1 ./scripts/local/run_app_local.sh

echo
echo "Stage $STAGE up. SMOKE-TEST the matched pair (deadline query → .practical.items must be non-empty):"
echo "  curl -s localhost:8002/api/answer -H 'content-type: application/json' \\"
echo "    -d '{\"query\":\"kdy musím nahlásit prvního zaměstnance na OSSZ?\"}' | python -m json.tool | grep -A3 practical"
echo
echo "For the SCORED OFF-vs-ON artifact: run Stage 0 (OFF-arm) then Stage 1 (ON-arm) over Build/eval/gold_layman_v1.jsonl,"
echo "capture both, diff Answer-Recall@k + citation-grounding + refusal-rate. Numbers are DIRECTIONAL until the gold is lawyer-certified."
