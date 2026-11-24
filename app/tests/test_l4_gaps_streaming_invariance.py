"""Wave3 memory hardening — L4 gap-output invariance + bounded in-process cache.

The L4 gap scan was refactored from "load EVERY obligation into one list + join
EVERY client doc into one giant lowercased blob" to a memory-bounded streaming
form (paged obligation keyset + per-doc evidence-term reduction). This file
PINS the contract the refactor must preserve: the SET OF GAPS produced is
identical — only peak memory changes.

It also covers the new bounded in-process embedding LRU (default-OFF, capped).

DB-free: the streaming helpers' DB access is faked in-memory; the real scan /
decision / suppression logic in ``run_l4_gaps`` runs unchanged.
(asyncio_mode=auto → async tests run.)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from regsuper.analysis import l4_gaps
from regsuper.analysis.l4_gaps import EVIDENCE_PATTERNS, _detect_evidence


# --------------------------------------------------------------------------- fixture
def _make_obligation(action: str, supporting: str, *, statement: str | None = None) -> dict:
    return {
        "id": uuid4(),
        "cited_provisions": [uuid4()],
        "cited_documents": [uuid4()],
        "action": action,
        "supporting_text": supporting,
        "statement": statement or f"{action} {supporting}".strip(),
        "applicability": None,
        "actor": None,
        "modal": None,
    }


# A spread of obligations: some trigger evidence keywords, some don't.
def _fixture_obligations() -> list[dict]:
    return [
        _make_obligation("obtain a licence", "the firm must hold a permit"),
        _make_obligation("file a notification", "report annually"),
        _make_obligation("register the entity", "registration with the regulator"),
        _make_obligation("no keywords here at all", "plain narrative text"),
        _make_obligation("appoint a depositary", "custody and audit arrangements"),
        _make_obligation("publish a prospectus", "insurance and guarantee details"),
    ]


# Client docs: 'licence' and 'permit' and 'registration' present; others absent.
def _fixture_docs() -> list[str]:
    return [
        "Our LICENCE is current and the permit is on file.",
        "Registration confirmation from the regulator (2024).",
        "",  # empty doc — skipped exactly like the old loader (`if r[0]`)
    ]


# --------------------------------------------------------------------------- reference (OLD) algorithm
def _old_gap_set(obligations: list[dict], docs: list[str]) -> set[tuple]:
    """The pre-refactor logic, reconstructed verbatim, as the ground truth.

    OLD: doc_blob = "\\n\\n".join(non_empty_texts).lower(); a gap is produced for
    an obligation iff it has evidence AND at least one evidence term is missing
    from doc_blob. Returns a comparable set of (obligation_id, frozenset(missing
    search_terms))."""
    texts = [t for t in docs if t]
    doc_blob = "\n\n".join(texts).lower()
    out: set[tuple] = set()
    for ob in obligations:
        evidence_text = f"{ob['action']} {ob['supporting_text']}".strip()
        evidence = _detect_evidence(evidence_text)
        if not evidence:
            continue
        missing = [e for e in evidence if e.search_term.lower() not in doc_blob]
        if missing:
            out.add((str(ob["id"]), frozenset(e.search_term for e in missing)))
    return out


# --------------------------------------------------------------------------- run_l4_gaps with faked DB I/O
async def _run_new_path(
    obligations: list[dict],
    docs: list[str],
    *,
    page_size: int = 500,
) -> set[tuple]:
    """Drive the REAL run_l4_gaps, faking only DB I/O, and capture the gaps it
    persists. ``page_size`` lets us force multi-page streaming."""
    captured: list[tuple] = []

    async def fake_iter(project_id, *, page_size=page_size):
        # Stream in pages of `page_size`, preserving order — the same contract
        # the real keyset iterator guarantees.
        for i in range(0, len(obligations), max(1, page_size)):
            for ob in obligations[i : i + max(1, page_size)]:
                yield ob

    texts = [t for t in docs if t]

    async def fake_present(project_id, search_terms, *, page_size=page_size):
        present: set[str] = set()
        for t in (s.lower() for s in search_terms):
            for d in texts:
                if t in d.lower():
                    present.add(t)
                    break
        return present, len(texts)

    async def fake_count(project_id):
        return len(obligations)

    async def fake_existing(project_id):
        return set()

    async def fake_persist(*, project_id, org_id, obligation, missing_evidence, suppressed_by=None):
        captured.append((str(obligation["id"]), frozenset(e.search_term for e in missing_evidence)))
        return uuid4()

    async def fake_get_project(project_id):
        return SimpleNamespace(org_id=uuid4())

    with (
        patch.object(l4_gaps, "_iter_obligation_findings", fake_iter),
        patch.object(l4_gaps, "_present_search_terms", fake_present),
        patch.object(l4_gaps, "_count_obligation_findings", fake_count),
        patch.object(l4_gaps, "_existing_gap_obligations", fake_existing),
        patch.object(l4_gaps, "_persist_gap", fake_persist),
        patch.object(l4_gaps, "get_project", fake_get_project),
        patch.object(l4_gaps, "_audit_l4_call", AsyncMock()),
        patch.object(l4_gaps, "emit", AsyncMock()),
        patch.object(l4_gaps, "require_cascade_law_source", lambda layer: None),
        patch.object(
            l4_gaps,
            "get_settings",
            lambda: SimpleNamespace(
                multi_limb_gap_dedup_enabled=False,
                entity_scoping_gap_suppression_enabled=False,
                law_source="regsuper",
            ),
        ),
    ):
        await l4_gaps.run_l4_gaps(uuid4())
    return set(captured)


async def test_l4_gap_set_identical_before_and_after_refactor():
    """The headline contract: gaps produced by the streaming path == gaps from
    the original blob algorithm, on the same fixture."""
    obligations = _fixture_obligations()
    docs = _fixture_docs()

    expected = _old_gap_set(obligations, docs)
    actual = await _run_new_path(obligations, docs)

    assert actual == expected
    # Sanity: the fixture actually exercises both gap and non-gap branches.
    assert len(expected) >= 3


async def test_l4_gap_set_identical_under_forced_paging():
    """Paging must NOT change the gap set — page_size=1 forces a page per
    obligation/doc; the result must match the single-page run."""
    obligations = _fixture_obligations()
    docs = _fixture_docs()

    one_page = await _run_new_path(obligations, docs, page_size=1000)
    many_pages = await _run_new_path(obligations, docs, page_size=1)
    assert one_page == many_pages == _old_gap_set(obligations, docs)


async def test_l4_report_counts_match_old_semantics():
    """n_obligations_scanned / n_client_documents come from the streamed counts;
    confirm they equal the fixture sizes (non-empty docs only, as the old
    loader counted)."""
    obligations = _fixture_obligations()
    docs = _fixture_docs()
    n_docs_nonempty = len([t for t in docs if t])

    async def fake_iter(project_id, *, page_size=500):
        for ob in obligations:
            yield ob

    async def fake_present(project_id, search_terms, *, page_size=200):
        return set(), n_docs_nonempty

    with (
        patch.object(l4_gaps, "_iter_obligation_findings", fake_iter),
        patch.object(l4_gaps, "_present_search_terms", fake_present),
        patch.object(
            l4_gaps, "_count_obligation_findings", AsyncMock(return_value=len(obligations))
        ),
        patch.object(l4_gaps, "_existing_gap_obligations", AsyncMock(return_value=set())),
        patch.object(l4_gaps, "_persist_gap", AsyncMock(return_value=uuid4())),
        patch.object(
            l4_gaps, "get_project", AsyncMock(return_value=SimpleNamespace(org_id=uuid4()))
        ),
        patch.object(l4_gaps, "_audit_l4_call", AsyncMock()),
        patch.object(l4_gaps, "emit", AsyncMock()),
        patch.object(l4_gaps, "require_cascade_law_source", lambda layer: None),
        patch.object(
            l4_gaps,
            "get_settings",
            lambda: SimpleNamespace(
                multi_limb_gap_dedup_enabled=False,
                entity_scoping_gap_suppression_enabled=False,
                law_source="regsuper",
            ),
        ),
    ):
        report = await l4_gaps.run_l4_gaps(uuid4())

    assert report.n_obligations_scanned == len(obligations)
    assert report.n_client_documents == n_docs_nonempty


# --------------------------------------------------------------------------- present-terms equivalence
async def test_present_terms_equivalent_to_blob_membership():
    """_present_search_terms must decide coverage identically to the old
    `term in "\\n\\n".join(docs).lower()`, INCLUDING the cross-doc boundary:
    a term that would only 'match' by straddling the \\n\\n join must NOT be
    reported present (the search terms are newline-free, so this can't happen)."""
    docs = [
        "ABC contains a Licence reference.",
        "and a PERMIT too",
        "registration here",
    ]

    async def fake_iter(project_id, *, page_size=200):
        for d in docs:
            if d:
                yield d

    terms = [t for _, _, t in EVIDENCE_PATTERNS]
    with patch.object(l4_gaps, "_iter_client_doc_texts", fake_iter):
        present, n = await l4_gaps._present_search_terms(uuid4(), terms)

    blob = "\n\n".join(docs).lower()
    expected = {t.lower() for t in terms if t.lower() in blob}
    assert present == expected
    assert n == len([d for d in docs if d])
    assert "licence" in present and "permit" in present and "registration" in present
    assert "depositary" not in present


# --------------------------------------------------------------------------- bounded in-process cache
def test_bounded_lru_disabled_is_noop():
    from regsuper.retrieval.cache import BoundedLRUCache

    c = BoundedLRUCache(0)
    assert not c.enabled
    c.put("k", [1.0, 2.0])
    assert c.get("k") is None  # disabled → never retains
    assert len(c) == 0


def test_bounded_lru_evicts_least_recently_used():
    from regsuper.retrieval.cache import BoundedLRUCache

    c = BoundedLRUCache(2)
    assert c.enabled
    c.put("a", 1)
    c.put("b", 2)
    assert c.get("a") == 1  # touch 'a' → 'b' becomes LRU
    c.put("c", 3)  # evicts 'b'
    assert c.get("b") is None
    assert c.get("a") == 1 and c.get("c") == 3
    assert len(c) == 2  # hard cap holds


def test_bounded_lru_hit_returns_stored_value_unchanged():
    """Cache-hit correctness: an LRU hit returns exactly what was stored (the
    same property a DB hit would have for deterministic embeddings)."""
    from regsuper.retrieval.cache import BoundedLRUCache

    c = BoundedLRUCache(8)
    vec = [0.1, 0.2, 0.3]
    c.put("key", vec)
    assert c.get("key") == vec


def test_inproc_cache_env_cap_resolution(monkeypatch):
    from regsuper.retrieval import cache

    # Unset env + default_max=0 → inert.
    monkeypatch.delenv(cache._ENV_INPROC_CACHE_MAX, raising=False)
    cache.reset_embed_inproc_cache()
    assert not cache.get_embed_inproc_cache().enabled

    # Env overrides default_max, even forcing OFF with 0.
    monkeypatch.setenv(cache._ENV_INPROC_CACHE_MAX, "0")
    cache.reset_embed_inproc_cache()
    assert not cache.get_embed_inproc_cache(default_max=1000).enabled

    # Positive env → bounded + enabled.
    monkeypatch.setenv(cache._ENV_INPROC_CACHE_MAX, "5")
    cache.reset_embed_inproc_cache()
    c = cache.get_embed_inproc_cache()
    assert c.enabled and c.maxsize == 5

    # Unparseable env → fail-safe OFF.
    monkeypatch.setenv(cache._ENV_INPROC_CACHE_MAX, "not-a-number")
    cache.reset_embed_inproc_cache()
    assert not cache.get_embed_inproc_cache(default_max=10).enabled

    # default_max applies only when env unset.
    monkeypatch.delenv(cache._ENV_INPROC_CACHE_MAX, raising=False)
    cache.reset_embed_inproc_cache()
    assert cache.get_embed_inproc_cache(default_max=123).maxsize == 123

    cache.reset_embed_inproc_cache()
