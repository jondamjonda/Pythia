"""TC-3b — the AS-OF RENDERER proof (additive, gated default-OFF, read-only).

The render half TC-3 split off. TC-3 added LLM-prose temporal caveats; TC-3b serves the version-correct
STATUTORY TEXT in force at a PAST as_of on the /v1/retrieve path (CitedProvision.text_md). The TMP-03
overlay POPULATES Seed.asof_text / NeighborHit.asof_text with the [valid_from,valid_to)-window text from
provision_version; before TC-3b engine.retrieve()/_decorate always served the HEAD text and DROPPED
asof_text (the seed→NeighborHit rebuild dropped it; _decorate ignored the graph hits' own copy).

Proves the TC-3b invariants — NO DB (the pure decorate path + an injected fake Nexus):

  (a) OFF byte-identical    — render_asof=False ⇒ text_md is the HEAD text truncated to 240, IDENTICAL
      whether or not the hit carries an asof_text (the field is inert while OFF).
  (b) ON serves asof_text   — render_asof=True ⇒ a hit whose asof_text is present serves THAT text (the
      version-correct past text), truncated identically to 240.
  (c) ON honest fallback    — render_asof=True but asof_text absent/empty ⇒ the HEAD text (a hit with no
      version match, or a None/today/future as_of, is never blanked).
  (d) truncation identical  — both the head and the asof text take the SAME [:240] slice.
  (e) seed-merge threading  — the END-TO-END retrieve() path: a SEED carrying asof_text keeps it through the
      seed→NeighborHit rebuild (the engine.py fix) and, with render_asof ON, its text_md is the asof text;
      with render_asof OFF the SAME pipeline serves the head text (the seed rebuild carries inert data).
  (f) other facets untouched — ON vs OFF return the IDENTICAL provision_id, order, trust facets and score;
      ONLY text_md differs for a hit that carries a past-version text.

All of this is DOUBLY INERT on the live single-version corpus (asof_text is only ever populated by the
substrate at a strictly-past as_of with a matching provision_version window; max version_no=1 today ⇒ the
overlay reads the head text anyway ⇒ ON ≡ OFF until a real amend produces a v2 row).
"""

from __future__ import annotations

import asyncio

import pytest

import pythia.engine as engine_mod
from pythia.config import PythiaSettings
from pythia.contract import PythiaQuery
from pythia.engine import PythiaEngine
from pythia.ports import ExpandResult, NeighborHit, ProvisionRow, Seed

_HEAD = "HEAD version text — the current head of the provision."
_ASOF = "OLD version text — what was in force at the past as_of date."
_LONG_HEAD = "H" * 600
_LONG_ASOF = "A" * 600


def _row(pid: str, text: str) -> ProvisionRow:
    """A minimal ProvisionRow (resolve_trust reads only columns — no DB)."""
    return ProvisionRow(
        id=pid,
        label="§ 1",
        text=text,
        jurisdiction="CZ",
        legal_hierarchy="ZAKON",
        valid_from=None,
        valid_to=None,
        source_title="Test Act",
        external_id="1/2020",
        bindingness="binding",
        is_authoritative=True,
        temporal_status="IN_FORCE",
        analysis_roles=None,
    )


def _hit(pid: str, *, asof_text=None) -> NeighborHit:
    return NeighborHit(pid, hops=0, score=1.5, relation_path=["seed"], asof_text=asof_text)


def _decorate(hits, prefetched, *, render_asof):
    return asyncio.run(PythiaEngine()._decorate(hits, as_of=None, prefetched=prefetched, render_asof=render_asof))


# ── (a) OFF byte-identical — asof_text inert while OFF ──────────────────────────────────────────────
@pytest.mark.parametrize("asof_text", [None, _ASOF, "", _LONG_ASOF])
def test_off_serves_head_text_regardless_of_asof(asof_text):
    pid = "11111111-1111-1111-1111-111111111111"
    out = _decorate([_hit(pid, asof_text=asof_text)], {pid: _row(pid, _HEAD)}, render_asof=False)
    assert out[0].text_md == _HEAD[:240]


def test_off_long_head_truncated_to_240():
    pid = "22222222-2222-2222-2222-222222222222"
    out = _decorate([_hit(pid, asof_text=_LONG_ASOF)], {pid: _row(pid, _LONG_HEAD)}, render_asof=False)
    assert out[0].text_md == _LONG_HEAD[:240]
    assert len(out[0].text_md) == 240


# ── (b) ON serves the past-version asof_text when present ───────────────────────────────────────────
def test_on_serves_asof_text_when_present():
    pid = "33333333-3333-3333-3333-333333333333"
    out = _decorate([_hit(pid, asof_text=_ASOF)], {pid: _row(pid, _HEAD)}, render_asof=True)
    assert out[0].text_md == _ASOF[:240]
    assert out[0].text_md != _HEAD[:240]


def test_on_long_asof_truncated_to_240_identically():
    pid = "44444444-4444-4444-4444-444444444444"
    out = _decorate([_hit(pid, asof_text=_LONG_ASOF)], {pid: _row(pid, _LONG_HEAD)}, render_asof=True)
    assert out[0].text_md == _LONG_ASOF[:240]
    assert len(out[0].text_md) == 240


# ── (c) ON honest fallback — absent/empty asof ⇒ head text (never blank) ────────────────────────────
@pytest.mark.parametrize("asof_text", [None, ""])
def test_on_falls_back_to_head_when_asof_absent(asof_text):
    pid = "55555555-5555-5555-5555-555555555555"
    out = _decorate([_hit(pid, asof_text=asof_text)], {pid: _row(pid, _HEAD)}, render_asof=True)
    assert out[0].text_md == _HEAD[:240]


# ── (f) ON vs OFF — only text_md differs; every other facet is identical ────────────────────────────
def test_on_vs_off_only_text_md_differs():
    pid = "66666666-6666-6666-6666-666666666666"
    pref = {pid: _row(pid, _HEAD)}
    off = _decorate([_hit(pid, asof_text=_ASOF)], pref, render_asof=False)[0]
    on = _decorate([_hit(pid, asof_text=_ASOF)], pref, render_asof=True)[0]
    assert off.text_md == _HEAD[:240]
    assert on.text_md == _ASOF[:240]
    # everything else identical
    assert (
        off.provision_id,
        off.label,
        off.jurisdiction,
        off.legal_hierarchy,
        off.source_title,
        off.score,
        off.hops,
    ) == (on.provision_id, on.label, on.jurisdiction, on.legal_hierarchy, on.source_title, on.score, on.hops)
    assert off.trust == on.trust
    assert off.temporal_status == on.temporal_status


# ── (e) seed-merge threading — the end-to-end retrieve() path ───────────────────────────────────────
class _FakeNexus:
    """Minimal Nexus: one seed carrying asof_text, a head-text ProvisionRow. `graph_asof` (when set) makes
    expand() return the SAME provision id as a graph hit carrying its OWN asof_text — the both-seed-and-graph
    collision (engine merges via setdefault → the SEED entry must win). score_semantic supports the fusion path."""

    def __init__(self, pid: str, head: str, asof: str | None, *, graph_asof: str | None = None) -> None:
        self._pid, self._head, self._asof, self._graph_asof = pid, head, asof, graph_asof

    async def search(self, query, *, k=8, jurisdiction=None, as_of=None, rerank=True, rerank_pool=None):
        return [Seed(provision_id=self._pid, score=0.9, asof_text=self._asof)]

    async def expand(self, query, seed_ids, *, method="cte", k=20, max_hops=3, as_of=None, include_case_law=False):
        if self._graph_asof is None:
            return ExpandResult([])
        return ExpandResult(
            [NeighborHit(self._pid, hops=1, score=0.3, relation_path=["refers_to"], asof_text=self._graph_asof)]
        )

    async def score_semantic(self, query, ids):
        return {i: 0.5 for i in ids}  # fusion lane: a uniform cosine for the id(s)

    async def fetch_provisions(self, ids):
        return [_row(self._pid, self._head)]


def _patch_settings(monkeypatch, *, render_asof: bool, fusion: bool = False) -> None:
    s = PythiaSettings()
    s.temporal_asof_render_enabled = render_asof
    s.fusion_enabled = fusion  # OFF → prefetch-free fetch_provisions path
    monkeypatch.setattr(engine_mod, "get_settings", lambda: s, raising=True)


def _run_retrieve(pid, head, asof, **kw):
    eng = PythiaEngine()
    eng.nexus = _FakeNexus(pid, head, asof, **kw)
    return asyncio.run(eng.retrieve(PythiaQuery(query="q", k=10)))


def test_seed_asof_survives_rebuild_and_renders_on(monkeypatch):
    pid = "77777777-7777-7777-7777-777777777777"
    _patch_settings(monkeypatch, render_asof=True)
    ans = _run_retrieve(pid, _HEAD, _ASOF)
    assert not ans.refused
    assert ans.cited_provisions[0].text_md == _ASOF[:240]  # seed's asof_text threaded through + rendered


def test_seed_asof_threaded_but_inert_off(monkeypatch):
    pid = "88888888-8888-8888-8888-888888888888"
    _patch_settings(monkeypatch, render_asof=False)
    ans = _run_retrieve(pid, _HEAD, _ASOF)
    assert not ans.refused
    assert ans.cited_provisions[0].text_md == _HEAD[:240]  # SAME pipeline, OFF ⇒ head text (asof inert)


def test_asof_survives_fusion_rerank(monkeypatch):
    # fusion_enabled ON: fuse() mutates the NeighborHits in place (sets sem/fused/why) and returns the SAME
    # objects — asof_text must survive into _decorate (which reads the prefetched rows on the fusion path).
    pid = "99999999-9999-9999-9999-999999999999"
    _patch_settings(monkeypatch, render_asof=True, fusion=True)
    ans = _run_retrieve(pid, _HEAD, _ASOF)
    assert not ans.refused
    assert ans.cited_provisions[0].text_md == _ASOF[:240]


def test_both_seed_and_graph_collision_seed_asof_wins(monkeypatch):
    # a provision that is BOTH a seed AND a graph dst: merged.setdefault keeps the SEED entry (built carrying the
    # seed's asof_text); the graph hit is discarded. In production both copies are the SAME text (same as_of,
    # same _asof_version_text) — here they differ ONLY to prove which one wins.
    pid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    seed_asof, graph_asof = "SEED as-of text", "GRAPH as-of text (discarded)"
    _patch_settings(monkeypatch, render_asof=True)
    ans = _run_retrieve(pid, _HEAD, seed_asof, graph_asof=graph_asof)
    assert not ans.refused
    assert ans.cited_provisions[0].text_md == seed_asof[:240]
    assert graph_asof[:240] not in (ans.cited_provisions[0].text_md,)
