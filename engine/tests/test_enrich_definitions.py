"""enrich_definitions — candidate-predicate / prompt-branch unit tests.

DB-free assertions over the SQL strings + the prompt selector (B1a-05/06):
  • the widened predicate matches an EN 'means'/'shall mean' definition provision (EN markers present,
    gated on language='en');
  • the CZ candidate set is a SUPERSET of the old behaviour — the original CZ markers are still present
    verbatim, and no EN marker can pull in a 'cs' row (so CZ behaviour is byte-stable);
  • the --all-missing act resolver is PER-PROVISION (selects acts via missing definition_provision_id,
    not the old act-level scope_document_id NOT EXISTS), so already-partially-defined acts are reached;
  • the per-provision fetch keeps the resume guard and selects language for the prompt branch.

A DB-backed test (skipped when `pythia` is unreachable) further asserts the widened predicate selects
a known already-partially-defined act's still-missing CZ provisions and finds EN candidates.
"""

from __future__ import annotations

import asyncio

import pytest

from pythia.ingest import enrich_definitions as ed


# ── DB-free: the widened candidate predicate ─────────────────────────────────────────────────────
def test_en_markers_present_in_predicate():
    # B1a-06: EN definition markers ('means'/'shall mean'/'for the purposes of this'/'the following
    # definitions') are now part of the candidate predicate.
    pred = ed._CANDIDATE_PREDICATE
    assert "%means%" in pred
    assert "%shall mean%" in pred
    assert "%for the purposes of this%" in pred
    assert "%the following definitions%" in pred


def test_en_markers_gated_on_language_en():
    # The EN branch is conjoined with sd.language = 'en' — an EN marker can NEVER widen the set of
    # 'cs' rows, so the CZ candidate set is byte-stable (strict superset of pre-B1a-06).
    pred = ed._CANDIDATE_PREDICATE
    assert "sd.language = 'en'" in pred
    en_branch = ed._EN_MARKERS
    # every EN marker sits inside the language-gated branch, not the ungated CZ branch
    assert en_branch in pred
    assert en_branch not in ed._CZ_MARKERS


def test_cz_markers_unchanged_superset():
    # The original CZ markers must be present verbatim (no regression / no dropped CZ candidate).
    assert "p.text ILIKE '%se rozumí%'" in ed._CANDIDATE_PREDICATE
    assert "p.text ILIKE '%pro účely tohoto zákona%'" in ed._CANDIDATE_PREDICATE
    # …and the CZ marker fragment is NOT language-gated (fires on any row, as before).
    assert "se rozumí" in ed._CZ_MARKERS
    assert "language" not in ed._CZ_MARKERS


def test_predicate_shared_by_act_resolver_and_fetch():
    # Both the --all-missing act resolver and the per-provision fetch use the SAME widened predicate,
    # so resolution and enrichment never disagree on what a candidate is.
    assert ed._CANDIDATE_PREDICATE in ed._MISSING_PROVISION_ACTS
    assert ed._CANDIDATE_PREDICATE in ed._CANDIDATE_PROVISIONS


# ── DB-free: per-provision scope (B1a-05) ────────────────────────────────────────────────────────
def test_all_missing_resolver_is_per_provision():
    sql = ed._MISSING_PROVISION_ACTS
    # reaches acts via a missing PER-PROVISION definition link …
    assert "definition_provision_id = p.id" in sql
    # … and joins provision→source_document (act-level scope_document_id guard is gone).
    assert "FROM provision p" in sql
    assert "JOIN source_document sd" in sql
    assert "scope_document_id" not in sql
    assert "CASE_LAW" in sql  # rulings still excluded


def test_fetch_keeps_resume_guard_and_selects_language():
    sql = ed._CANDIDATE_PROVISIONS
    # per-provision resume guard preserved (re-runnable) …
    assert "NOT EXISTS (SELECT 1 FROM defined_term dt WHERE dt.definition_provision_id = p.id)" in sql
    # … and language is selected so the loop can pick the CZ vs EN prompt branch.
    assert "sd.language lang" in sql


# ── DB-free: prompt branch selection (B1a-06) ────────────────────────────────────────────────────
def test_prompt_branch_selection():
    assert ed._prompt_for("en") is ed._PROMPT_EN
    assert ed._prompt_for("EN") is ed._PROMPT_EN  # case-insensitive
    assert ed._prompt_for("cs") is ed._PROMPT
    assert ed._prompt_for(None) is ed._PROMPT  # default to CZ
    assert ed._prompt_for("") is ed._PROMPT
    # the EN prompt is genuinely English (matches the EU 'means' drafting style)
    assert "means" in ed._PROMPT_EN.lower()
    assert "{text}" in ed._PROMPT_EN and "{text}" in ed._PROMPT


# ── DB-backed (skipped when `pythia` is unreachable) ─────────────────────────────────────────────
async def _maybe_pool():
    try:
        from pythia.db import get_pool

        return await asyncio.wait_for(get_pool(), timeout=3.0)
    except Exception:
        return None


@pytest.mark.asyncio
async def test_predicate_selects_en_and_skipped_cz_live():
    pool = await _maybe_pool()
    if pool is None:
        pytest.skip("pythia DB not reachable")
    async with pool.acquire() as c:
        # at least one EN provision is now a candidate (was 0 under the CZ-only filter)
        n_en = await c.fetchval(f"""
            SELECT count(*) FROM provision p
              JOIN source_document sd ON sd.id = p.source_document_id
             WHERE sd.language = 'en' AND ({ed._CANDIDATE_PREDICATE})
               AND NOT EXISTS (SELECT 1 FROM defined_term dt WHERE dt.definition_provision_id = p.id)""")
        assert n_en > 0, "widened predicate must surface EN definition provisions"

        # the per-provision resolver now reaches acts that ALREADY have ≥1 defined_term but still
        # hold un-enriched candidate provisions (the B1a-05 125-provision class).
        n_partial = await c.fetchval(f"""
            SELECT count(DISTINCT sd.external_id) FROM provision p
              JOIN source_document sd ON sd.id = p.source_document_id
             WHERE sd.legal_hierarchy IS DISTINCT FROM 'CASE_LAW'
               AND ({ed._CANDIDATE_PREDICATE})
               AND NOT EXISTS (SELECT 1 FROM defined_term dt WHERE dt.definition_provision_id = p.id)
               AND EXISTS (SELECT 1 FROM defined_term dt2 WHERE dt2.scope_document_id = sd.id)""")
        assert n_partial > 0, "per-provision scope must reach already-partially-defined acts"
