#!/usr/bin/env bash
# run_aq_lite.sh — LITE serving posture. A MEMORY-TIGHT, LOCAL, gpt-5.5-off-box test posture.
#
# WHAT THIS IS: the smallest AQ flag set that still delivers the actual goal — a STRUCTURED
# plain-language answer whose citations are GROUNDED/VERIFIED (shadow) — with every heavy
# retrieval enhancer, extra-LLM-call, concurrency, temporal, and discovery feature turned OFF.
# It exists because "everything ON" (run_aq_local.sh / run_aq_all.sh) does not fit a tight Mac.
#
# WHY LITE FITS WHERE FULL DOES NOT — the memory floor is BASELINE, not the flags:
#   The dominant resident cost is three UNCONDITIONAL consumers, not any PYTHIA_ flag:
#     1. the OUT-OF-PROCESS local Ollama qwen2.5:14b synthesizer  ~9-10 GB
#     2. the cross-encoder reranker bge-reranker-v2-m3            ~2-2.5 GB  (HARDCODED ON,
#        engine.py:32 self.seed_rerank=True — NO env flag to disable; see HUMAN FIX below)
#     3. the bge-m3 bi-encoder                                    ~1.5-2.5 GB (unconditional)
#   i.e. a ~13-15 GB everything-resident floor that no flag can touch.
#   LITE becomes feasible ONLY by moving synthesis off-box: PYTHIA_SYNTHESIZER_PROVIDER=openai
#   (gpt-5.5) reclaims the biggest ~9-10 GB. This is REQUIRED, not a compromise — the requested
#   posture keeps gpt-5.5 anyway. Keeping RETRIEVAL_CONCURRENT_LEGS OFF then prevents the N-way
#   bge-m3 activation spike that is the most likely cause of ':8099 unreachable mid-answer' OOM.
#
#   ✓ RERANKER NOW TOGGLEABLE (fix applied 2026-07-09):
#     engine.py:32 now reads self.seed_rerank = get_settings().seed_rerank_enabled (default True).
#     LITE sets PYTHIA_SEED_RERANK_ENABLED=0 below to drop the ~2-2.5 GB cross-encoder — the
#     bi-encoder cosine seed still returns a valid ranked shortlist (lower ranking quality, no crash).
#     With gpt-5.5 off-box + reranker off + concurrent-legs off, the engine floor falls to roughly
#     bge-m3 + torch ≈ 2-3.5 GB — Mac-feasible. Set PYTHIA_SEED_RERANK_ENABLED=1 to restore it.
#
#   ── TURN FEATURES BACK ON ONE BY ONE ──────────────────────────────────────────────────
#   Every heavy feature below is OFF in LITE. To re-enable one, export its VAR=1 BEFORE running
#   this script (or edit its line in the engine block). CHILD flags need their PARENT ON first
#   (parent listed → child). Re-enable one at a time and watch ./logs/engine.log + memory.
#
#   RETRIEVAL ENHANCERS (add MPS/bge compute per answer):
#     export PYTHIA_SEED_RERANK_ENABLED=1          # restore the ~2-2.5GB cross-encoder reranker (better ranking; the biggest re-add)
#     export PYTHIA_ANSWER_PATH_FUSION_ENABLED=1   # extra bge-m3 encode per leg (re-rank only)
#     export PYTHIA_SEED_TOKEN_IDF_ENABLED=1       # model-free, A/B benefit unproven (gate #3)
#     export PYTHIA_SEED_POOL_SCALE_ENABLED=1      # cross-encoder scores up to 48 cand/leg (more MPS)
#     export PYTHIA_RETRIEVAL_QUOTAS_ENABLED=1     # k_eff up to 24 → bigger gpt-5.5 GENERATE load
#     export PYTHIA_FUSION_ENABLED=1               # global fusion; B3-gated, no-op on answer path
#     export PYTHIA_CLAUSE_READ_ENABLED=1          # extra bge-ANN leg (shares singleton, +compute)
#     export PYTHIA_TERM_NORM_ENABLED=1            # extra sparse DB leg + RRF; lawyer-review-pending (gate #1)
#     export PYTHIA_EVIDENCE_BUNDLE_ENABLED=1      # bounded read-only DB fan-out (neighbor promotion)
#
#   CONCURRENCY (PRIME OOM RISK — re-enable last, only after the reranker un-hardcode):
#     export PYTHIA_RETRIEVAL_CONCURRENT_LEGS_ENABLED=1   # PARENT: N simultaneous bge-m3 forward passes
#       export PYTHIA_ANSWER_LEG_CONCURRENCY=4            #   child: needs the parent ON first
#
#   EXTRA-LLM-CALL FEATURES (each adds ≥1 network LLM call per answer; not needed for LITE):
#     export PYTHIA_STRUCTURED_PLAN_LLM_ENABLED=1  # child: needs PYTHIA_STRUCTURED_ANSWER_ENABLED (already ON)
#     export PYTHIA_INTAKE_V2_ENABLED=1            # PARENT: one extra cheap-tier LLM call per answer
#       export PYTHIA_INTAKE_V2_MODE=active        #   child: needs INTAKE_V2 parent ON (active = heavier retrieval)
#       export PYTHIA_INTAKE_CLARIFY_ENABLED=1     #   child: needs INTAKE_V2 parent ON (extra bge encode)
#     export PYTHIA_METAGATE_ENABLED=1             # PARENT: up to 3 extra cheap-tier LLM calls; ENFORCE over-refuses
#       export PYTHIA_METAGATE_MODE=enforce        #   child: needs METAGATE parent ON (lawyer-gated; keep shadow)
#       export PYTHIA_METAGATE_TRUE_COSINE_ENABLED=1  # child: needs METAGATE parent ON (extra bge encode)
#
#   CLAIM-VERIFY SUB-FLAGS (parent PYTHIA_CLAIM_VERIFY_ENABLED is already ON in LITE, in shadow):
#     export PYTHIA_CLAIM_VERIFY_GAP_PROBE_ENABLED=1   # child: extra DB reads; enables promotion
#     export PYTHIA_CLAIM_ENTAILMENT_LLM_ENABLED=1     # child: extra cheap-tier LLM + worker thread per ambiguous claim
#     export PYTHIA_CLAIM_REPAIR_LLM_ENABLED=1         # child: needs CLAIM_VERIFY parent ON AND
#       export PYTHIA_CLAIM_VERIFY_MODE=enforce        #   ...MODE=enforce (lawyer-gated, gate #4) — heaviest LLM sub-flag
#
#   CONTEXT / DRAFT / TEMPORAL / DISCOVERY (mostly free but user-invisible or inert on this corpus):
#     export PYTHIA_CTX_CLAUSE_SNAP_ENABLED=1      # child: needs PYTHIA_CTX_WINDOW_ENABLED (already ON); +DB round-trip
#     export PYTHIA_DRAFT_PLANNER_ENABLED=1        # pure-python; only visible via the claim-verify atomizer
#     export PYTHIA_TEMPORAL_CONSUMER_ENABLED=1    # display-only PIT facets; inert on single-version corpus
#     export PYTHIA_TEMPORAL_PROSE_GUARD_ENABLED=1 # temporal caveats in prompt; inert on IN_FORCE corpus
#     export PYTHIA_DISCOVERY_ENABLED=1            # background publication-feed watcher; NOT on the answer path
#
#   APP-SIDE FEATURES (re-enable in the app block below; some need an engine parent):
#     export ANSWER_INTAKE_PANEL_ENABLED=1         # PARENT (app): inert unless engine intake_v2 attaches audit_trace['intake']
#       export ANSWER_PRACTICAL_BLOCK_ENABLED=1    #   child (app): needs INTAKE_PANEL parent; also G14 lawyer-review-pending copy
#     export ANSWER_PROFILE_CONTEXT_ENABLED=1      # app: extra DB read to seed intake (pair with intake_v2)
#     export ANSWER_CLARIFY_ENABLED=1              # app: deterministic 'ask before thin questions' path (one DB read)
#     export AUTHORITY_REGISTRY_ENABLED=1          # app: 'kam se obrátit' authorities (needs the AUTHORITY_*_PATH seeds set)
#     export SME_ROLES_ENABLED=1                   # child (app): needs entity_scoping_enabled; not on the /api/answer path anyway
#     export CLAIM_LEDGER_EXPORT_ENABLED=1         # app: background disk write of the ledger
#     # SPEND CAPS — do NOT flip on this local box unless you set realistic caps first; under the
#     # live $1/day TEST cap these REFUSE every local answer:
#     #   export ANSWER_BUDGET_PRECHECK_ENABLED=1
#     #   export PYTHIA_BOUNDARY_SPEND_GATE_ENABLED=1
#   ──────────────────────────────────────────────────────────────────────────────────────
#
# Committed config defaults are UNCHANGED (this is a launch-env script only).
#   ./scripts/local/run_aq_lite.sh        → stop, relaunch LITE (gpt-5.5 off-box), print the URL to open
#   ./scripts/local/run_aq_lite.sh stop   → stop
#
# NOT a public/paid serving authorization. LITE = deliberately REDUCED features (no metagate,
# no extra-LLM verify loop, no retrieval enhancers, shadow claim-verify) for a memory-tight box.
set -u
cd "$(dirname "$0")/../.."; APP="$(pwd)"; ENG="$APP/../engine"; LOG="$APP/logs"; mkdir -p "$LOG"
[ "${1:-}" = "stop" ] && { ./scripts/local/run_app_local.sh stop; exit 0; }

export AQ_ENV_MANIFEST_DIR="$APP/manifests"; mkdir -p "$AQ_ENV_MANIFEST_DIR"

echo "Stopping current stack …"; ./scripts/local/run_app_local.sh stop >/dev/null 2>&1 || true; sleep 2
# hard-kill any stray engine so our LITE engine owns :8099
pkill -f "uvicorn pythia.service" 2>/dev/null || true
lsof -ti:8099 2>/dev/null | xargs kill 2>/dev/null || true
sleep 2

# gpt-5.5 is OFF-BOX (reclaims the ~9-10 GB local Ollama 14B). Read the OpenAI key from app config
# the same way run_app_local.sh does; it stays local and is never printed.
KEY="$("$APP/.venv/bin/python" -c "from regsuper.config import get_settings; print(get_settings().openai_api_key or '')")"
[ -n "$KEY" ] || echo "  ⚠ No OpenAI key in config — with PYTHIA_SYNTHESIZER_PROVIDER=openai the answer feature will not synthesize."

echo "Starting engine :8099 — LITE posture (core ON, heavy OFF), synthesizer=OPENAI gpt-5.5 off-box; metagate OFF; claim-verify SHADOW …"
( cd "$ENG" && env \
  PYTHIA_DATABASE_URL="postgresql+asyncpg://regsuper:regsuper@localhost:5432/pythia" \
  PYTHIA_OPENAI_API_KEY="$KEY" \
  PYTHIA_SYNTHESIZER_PROVIDER=openai \
  PYTHIA_SEED_RERANK_ENABLED=0 \
  `# ── CORE-KEEP (minimal useful grounded structured answer) ──` \
  PYTHIA_STRUCTURED_ANSWER_ENABLED=1 \
  PYTHIA_CLAIM_VERIFY_ENABLED=1 PYTHIA_CLAIM_VERIFY_MODE=shadow \
  PYTHIA_CTX_WINDOW_ENABLED=1 \
  PYTHIA_SOFTLAW_QUOTA_ENABLED=1 \
  PYTHIA_STAGE_LATENCY_ENABLED=1 \
  PYTHIA_QUALITY_TRIPWIRES_ENABLED=1 \
  `# ── HEAVY-DROP (retrieval enhancers / concurrency / extra-LLM / temporal / discovery) ──` \
  PYTHIA_FUSION_ENABLED=0 \
  PYTHIA_ANSWER_PATH_FUSION_ENABLED=0 \
  PYTHIA_SEED_TOKEN_IDF_ENABLED=0 \
  PYTHIA_SEED_POOL_SCALE_ENABLED=0 \
  PYTHIA_RETRIEVAL_QUOTAS_ENABLED=0 \
  PYTHIA_RETRIEVAL_CONCURRENT_LEGS_ENABLED=0 \
  PYTHIA_CLAIM_VERIFY_GAP_PROBE_ENABLED=0 \
  PYTHIA_CLAIM_ENTAILMENT_LLM_ENABLED=0 \
  PYTHIA_CLAIM_REPAIR_LLM_ENABLED=0 \
  PYTHIA_INTAKE_V2_ENABLED=0 PYTHIA_INTAKE_V2_MODE=shadow \
  PYTHIA_INTAKE_CLARIFY_ENABLED=0 \
  PYTHIA_STRUCTURED_PLAN_LLM_ENABLED=0 \
  PYTHIA_DRAFT_PLANNER_ENABLED=0 \
  PYTHIA_TERM_NORM_ENABLED=0 \
  PYTHIA_CTX_CLAUSE_SNAP_ENABLED=0 \
  PYTHIA_EVIDENCE_BUNDLE_ENABLED=0 \
  PYTHIA_TEMPORAL_CONSUMER_ENABLED=0 \
  PYTHIA_TEMPORAL_PROSE_GUARD_ENABLED=0 \
  PYTHIA_DISCOVERY_ENABLED=0 \
  PYTHIA_METAGATE_ENABLED=0 PYTHIA_METAGATE_MODE=shadow PYTHIA_METAGATE_TRUE_COSINE_ENABLED=0 \
  PYTHIA_CLAUSE_READ_ENABLED=0 \
  nohup "$ENG/.venv/bin/uvicorn" pythia.service:app --host 127.0.0.1 --port 8099 \
    > "$LOG/engine.log" 2>&1 < /dev/null & )
for i in $(seq 1 90); do curl -s -m 3 http://127.0.0.1:8099/health | grep -q '"status"' && { echo "engine up (${i}s)"; break; }; sleep 1; done

# ── App-side flags (LITE). COMPLIANCE_GUARD_MODE=shadow is consumed by run_everything.sh's overridable line. ──
export COMPLIANCE_GUARD_MODE=shadow
export ANSWER_ENDPOINT_ENABLED=1            # the /api/answer serving surface must be ON to serve at all
export NEXT_PUBLIC_ANSWER_VIEW_ENABLED=1    # the /answer UI
# heavy / inert / spend-cap app features OFF (see TURN FEATURES BACK ON ONE BY ONE above)
export ANSWER_PRACTICAL_BLOCK_ENABLED=0
export ANSWER_INTAKE_PANEL_ENABLED=0
export ANSWER_PROFILE_CONTEXT_ENABLED=0
export ANSWER_CLARIFY_ENABLED=0
export SME_ROLES_ENABLED=0
export AUTHORITY_REGISTRY_ENABLED=0
export CLAIM_LEDGER_EXPORT_ENABLED=0
export ANSWER_BUDGET_PRECHECK_ENABLED=0     # would refuse every answer under the live $1/day TEST cap
export PYTHIA_BOUNDARY_SPEND_GATE_ENABLED=0 # same spend-cap hazard, client layer

echo "Starting backend :8002 + FE (compliance=SHADOW, LITE posture) …"
./scripts/local/run_app_local.sh
echo "LITE posture up: structured plain-language answer + shadow citation-grounding, gpt-5.5 off-box, heavy features OFF."
echo "This is a REDUCED-feature memory-tight test posture — NOT cleared for public/paid serving."
