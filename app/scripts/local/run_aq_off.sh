#!/usr/bin/env bash
# run_aq_off.sh — CLEAN OFF BASELINE (the "before" arm for WS11/B4). NEVER ollama, NEVER the contaminated
# eval/artifacts/off_full.json. This is the reproducible OFF posture the aq_answer_eval harness scores against
# to produce deploy_off_*.json, so an OFF-vs-ON delta is a real measurement, not a stale artifact.
#
# It is a clone of run_aq_all.sh's ANSWER POSTURE with every AQ answer-quality flag at its COMMITTED DEFAULT
# (all default-OFF, verified from pythia.config / regsuper.config on 2026-07-08), but KEEPING the two things
# the harness needs identical to the ON arm (step 86): the gpt-5.5 synthesizer + metagate in SHADOW. So the
# ONLY difference between this OFF arm and run_aq_all.sh's ON arm is the AQ answer-quality levers — model and
# metagate posture are held constant, which is what makes the delta attributable to the levers.
#
#   ./scripts/local/run_aq_off.sh          → stop, relaunch the CLEAN-OFF engine (+ backend for parity), then score
#   ./scripts/local/run_aq_off.sh stop     → stop
#
# THE OFF FLAG POSTURE (what is turned OFF vs run_aq_all.sh):
#   engine answer-quality levers, ALL at their default-OFF: intake_v2 (mode=shadow, NOT active),
#   intake_clarify, structured_answer, structured_plan_llm, ctx_window, ctx_clause_snap, evidence_bundle,
#   term_norm, softlaw_quota, fusion, answer_path_fusion, seed_token_idf, seed_pool_scale, retrieval_quotas,
#   retrieval_concurrent_legs, draft_planner, quality_tripwires, claim_verify (+ mode=shadow, gap_probe,
#   entailment_llm, repair_llm), metagate_true_cosine, temporal_consumer, temporal_prose_guard, discovery.
#   metagate_true_cosine is OFF here (it is an AQ retrieval lever; ON flips it) — metagate still RUNS in shadow.
#   stage_latency stays ON (observe-only cost telemetry — never changes the answer; needed to attribute latency).
#   KEPT ON (held constant vs ON, NOT an AQ lever): PYTHIA_SYNTHESIZER_PROVIDER=openai (gpt-5.5),
#   PYTHIA_METAGATE_ENABLED=1 + PYTHIA_METAGATE_MODE=shadow.
#   app-side: NO AQ answer-quality flags flipped (practical_block / intake_panel / profile_context / clarify /
#   sme_roles / authority_registry / budget_precheck / claim_ledger_export / boundary_spend_gate all default-OFF).
set -u
cd "$(dirname "$0")/../.."; APP="$(pwd)"; ENG="$APP/../engine"; LOG="$APP/logs"; mkdir -p "$LOG"
[ "${1:-}" = "stop" ] && { ./scripts/local/run_app_local.sh stop; exit 0; }

# AQ-06 (WS0 step 7): the engine dumps its fully-resolved settings (secrets redacted) here at boot, so the
# exact OFF posture this run booted with is an on-disk artifact the harness manifest can reference.
export AQ_ENV_MANIFEST_DIR="$APP/manifests"; mkdir -p "$AQ_ENV_MANIFEST_DIR"
KEY="$(cd "$APP" && "$APP/.venv/bin/python" -c "from regsuper.config import get_settings; print(get_settings().openai_api_key or '')")"
[ -n "$KEY" ] || { echo "no OpenAI key"; exit 1; }

echo "Stopping current stack …"; ./scripts/local/run_app_local.sh stop >/dev/null 2>&1 || true; sleep 2

# CLEAN OFF engine :8099 — gpt-5.5 + metagate SHADOW held constant; EVERY AQ answer-quality lever at default.
# NOTE: we set ONLY the two constants + stage_latency (observe-only). Every other AQ flag is DELIBERATELY
# ABSENT so the committed default (all OFF / shadow) applies — this is what makes it the clean "before".
echo "Starting engine :8099 — CLEAN OFF BASELINE (gpt-5.5; metagate SHADOW; all AQ levers default-OFF) …"
( cd "$ENG" && env \
  PYTHIA_DATABASE_URL="postgresql+asyncpg://regsuper:regsuper@localhost:5432/pythia" \
  PYTHIA_OPENAI_API_KEY="$KEY" PYTHIA_SYNTHESIZER_PROVIDER=openai \
  PYTHIA_METAGATE_ENABLED=1 PYTHIA_METAGATE_MODE=shadow \
  PYTHIA_STAGE_LATENCY_ENABLED=1 \
  nohup "$ENG/.venv/bin/uvicorn" pythia.service:app --host 127.0.0.1 --port 8099 \
    > "$LOG/engine.log" 2>&1 < /dev/null & )
for i in $(seq 1 60); do curl -s -m 3 http://127.0.0.1:8099/health | grep -q '"status"' && { echo "engine up (${i}s)"; break; }; sleep 1; done

# Backend :8002 + FE for PARITY with run_aq_all.sh (the harness targets the engine /v1/answer :8099 directly,
# so app-side AQ flags are irrelevant to the OFF measurement — none are flipped here). This just gives you the
# same running shape. MODEL_CHEAP is the one non-AQ correctness env (the enforce compliance gate model id);
# harmless on the OFF path since compliance-enforce is not part of the direct /v1/answer measurement.
export MODEL_CHEAP=gpt-4o-mini
echo "Starting backend :8002 + FE (run_everything full posture, NO AQ answer-quality flags) …"
./scripts/local/run_app_local.sh
echo "CLEAN OFF BASELINE up. Score it with:"
echo "  cd $ENG && .venv/bin/python eval/aq_answer_eval.py --posture-tag off --limit 6 \\"
echo "      --out eval/artifacts/deploy_off_slice.json"
