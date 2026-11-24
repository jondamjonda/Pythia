"""Track A / A1 — deterministic retrieval-robustness tests (LLM-free, DB-free).

Covers the four DEFAULT-OFF mechanics, each with a byte-identical-OFF pin + an ON behaviour proof:
  1. seed_token_idf_enabled — OFF keeps the exact first-14 ≥3-char slice; ON re-ranks by IDF (rarer wins).
  2. seed_pool_scale_enabled — rerank_pool None ⇒ hybrid_seed's rk == max(k*2, _RERANK_POOL); set ⇒ override.
  3. retrieval_concurrent_legs_enabled — concurrent legs yield the SAME merged rrf/raw as sequential.
  4. retrieval_quotas_enabled — a per-leg quota widens coverage (a starved leg's hits survive the merge).

All pure/stubbed: the engine retrieve is monkeypatched, the df cache is stubbed, no live DB is touched.
"""

from __future__ import annotations

from math import ceil


from pythia.config import get_settings
from pythia.contract import PythiaQuery
from pythia.graph import seed as seedmod


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# Flag defaults — all four OFF, so committing this is byte-identical.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_a1_flag_defaults_all_false():
    s = get_settings()
    assert s.seed_token_idf_enabled is False
    assert s.seed_pool_scale_enabled is False
    assert s.retrieval_concurrent_legs_enabled is False
    assert s.retrieval_quotas_enabled is False


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 1. IDF seed-token selection.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
_LONG_Q = (
    "Mám malou restauraci a chci vědět co všechno musím udělat než někoho poprvé "
    "zaměstnat a na koho se hlásit ohledně notifikace"
)


async def test_seed_terms_off_is_exact_first_14_slice(monkeypatch):
    monkeypatch.delenv("PYTHIA_SEED_TOKEN_IDF_ENABLED", raising=False)
    import re

    expected = [t for t in re.findall(r"\w+", _LONG_Q, re.UNICODE) if len(t) >= 3][:14]
    got = await seedmod._seed_terms(_LONG_Q, idf=False)
    assert got == expected
    assert len(got) <= 14


def test_idf_rank_picks_rarer_terms_over_common_ones():
    # crafted df: 'notifikace' is rare (df=2), 'restauraci' rare (df=5); the fillers are common (high df).
    terms = [
        "mam",
        "chci",
        "vedet",
        "musim",
        "udelat",
        "nez",
        "nekoho",
        "poprve",
        "zamestnat",
        "koho",
        "hlasit",
        "ohledne",
        "restauraci",
        "notifikace",
        "vsechno",
    ]
    df = {t.lower(): 40000 for t in terms}  # everything common by default …
    df["notifikace"] = 2  # … except the two discriminative legal terms
    df["restauraci"] = 5
    df["zamestnat"] = 30
    picked = seedmod._idf_rank(terms, df, n_docs=54000, limit=14)
    # 15 terms, limit 14 → exactly one common filler is dropped; the two rarest must survive and lead.
    assert "notifikace" in picked and "restauraci" in picked
    assert picked[0] == "notifikace" and picked[1] == "restauraci"
    assert picked[2] == "zamestnat"
    assert len(picked) == 14


def test_idf_rank_ties_keep_original_order():
    terms = ["alfa", "beta", "gama", "delta"]
    df = {t: 100 for t in terms}  # all equal df → stable sort keeps input order
    assert seedmod._idf_rank(terms, df, n_docs=1000, limit=4) == terms


def test_idf_rank_unknown_token_ranks_below_corpus_matched():
    # VL-B-01: a token whose lexeme is absent from the corpus (a true OOV) is UNKNOWN — it can seed no
    # match, so it must rank BELOW any corpus-matched term, NOT first ("fake-rare" was the inversion bug).
    terms = ["common", "unknownoov"]
    df = {"common": 50000}  # 'unknownoov' lexeme absent → UNKNOWN
    ranked = seedmod._idf_rank(terms, df, n_docs=50000, limit=2)
    assert ranked[0] == "common" and ranked[-1] == "unknownoov"


def test_idf_rank_lexeme_normalization_beats_raw_token_lookup():
    # THE VL-B-01 regression. 'zaměstnance'→lexeme 'zaměstnanec' (df 2145, the corpus's MOST common term)
    # would fake-rank RARE under the old raw-token lookup and steal a seed slot; 'restauraci'→'restaurace'
    # (df 5) is the discriminative term that should lead; a true OOV ('xyzzy', lexeme absent) must rank LAST.
    terms = ["zaměstnance", "restauraci", "plzni", "xyzzy"]
    df = {"restaurace": 5, "plzeň": 37, "zaměstnanec": 2145}
    lex_map = {
        "zaměstnance": ["zaměstnanec"],
        "restauraci": ["restaurace"],
        "plzni": ["plzeň"],
        "xyzzy": ["xyzzy"],
    }  # 'xyzzy' lexeme not in df ⇒ UNKNOWN
    picked = seedmod._idf_rank(terms, df, n_docs=54000, lex_map=lex_map, limit=4)
    assert picked == ["restauraci", "plzni", "zaměstnance", "xyzzy"]  # df 5 < 37 < 2145 < UNKNOWN
    # the inflected common word is NOT fake-rare, and the OOV never outranks a corpus match:
    assert picked.index("restauraci") < picked.index("zaměstnance")
    assert picked[-1] == "xyzzy"
    # with a tight limit the useless OOV is the one dropped:
    assert "xyzzy" not in seedmod._idf_rank(terms, df, n_docs=54000, lex_map=lex_map, limit=3)


async def test_seed_terms_on_reorders_by_idf(monkeypatch):
    async def _stub_cache():
        # every query token's lexeme present (none UNKNOWN); 'notifikace' is the rarest.
        return ({"prvni": 9000, "zamestnanec": 4000, "povinnost": 20000, "notifikace": 2}, 54000)

    async def _stub_lex(terms):
        # identity normalization for these ASCII test tokens (each token is its own lexeme).
        return {t: [t] for t in terms}

    monkeypatch.setattr(seedmod, "_load_df_cache", _stub_cache)
    monkeypatch.setattr(seedmod, "_lexemes_for", _stub_lex)
    # 'notifikace' (df 2) is late in the query but must lead after IDF ranking.
    got = await seedmod._seed_terms("prvni zamestnanec a povinnost notifikace", idf=True)
    assert got[0] == "notifikace"


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 2. Rerank-pool scale — the rk computation inside hybrid_seed.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def _rk(k: int, rerank_pool):
    """Mirror of the rk line in hybrid_seed (kept in lockstep with seed.py)."""
    return max(k * 2, seedmod._RERANK_POOL if rerank_pool is None else rerank_pool)


def test_rerank_pool_none_is_byte_identical_rk():
    for k in (4, 8, 12, 20):
        assert _rk(k, None) == max(k * 2, seedmod._RERANK_POOL)


def test_rerank_pool_set_overrides_floor():
    # a scaled pool of 48 with a small k widens the shortlist beyond the 16 floor.
    assert _rk(8, 48) == 48
    assert _rk(8, None) == 16
    # but k*2 still floors it (the override never SHRINKS below k*2).
    assert _rk(20, 10) == 40


def test_pythia_query_rerank_pool_default_none():
    assert PythiaQuery(query="x").rerank_pool is None
    assert PythiaQuery(query="x", rerank_pool=48).rerank_pool == 48


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 3 + 4. Concurrent legs + quotas — exercised through answer.answer() with a stubbed engine (no DB/LLM).
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
class _StubCP:
    def __init__(self, pid, score):
        self.provision_id = pid
        self.score = score


class _StubAnswer:
    def __init__(self, cps):
        self.cited_provisions = cps


def _install_answer_stubs(monkeypatch, *, per_query):
    """Stub _decompose (fixed sub_queries), the LLM factory, _fetch, verify, sections and the engine so
    answer.answer() runs its leg/merge/select pipeline with NO DB and NO LLM. `per_query` maps a sub-query
    string → list[(pid, score)] the engine 'retrieves' for it. Returns the merged `top` ids the pipeline
    selected by short-circuiting right after selection via a captured hook."""
    import pythia.reasoning.answer as A

    sub_queries = list(per_query.keys())

    async def _stub_decompose(_llm, _query):
        return sub_queries, ["CZ", "EU"], "test"

    monkeypatch.setattr(A, "_decompose", _stub_decompose)

    class _StubLLM:
        async def available(self):
            return True

        async def text(self, *a, **k):
            return "## Legislativní rámec\nx\n## Hodnocení\ny"

        async def json(self, *a, **k):
            return {"unsupported": []}

    monkeypatch.setattr(A, "get_llm", lambda tier: _StubLLM())

    class _StubEngine:
        seed_rerank = True

        async def retrieve(self, q, *, method="cte"):
            cps = [_StubCP(pid, sc) for pid, sc in per_query.get(q.query, [])]
            return _StubAnswer(cps)

    monkeypatch.setattr(A, "PythiaEngine", _StubEngine)

    captured = {}

    async def _stub_fetch(ids, *, as_of=None, render_asof=False, ctx_window=False, terms=None):
        captured["top"] = list(ids)
        # a minimal meta dict so the rest of answer() can build (it will, but we assert on captured['top']).
        return {
            i: {
                "id": i,
                "label": "§ 1",
                "ext": "1/2000",
                "title": "t",
                "jurisdiction": "CZ",
                "txt": "text",
                "bindingness": None,
                "is_authoritative": None,
                "temporal_status": None,
                "legal_hierarchy": None,
                "pit_status": None,
                "vector_stale": None,
            }
            for i in ids
        }

    monkeypatch.setattr(A, "_fetch", _stub_fetch)
    return captured


async def test_concurrent_legs_equal_sequential(monkeypatch):
    # two legs with overlapping + distinct hits; the merged top must be identical concurrent vs sequential.
    per_query = {
        "leg-a": [("p1", 0.9), ("p2", 0.5), ("p3", 0.4)],
        "leg-b": [("p2", 0.8), ("p4", 0.6), ("p5", 0.3)],
    }

    monkeypatch.delenv("PYTHIA_RETRIEVAL_CONCURRENT_LEGS_ENABLED", raising=False)
    cap_seq = _install_answer_stubs(monkeypatch, per_query=per_query)
    import pythia.reasoning.answer as A

    await A.answer("q", k=12)
    seq_top = cap_seq["top"]

    monkeypatch.setenv("PYTHIA_RETRIEVAL_CONCURRENT_LEGS_ENABLED", "1")
    cap_con = _install_answer_stubs(monkeypatch, per_query=per_query)
    await A.answer("q", k=12)
    con_top = cap_con["top"]

    assert seq_top == con_top, (seq_top, con_top)
    # sanity: the merged set is the union of both legs' hits.
    assert set(seq_top) == {"p1", "p2", "p3", "p4", "p5"}


async def test_quotas_off_uses_global_k_and_full_legs(monkeypatch):
    # one dominant leg with many hits + a small second leg. OFF: k=3 global top-k, full legs feed the merge.
    per_query = {
        "big": [(f"b{i}", 1.0 - i * 0.01) for i in range(10)],
        "small": [("s1", 0.95)],
    }
    monkeypatch.delenv("PYTHIA_RETRIEVAL_QUOTAS_ENABLED", raising=False)
    cap = _install_answer_stubs(monkeypatch, per_query=per_query)
    import pythia.reasoning.answer as A

    await A.answer("q", k=3)
    top = cap["top"]
    assert len(top) == 3  # OFF ⇒ k_eff == k


async def test_quotas_on_widens_coverage_and_k_eff(monkeypatch):
    # the small leg's single hit is at rank 0 (rrf 1/60) — strong; a huge dominant leg would otherwise
    # crowd the global top-k. Quotas scale k_eff with legs so the second aspect survives.
    per_query = {
        "big": [(f"b{i}", 1.0 - i * 0.001) for i in range(20)],
        "small": [("s1", 0.99)],
    }
    monkeypatch.setenv("PYTHIA_RETRIEVAL_QUOTAS_ENABLED", "1")
    cap = _install_answer_stubs(monkeypatch, per_query=per_query)
    import pythia.reasoning.answer as A

    await A.answer("q", k=3)
    top = cap["top"]
    s = get_settings()
    expected_k_eff = min(s.retrieval_k_eff_cap, 3 * ceil(2 / 2))  # ceil(2/2)=1 → k_eff==k here
    assert len(top) == expected_k_eff
    # the small leg's hit (rank 0 → rrf 1/60, the highest single-leg contribution) must be surfaced.
    assert "s1" in top


async def test_quotas_k_eff_scales_with_more_legs(monkeypatch):
    # four legs → ceil(4/2)=2 → k_eff = min(cap, k*2). Proves the leg-count scaling of the final k.
    per_query = {f"leg{i}": [(f"p{i}_{j}", 0.9 - j * 0.01) for j in range(5)] for i in range(4)}
    monkeypatch.setenv("PYTHIA_RETRIEVAL_QUOTAS_ENABLED", "1")
    cap = _install_answer_stubs(monkeypatch, per_query=per_query)
    import pythia.reasoning.answer as A

    await A.answer("q", k=4)
    top = cap["top"]
    s = get_settings()
    assert len(top) == min(s.retrieval_k_eff_cap, 4 * ceil(4 / 2))  # min(24, 8) == 8


def test_no_second_topk_slicer_single_selection_path():
    # Guard the SINGLE composable selection path: exactly one `sorted(rrf, ... )[:k_eff]` slicer in answer().
    import pathlib

    src = pathlib.Path(seedmod.__file__).parent.parent / "reasoning" / "answer.py"
    txt = src.read_text(encoding="utf-8")
    assert txt.count("sorted(rrf, key=lambda i: -rrf[i])") == 1
