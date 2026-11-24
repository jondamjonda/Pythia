"""T4 — term-normalization retrieval leg + soft-law k-budget cap (LLM-free, DB-free).

Two DEFAULT-OFF mechanics, each with a byte-identical-OFF pin + an ON behaviour proof:
  1. term_norm_enabled — OFF ⇒ hybrid_seed adds NO extra leg/seed (byte-identical); ON ⇒ an alias match
     adds the exact-statutory-term leg + the defining-provision seeds, and a zero-lexical-overlap alias
     ("cookies" → "koncové zařízení") resolves.
  2. softlaw_quota_enabled — OFF ⇒ the k-selection is the plain rrf top-k; ON ⇒ soft-law share is capped
     at ceil(k/3) by DEMOTING excess soft-law below the binding hits, never dropping a binding provision.

All pure/stubbed: the lexicon is injected, the alias file is parsed from the repo, no live DB is touched.
"""

from __future__ import annotations

from math import ceil

from pythia.config import get_settings
from pythia.graph import seed as seedmod
from pythia.graph import term_norm as tn
from pythia.reasoning.softlaw_quota import (
    SOFT_LAW,
    apply_softlaw_quota,
    default_max_soft,
)


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# Flag defaults — both OFF, so committing this is byte-identical.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_t4_flag_defaults_all_false():
    s = get_settings()
    assert s.term_norm_enabled is False
    assert s.softlaw_quota_enabled is False


# ═════════════════════════════════════════════════════════════════════════════════════════════════════════
# 1. TERM-NORMALIZATION leg.
# ═════════════════════════════════════════════════════════════════════════════════════════════════════════
def _lex(entries):
    """Build a lexicon from (surface, statutory_list, defining_ids, is_alias) tuples (already-folded)."""
    return [
        tn.LexEntry(surface=tn._fold(su), statutory=[tn._fold(x) for x in st], defining_ids=list(ids), is_alias=alias)
        for (su, st, ids, alias) in entries
    ]


def test_alias_file_parses_and_seeds_diacritic_free():
    # the shipped DRAFT alias file loads, has the seed entries, and every surface is folded (no accents).
    aliases = tn._load_aliases()
    assert len(aliases) >= 30
    surfaces = {e.surface for e in aliases}
    assert "cookies" in surfaces  # the flagship zero-overlap entry
    assert "svarcsystem" in surfaces  # folded (no diacritics)
    assert "osvc" in surfaces
    assert "platce dph" in surfaces
    for e in aliases:  # every alias statutory term is folded + non-empty
        assert e.statutory and all(t == tn._fold(t) for t in e.statutory)


def test_match_terms_exact_alias_hit():
    lex = _lex([("cookies", ["koncové zařízení", "elektronická komunikace"], ["prov-1"], True)])
    m = tn.match_terms("musim resit cookies na webu", lex)
    assert len(m) == 1
    assert m[0].kind == "exact"
    assert tn._fold("koncové zařízení") in m[0].statutory
    assert m[0].defining_ids == ["prov-1"]


async def test_match_terms_zero_lexical_overlap_resolves(monkeypatch):
    # THE cardinal case: the query word "cookies" shares no letters with the statutory "koncové zařízení",
    # yet term-norm bridges it to the statutory register term the corpus actually uses.
    lex = _lex([("cookies", ["koncové zařízení"], ["def-cookie"], True)])

    async def _fake_build(*, force=False):
        return lex

    tn._reset_cache()  # A1: drop any stale materialised cache so build_materialized re-reads the patched lexicon
    monkeypatch.setattr(tn, "build_lexicon", _fake_build)
    boost, ids = await tn.term_norm_augmentation("chci vedet pravidla pro cookies")
    assert tn._fold("koncové zařízení") in boost
    assert ids == ["def-cookie"]


def test_aqb2_zero_lexical_alias_surfaces_resolve():
    """AQ-B2 — the DRAFT alias rows appended for the zero_lexical gold cluster must EXACT-match to their
    statutory register via match_terms() over the shipped file. This pins that each newly-added colloquial
    surface (as a layman types it) actually fires its statutory-boost leg. DRAFT-only: no flag is flipped;
    the alias->statute mapping still needs advocate certification before any serving env turns term_norm ON."""
    lex = tn._load_aliases()
    # (question fragment a layman would type, one statutory register term the entry must inject)
    cases = [
        ("chci rozesilat newsletter zakaznikum", "obchodni sdeleni"),
        ("prodavam pres marketplace kaufland", "prodavajici"),
        ("delam dropshipping z ciny", "prodavajici"),
        ("jsem influencer a delam reklamu", "skryta reklama"),
        ("davam zamestnancum home office", "prace na dalku"),
        ("unikla mi databaze zakazniku", "poruseni zabezpeceni osobnich udaju"),
        ("davam mu neco na ruku bez papiru", "nelegalni prace"),
        ("musim davat uctenku v hospode", "doklad o zakoupeni vyrobku"),
        ("beru si na leto brigadnika studenta", "dohoda o provedeni prace"),
        ("cast trzeb si nechavam bokem cerne trzby", "povinnost vest ucetnictvi"),
        ("chci zabor chodniku pred kram", "zvlastni uzivani verejneho prostranstvi"),
    ]
    for query, want_statutory in cases:
        matches = tn.match_terms(query, lex)
        injected = {t for m in matches for t in m.statutory}
        assert tn._fold(want_statutory) in injected, (
            f"query {query!r} did not resolve to statutory term {want_statutory!r}; got {injected}"
        )


def test_match_terms_bigram_alias():
    # a two-word alias ("platce dph") must fire off the adjacent-bigram span, not just single tokens.
    lex = _lex([("platce dph", ["platce dane z pridane hodnoty"], [], True)])
    m = tn.match_terms("jsem platce dph a resim registraci", lex)
    assert m and m[0].kind == "exact"
    assert tn._fold("platce dane z pridane hodnoty") in m[0].statutory


def test_match_terms_trigram_typo_tolerance():
    # a misspelling ("cookis") still resolves via trigram similarity above the threshold.
    lex = _lex([("cookies", ["koncové zařízení"], [], True)])
    m = tn.match_terms("pravidla pro cookis online", lex, trigram_threshold=0.5)
    assert m and m[0].kind in ("exact", "trigram")
    assert tn._fold("koncové zařízení") in m[0].statutory


def test_match_terms_no_hit_is_empty():
    lex = _lex([("cookies", ["koncové zařízení"], [], True)])
    assert tn.match_terms("uplne nesouvisejici dotaz o pocasi", lex, trigram_threshold=0.9) == []


def test_match_terms_empty_lexicon_is_empty():
    assert tn.match_terms("cookies", []) == []


async def test_augmentation_caps_seeds(monkeypatch):
    # the defining-provision seeds are capped at term_norm_max_seeds.
    monkeypatch.setenv("PYTHIA_TERM_NORM_MAX_SEEDS", "2")
    lex = _lex([("cookies", ["koncové zařízení"], ["a", "b", "c", "d"], True)])

    async def _fake_build(*, force=False):
        return lex

    tn._reset_cache()  # A1: drop the materialised cache so build_materialized re-reads the patched lexicon
    monkeypatch.setattr(tn, "build_lexicon", _fake_build)
    boost, ids = await tn.term_norm_augmentation("cookies")
    assert len(ids) == 2


async def test_augmentation_degrades_on_error(monkeypatch):
    # any failure inside build/match degrades to ([], []) — the leg is purely additive, never fatal.
    async def _boom(*, force=False):
        raise RuntimeError("db down")

    tn._reset_cache()  # A1: clear the materialised cache so the injected build failure is actually reached
    monkeypatch.setattr(tn, "build_lexicon", _boom)
    assert await tn.term_norm_augmentation("cookies") == ([], [])


# ── OFF byte-identical: hybrid_seed adds no term leg/seed when term_norm_enabled is OFF. ──────────────────
async def test_hybrid_seed_off_adds_no_term_leg(monkeypatch):
    monkeypatch.delenv("PYTHIA_TERM_NORM_ENABLED", raising=False)
    calls = {"sparse": 0, "augment": 0}

    async def _fake_sparse(query, **kw):
        calls["sparse"] += 1
        return [seedmod.Seed("p1", 0.9), seedmod.Seed("p2", 0.5)]

    async def _fake_dense(query, **kw):
        return [seedmod.Seed("p2", 0.8)]

    async def _fake_augment(query):
        calls["augment"] += 1
        return ["x"], ["y"]

    monkeypatch.setattr(seedmod, "sparse_seed", _fake_sparse)
    monkeypatch.setattr(seedmod, "dense_seed", _fake_dense)
    monkeypatch.setattr("pythia.graph.term_norm.term_norm_augmentation", _fake_augment)

    out = await seedmod.hybrid_seed("q", k=4)
    # OFF ⇒ augment NEVER called, exactly TWO sparse-leg calls (sparse + dense), no term extra leg.
    assert calls["augment"] == 0
    assert calls["sparse"] == 1  # only the primary sparse leg (dense is separate)
    ids = {s.provision_id for s in out}
    assert ids == {"p1", "p2"}


async def test_hybrid_seed_on_adds_term_leg_and_seeds(monkeypatch):
    monkeypatch.setenv("PYTHIA_TERM_NORM_ENABLED", "1")
    seen_queries = []

    async def _fake_sparse(query, **kw):
        seen_queries.append(query)
        if "koncove" in tn._fold(query):  # the term-boost leg surfaces the defining provision
            return [seedmod.Seed("cookie-def", 0.7)]
        return [seedmod.Seed("p1", 0.9)]

    async def _fake_dense(query, **kw):
        return []

    async def _fake_augment(query):
        return [tn._fold("koncové zařízení")], ["seed-def"]

    monkeypatch.setattr(seedmod, "sparse_seed", _fake_sparse)
    monkeypatch.setattr(seedmod, "dense_seed", _fake_dense)
    monkeypatch.setattr("pythia.graph.term_norm.term_norm_augmentation", _fake_augment)

    out = await seedmod.hybrid_seed("cookies", k=8)
    ids = {s.provision_id for s in out}
    # the term-boost sparse leg ran (a query containing 'koncove' was issued) AND its hit + the injected
    # defining-provision seed both reached the candidate set.
    assert any("koncove" in tn._fold(q) for q in seen_queries)
    assert "cookie-def" in ids
    assert "seed-def" in ids
    assert "p1" in ids  # the original leg's hit is never dropped


# ═════════════════════════════════════════════════════════════════════════════════════════════════════════
# 2. SOFT-LAW k-budget cap.
# ═════════════════════════════════════════════════════════════════════════════════════════════════════════
def _bind(soft_ids, binding_ids):
    b = {i: SOFT_LAW for i in soft_ids}
    b.update({i: "BINDING" for i in binding_ids})
    return b


def test_default_max_soft_is_ceil_k_over_3():
    assert default_max_soft(9) == 3
    assert default_max_soft(8) == ceil(8 / 3) == 3
    assert default_max_soft(3) == 1
    assert default_max_soft(1) == 1
    assert default_max_soft(0) == 0


def test_softlaw_cap_caps_share_at_ceil_k_over_3():
    # 6 soft-law hits lead the ranking, then ENOUGH binding to fill the rest. k=6 ⇒ max_soft=2. The top-6
    # must contain at most 2 soft-law; the excess soft-law is demoted below the binding hits.
    ranked = ["s0", "s1", "s2", "s3", "s4", "s5", "b0", "b1", "b2", "b3", "b4", "b5"]
    b = _bind(["s0", "s1", "s2", "s3", "s4", "s5"], ["b0", "b1", "b2", "b3", "b4", "b5"])
    out = apply_softlaw_quota(ranked, b, k=6)
    top = out[:6]
    n_soft = sum(1 for i in top if b[i] == SOFT_LAW)
    assert n_soft <= ceil(6 / 3)  # == 2 — soft-law share capped
    # the two highest-ranked soft-law hits are kept; the binding hits fill the rest of the visible window.
    assert top[:2] == ["s0", "s1"]
    assert all(b[i] == "BINDING" for i in top[2:])
    # permutation — nothing dropped.
    assert sorted(out) == sorted(ranked)


def test_softlaw_cap_backfills_when_binding_scarce():
    # DEMOTE-ONLY, never leaves a slot empty: when binding hits can't fill the top-k, demoted soft-law
    # backfills the remaining slots (better than an empty result) — the binding hit is still surfaced.
    ranked = ["s0", "s1", "s2", "s3", "s4", "s5", "b0"]
    b = _bind(["s0", "s1", "s2", "s3", "s4", "s5"], ["b0"])
    out = apply_softlaw_quota(ranked, b, k=6)
    assert "b0" in out[:6]  # the lone binding hit is surfaced within the window
    assert sorted(out) == sorted(ranked)  # nothing dropped


def test_softlaw_cap_never_drops_a_binding():
    ranked = ["s0", "s1", "s2", "b0", "s3", "s4"]
    b = _bind(["s0", "s1", "s2", "s3", "s4"], ["b0"])
    out = apply_softlaw_quota(ranked, b, k=3)
    assert "b0" in out[:3]  # the lone binding hit is surfaced
    assert set(out) == set(ranked)  # nothing removed


def test_softlaw_cap_off_is_byte_identical_selection():
    # the OFF path in answer.py is `_ranked[:k_eff]`; apply_softlaw_quota is simply not called. Here we pin
    # that when the number of soft-law hits already respects the budget, the cap is a NO-OP re-order.
    ranked = ["b0", "s0", "b1", "b2"]
    b = _bind(["s0"], ["b0", "b1", "b2"])
    out = apply_softlaw_quota(ranked, b, k=4)
    assert out == ranked  # already within ceil(4/3)=2 soft budget → identity


def test_softlaw_cap_null_bindingness_not_demoted():
    # an un-tagged (NULL bindingness) provision is treated as NON-soft — never demoted as if it were guidance.
    ranked = ["s0", "s1", "n0", "s2"]
    b = {"s0": SOFT_LAW, "s1": SOFT_LAW, "n0": None, "s2": SOFT_LAW}
    out = apply_softlaw_quota(ranked, b, k=3)
    # k=3 ⇒ max_soft=1; s0 kept, s1 demoted, n0 kept (non-soft), then s2. n0 must stay in the top-3.
    assert "n0" in out[:3]


def test_softlaw_cap_demoted_order_preserved():
    ranked = ["s0", "s1", "s2", "s3", "b0"]
    b = _bind(["s0", "s1", "s2", "s3"], ["b0"])
    out = apply_softlaw_quota(ranked, b, k=2)  # max_soft = ceil(2/3)=1
    # top-2: s0 (kept) + b0 (binding). demoted tail keeps s1<s2<s3 relative order.
    assert out[:2] == ["s0", "b0"]
    assert out[2:] == ["s1", "s2", "s3"]


def test_softlaw_cap_idempotent():
    ranked = ["s0", "s1", "s2", "b0", "b1"]
    b = _bind(["s0", "s1", "s2"], ["b0", "b1"])
    once = apply_softlaw_quota(ranked, b, k=4)
    twice = apply_softlaw_quota(once, b, k=4)
    assert once == twice
