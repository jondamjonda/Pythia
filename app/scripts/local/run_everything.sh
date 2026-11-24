#!/usr/bin/env bash
# run_everything.sh — FULL-PRODUCT test launch: every READY feature ON + the Pythia/Nexus sidecar (B3 cutover).
# Created 2026-06-29 for end-to-end product testing, with the operator's go + the advocate's G14 sign-off.
#
# NOTE — "everything" means every flag that is WIRED + SAFE. The 47-flag audit found a few that would BREAK the
# product if flipped; they stay OFF on purpose (see the WHAT STAYS OFF block). All of these are LAUNCH-ENV only —
# the committed config defaults are unchanged, so the test suite + any default connection are unaffected.
#
# PRECONDITION: the Pythia engine must be running (checked below). Start it:
#   cd ../engine && PYTHIA_DATABASE_URL=<...> ./.venv/bin/uvicorn pythia.service:app --port 8099
set -euo pipefail
cd "$(dirname "$0")/../.."

PYTHIA_URL="${PYTHIA_URL:-http://127.0.0.1:8099}"
echo "Checking the Pythia / Nexus engine at ${PYTHIA_URL} …"
# LEDGER-L-runtime-02: accept "degraded" as well as "ok". The engine reports
# "degraded" (still HTTP 200) when only Ollama is down — the LLM-free nexus
# retrieval path (/v1/retrieve), which is all law_source=nexus needs, still
# works. Only a missing/unreachable engine (no status / non-200) should refuse.
_health="$(curl -s -m 5 "${PYTHIA_URL}/health" || true)"
if ! printf '%s' "${_health}" | grep -Eq '"status":"(ok|degraded)"'; then
  echo "✋ REFUSING TO LAUNCH: the Pythia engine is not reachable/healthy at ${PYTHIA_URL} (law_source=nexus needs it)." >&2
  exit 1
fi
echo "Engine healthy (${_health})."

# ── Green-gated product features ──────────────────────────────────────────────
export AUDIT_EXPORT_ENABLED=1            # reproducible audit export
export AUDIT_EVIDENCE_ENABLED=1          # evidence-pack endpoint
export PROFILES_ENABLED=1               # SME business-profile CRUD + profile-driven analyze
export DIGEST_ENABLED=1                 # plain-language digest views
# LEDGER-M-flags / M28: L4_MODE is a DEAD flag — it is read NOWHERE in code. L4
# (gap analysis) is ALWAYS the deterministic keyword stub (generated_by
# "rule:l4-keyword-stub-v1"); setting L4_MODE=llm does NOT make L4 call an LLM.
# It is kept here only for parity with the docs, NOT because it changes behaviour.
# SPEND CAP (M28): data/limits.json ships daily_usd=1.0 with block_when_over=true.
# The LLM spend that can trip it is the L2/L3 cascade (NOT L4), so a long full-product
# run can hit BudgetExceeded and abort mid-run. Raise daily_usd in data/limits.json
# for sustained test runs.
export L4_MODE=llm                      # DEAD flag (no consumer) — L4 is always the keyword stub; see above
export REPLAY_ENABLED=1                 # admin replay endpoint
# Control-panel token is left UNSET (matches run_graph_ui.sh / the frontend's empty CONTROL_PANEL_TOKEN), so the
# loopback control panel stays reachable WITHOUT a token exactly as before — setting one here would make the
# backend require a header the UI does not send ("control panel token required").

# ── The applicability initiative (WF-1..WF-6) — all ON ────────────────────────
# SAFE FLAG SET enabled per Wave-2 QC (additive/annotation/read-only; digest output correctness
# depends on the C4 render fix in this same branch). Each only TAGs/annotates or exposes a
# read-only view; none drops a finding. (Note: the bare value "all" is NOT valid for
# DIGEST_CITATION_STRATEGY — the enum is first_only|all_direct|all_with_hierarchy; this run uses
# all_with_hierarchy, which recovers the full cited set AND annotates each ref's hierarchy path.)
export ENTITY_SCOPING_ENABLED=1                 # WF-1 binds-you gate (lawyer-verified)
export APPLICABILITY_CONFIDENCE_ENABLED=1       # WF-2 confidence split
export DIGEST_CITATION_STRATEGY=all_with_hierarchy   # WF-3 recover the full cited set (superset of all_direct)
export MULTI_LIMB_ENABLED=1                     # WF-4 multi-limb + dedup annotation
export TEMPORAL_FRESHNESS_ENABLED=1             # WF-6 temporal freshness annotation
# Gap-suppression (TAG-not-DROP; visible context bucket, never deletes) — WF-4 demotions reviewed (5 genuine dups)
export ENTITY_SCOPING_GAP_SUPPRESSION_ENABLED=1
export MULTI_LIMB_GAP_DEDUP_ENABLED=1
export TEMPORAL_GAP_SUPPRESSION_ENABLED=1

# ── Compliance lawful lane — ENFORCE (G14 signed; patterns narrowed to act-on-behalf only) ────────────────────
export COMPLIANCE_GUARD_ENABLED=1
# Overridable: defaults to enforce (the G14-signed posture), but a caller may pre-set
# COMPLIANCE_GUARD_MODE=shadow for a local $0 test run (the Tier-2 cheap check is hardcoded to the
# OpenAI client; when that account is quota-dead, enforce fail-closes valid questions to needs_review).
export COMPLIANCE_GUARD_MODE="${COMPLIANCE_GUARD_MODE:-enforce}"

# ── Tenancy / RLS — ENFORCED (as regsuper_app) ────────────────────────────────
export DATABASE_URL="postgresql+asyncpg://regsuper_app:regsuper_app_local@localhost:5432/regsuper"
export RLS_ENFORCED=1
export TENANCY_ENABLED=1

# ── B3 cutover — serve CURRENT law from the Nexus engine (the sidecar) ─────────
export LAW_SOURCE=nexus
export CUTOVER_APPROVED=1
export PYTHIA_ENABLED=1
export PYTHIA_URL="${PYTHIA_URL}"

# ── Tiers / entitlements / metering / billing / quota — ON (Phase-C C2 lane; AC-ENT green) ─────
# No longer "breaks the product": require_entitlement is pass-through-safe and the `answer` feature
# column exists (C2 + flip-prep). The sentinel org has an active ADVISOR subscription, so depth
# resolves to full L4 (NOT clamped to lite). Quota (429-before-spend), append-only billing_event,
# and per-analysis metering (analysis_run.cost_usd) are LIVE; billing provider stays the noop.
export ENTITLEMENTS_ENABLED=1

# ── Consumer digest/profiles UI — VISIBLE (the APIs were already on; this links the pages) ─────
export NEXT_PUBLIC_DIGEST_UI_ENABLED=1

# ── Synthesized ANSWER surface — ON (operator override, 2026-06-30) ────────────────────────────
# POST /api/answer + the AnswerView screen serve the synthesized, cited, sectioned answer. The ENGINE
# is launched with PYTHIA_SYNTHESIZER_PROVIDER=openai (gpt-5.5) + PYTHIA_METAGATE_ENABLED=1 +
# PYTHIA_METAGATE_MODE=shadow. SHADOW not enforce: AC-METAGATE is RED, and enforcing the un-tuned gate
# would wrongly refuse ~20% of VALID questions; in shadow the gateway classifies+logs but does NOT block,
# and VERIFY still grounds every citation (refuses if ungrounded). RESIDUAL: an off-corpus question can get
# a related-but-not-exact answer until the eval set is lawyer-reviewed + the gate goes green → then flip
# PYTHIA_METAGATE_MODE=enforce. A paid PUBLIC launch additionally needs the G14 sign-off + EU-AI-Act.
export ANSWER_ENDPOINT_ENABLED=1
# Shadow weak-coverage HEDGED surfacing (operator decision 2026-06-30): for a borderline in-corpus question
# where the engine (shadow/advisory) wrote a GROUNDED, self-hedged draft but the coverage-check flagged it
# weak (related, not an exact match — e.g. AML/GDPR), SURFACE that grounded answer as hedged (banner + caveat)
# instead of refusing. STRICTLY scoped: no_coverage (off-corpus/fake-law) + non-legal still refuse; nothing
# un-cited is ever shown; the engine VERIFY pass still grounds every citation. NOT an enforce flip. Reversible.
export ANSWER_SHADOW_WEAK_COVERAGE_HEDGED=1

# WHAT STAYS OFF (deliberately held — NOT "broken"):
#  • CONDUIT_ENABLED — internet-facing; WP-062 HTTP wiring incomplete + its 4 gates.
#  • auto-ingestion (operator-held) · BILLING_PROVIDER!=noop (no real PSP) · ACCOUNTING/EXPORT_PDF/
#    PROJECT_DELETION (not built) · REQUIRE_AUTH (anonymous = the sentinel org → RLS still scopes to it).

echo "Polarius — FULL MODE. ON: green-gates + WF-1..6 + compliance=ENFORCE + RLS + NEXUS + ENTITLEMENTS/quota/billing/metering + digest UI + SYNTHESIZED ANSWER (gpt-5.5, metagate=SHADOW)."
echo "HELD: metagate=enforce (AC-METAGATE RED → lawyer eval-set + axis work; VERIFY still grounds cites) · conduit · auto-ingestion · real billing PSP."
# Port 8002 = the port the Next.js UI proxies to (frontend/.env.local BACKEND_URL/NEXT_PUBLIC_API_URL). Running
# here (not 8000) is what makes the UI see these flags ON. PYTHIA_DATABASE_URL (the read-only graph corpus) is
# read from secrets.env by config, so the /control/graph page keeps working too.
exec .venv/bin/uvicorn regsuper.api.main:app --reload --host 127.0.0.1 --port 8002 "$@"
