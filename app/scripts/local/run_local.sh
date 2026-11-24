#!/usr/bin/env bash
# run_local.sh — launch the Polarius backend LOCALLY with the GREEN-GATE features turned ON.
#
# Created 2026-06-27. These flags are set in the LAUNCH ENVIRONMENT (not in secrets.env / .env) ON PURPOSE:
# the config + the test suites load those files, so putting flags there would flip the defaults the tests
# assert and turn the suites red. Setting them here affects ONLY this running instance. Fully reversible —
# delete a line to turn that feature back off, or just don't use this script.
#
# WHAT THIS TURNS ON:
#   AUDIT_EXPORT_ENABLED    reproducible audit-export endpoints      POST /api/projects/{id}/export
#   AUDIT_EVIDENCE_ENABLED  the evidence-pack endpoint               POST /api/evidence-pack/projects/{id}
#   PROFILES_ENABLED        SME "describe your business" profile CRUD + profile-driven analyze
#   DIGEST_ENABLED          plain-language digest views
#   L4_MODE=llm             DEAD flag — kept for doc parity only. It is read NOWHERE in code; L4 (gap
#                           analysis) is ALWAYS the deterministic keyword stub ("rule:l4-keyword-stub-v1").
#                           Setting it does NOT make L4 call an LLM (corrected per Wave-2 QC, LEDGER-M-flags).
#                           NOTE: data/limits.json ships a low test spend-cap ($1/day, block_when_over) — the
#                           spend that trips it is the L2/L3 cascade (NOT L4); raise it there for longer runs.
#   SAFE FLAG SET           APPLICABILITY_CONFIDENCE_ENABLED · DIGEST_CITATION_STRATEGY=all_direct ·
#                           MULTI_LIMB_ENABLED · TEMPORAL_FRESHNESS_ENABLED — Wave-2 QC additive/annotation-only
#                           flips (the Wave-1 "recommended flip set"); none drops a finding.
#   ENTITY_SCOPING_ENABLED  the entity/role APPLICABILITY FILTER (WF-1 "binds-you" gate). LAWYER-VERIFIED
#                           (2026-06-29 — the 79-finding B3-06a sample). DETERMINISTIC (no LLM; reads the
#                           already-extracted addressee, so the L2/L3 PROMPT_HASH audit chain is untouched) and
#                           RENDER-DEMOTE ONLY: it adds a "does not bind you directly" digest section + a
#                           binds∈{yes,conditional,out_of_scope} verdict per obligation — it SUPPRESSES NOTHING
#                           (the L4 gap-suppression variant was deliberately NEVER wired → no silent-drop path).
#                           entity_scoping_floor stays 0.0 (inert; no confidence-based demotion) until the
#                           lawyer sets a per-project floor. Reverse by deleting the export line.
#
# WHAT STAYS OFF (gated; do NOT set these until the gate is green):
#   COMPLIANCE_GUARD_MODE   stays 'shadow' (classifies+logs, does NOT block) — enforce needs the G14 lawyer sign-off
#   LAW_SOURCE              stays 'regsuper'. DELIBERATELY NOT set to 'nexus': the cutover code is NOT built
#                           (blocked on F-1), and require_cascade_law_source() FAILS every full analysis LOUD
#                           the moment law_source != 'regsuper' (nexus is preview-only, L1 candidate listing).
#                           Setting it now would BREAK every analysis, not enhance it.
#   RLS_ENFORCED            stays false — needs the cross-tenant leak test (AC-RLS-4)
#   (+ Phase C: TENANCY/CONDUIT/ENTITLEMENTS/BILLING and all the engine 'temporal' consumer flags — postponed)
set -euo pipefail
cd "$(dirname "$0")/../.."

export AUDIT_EXPORT_ENABLED=1
export AUDIT_EVIDENCE_ENABLED=1
export PROFILES_ENABLED=1
export DIGEST_ENABLED=1
# LEDGER-M-flags / M28: L4_MODE is a DEAD flag (read NOWHERE in code). L4 (gap analysis) is
# ALWAYS the deterministic keyword stub ("rule:l4-keyword-stub-v1"); =llm does NOT make L4 call
# an LLM. The LLM spend that can trip the data/limits.json $1/day cap is the L2/L3 cascade, not L4.
export L4_MODE=llm          # DEAD flag (no consumer) — L4 is always the keyword stub; see above
export ENTITY_SCOPING_ENABLED=1   # LAWYER-VERIFIED entity/role applicability filter (render-demote; suppresses nothing) — see above

# SAFE FLAG SET enabled per Wave-2 QC (additive/annotation/read-only; digest output correctness
# depends on the C4 render fix in this same branch).
#   These are the "RECOMMENDED FLIP SET" from the Wave-1 flag catalogue: each only TAGs/annotates
#   or exposes a read-only view; none drops a finding, exposes the internet, or breaks the build.
export APPLICABILITY_CONFIDENCE_ENABLED=1       # WF-2 confidence split (annotation only; rides on entity-scoping)
export DIGEST_CITATION_STRATEGY=all_direct      # WF-3 recover the full cited set (additive field; "all" is not a
                                                #   valid value — the enum is first_only|all_direct|all_with_hierarchy;
                                                #   all_direct = full id-set recovery)
export MULTI_LIMB_ENABLED=1                     # WF-4 multi-limb annotation bag (annotation only)
export TEMPORAL_FRESHNESS_ENABLED=1             # WF-6 temporal freshness annotation (annotation only)

# TENANCY / RLS — ENFORCED (AC-RLS-4 green; migration 015 applied 2026-06-29). The app connects as the
# NOBYPASSRLS login `regsuper_app` so the migration-015 tenant predicate actually bites (the `regsuper` owner
# bypasses RLS — it stays the SYNC/migration role). TenancyMiddleware sets app.current_org per request from the
# principal's org; under require_auth=False that is the sentinel org, which owns all existing local data.
# Proven on live data: as-sentinel sees its rows, as-another-org sees 0; public law tables (no RLS) unaffected.
export DATABASE_URL="postgresql+asyncpg://regsuper_app:regsuper_app_local@localhost:5432/regsuper"
export RLS_ENFORCED=1
export TENANCY_ENABLED=1

echo "Polarius backend — local. ON: export · evidence-pack · profiles · digest · L4=llm · entity-scoping (LAWYER-VERIFIED) · RLS/tenancy ENFORCED (as regsuper_app)."
echo "STAYS OFF (gated): compliance=shadow · law_source=regsuper (nexus needs the Pythia engine up — B3 gate-relax built, flip is a domain add)."
exec .venv/bin/uvicorn regsuper.api.main:app --reload --host 127.0.0.1 --port 8000 "$@"
