"""B1a — the context-window upgrade + practical-facts extractor proof (additive, gated, byte-identical-OFF).

Pure unit tests (no live LLM, no DB). Proves:

  (a) ctx_window_enabled defaults False.
  (b) Extractor — verbatim substring integrity (text[start:end] == value_raw), span slicing, fact types,
      no-match ⇒ [].
  (c) build_ctx_window — head-only for short text; a deadline at ~char 900 that the 700 cut missed IS inside
      the widened window; a normative TERM-MATCH span beyond 700 (no fact token) IS windowed; the ~1600 cap is
      enforced.
  (d) OFF byte-identity — _fetch sets ctx_txt == txt; _build_ctx(use_ctx_txt=False) == today's exact string;
      the served text_md grain (meta['txt']) is the 700-slice regardless of the flag.
  (e) ON wiring — _build_ctx(use_ctx_txt=True) reads meta['ctx_txt'].
"""

from __future__ import annotations

import pytest

from pythia.config import PythiaSettings
from pythia.reasoning.answer import (
    _CTX_CAP,
    _CTX_HEAD,
    _build_ctx,
    build_ctx_window,
)
from pythia.reasoning.practical_facts import (
    _tokens,
    extract_practical_facts,
    term_match_spans,
)


# ----- (a) flag defaults OFF -------------------------------------------------------------------------------


def test_flag_defaults_false():
    assert PythiaSettings().ctx_window_enabled is False


# ----- (b) extractor ---------------------------------------------------------------------------------------

_SAMPLE = (
    "Poplatník je povinen podat oznámení správci daně nejpozději do 30 dnů ode dne "
    "zahájení činnosti. Za porušení lze uložit pokutu do výše 500 000 Kč. "
    "Oznámení se podává na tiskopisu vydaném úřadem."
)


def test_extractor_spans_are_verbatim_substrings():
    facts = extract_practical_facts(_SAMPLE)
    assert facts
    for f in facts:
        assert _SAMPLE[f.char_start : f.char_end] == f.value_raw  # exact slice
        assert f.char_end > f.char_start
        # the canonical 7 fact types (the T10 wave added channel/fee/procedure_step)
        assert f.fact_type in (
            "deadline",
            "authority",
            "penalty",
            "form",
            "channel",
            "fee",
            "procedure_step",
        )


def test_extractor_finds_each_fact_type():
    types = {f.fact_type for f in extract_practical_facts(_SAMPLE)}
    assert "deadline" in types
    assert "authority" in types  # správci daně
    assert "penalty" in types  # pokutu / 500 000 Kč
    assert "form" in types  # tiskopisu


def test_extractor_deadline_value_captured():
    facts = extract_practical_facts(_SAMPLE)
    deadlines = [f.value_raw for f in facts if f.fact_type == "deadline"]
    assert any("30 dnů" in d for d in deadlines)


@pytest.mark.parametrize("bad", [None, "", "   ", "Toto ustanovení neobsahuje žádný praktický údaj."])
def test_extractor_no_match_returns_empty(bad):
    assert extract_practical_facts(bad) == []


def test_extractor_deterministic_and_sorted():
    a = extract_practical_facts(_SAMPLE)
    b = extract_practical_facts(_SAMPLE)
    assert [f.as_dict() for f in a] == [f.as_dict() for f in b]
    assert a == sorted(a, key=lambda f: (f.char_start, f.char_end))


def test_term_match_spans_diacritic_case_insensitive():
    text = "Provozovatel získá SOUHLAS uživatele."
    spans = term_match_spans(text, ["souhlas uzivatele"])  # no diacritics, lower-case query
    assert spans
    for s, e in spans:
        # the folded query matched; the offsets index the ORIGINAL text
        assert text[s:e].lower().replace("ž", "z") in ("souhlas", "uzivatele")


def test_term_match_ignores_short_terms():
    assert term_match_spans("aa bb cc dané slovo", ["aa", "bb"]) == []  # <3-char terms dropped


# ----- (c) build_ctx_window --------------------------------------------------------------------------------


def test_window_short_text_is_head_only():
    t = "Krátké ustanovení do 5 dnů."
    assert build_ctx_window(t, ["nic"]) == t[:_CTX_HEAD]  # head covers everything, no gap markers


def test_window_captures_deadline_beyond_700():
    # deadline sits at ~char 900 — the 700 cut would have missed it entirely
    t = "A" * 880 + " Oznámení se podává do 15 dnů ode dne. " + "B" * 500
    assert "do 15 dnů" not in t[:700]  # confirm the premise: the 700-slice misses it
    w = build_ctx_window(t, ["nic"])
    assert "do 15 dnů" in w  # …but the widened window catches it
    assert len(w) <= _CTX_CAP


def test_window_captures_term_span_beyond_700_no_fact_token():
    # the cookie §89 pattern: a normative rule beyond char 700 with NO practical-fact token, only query terms
    t = "C" * 900 + " Provozovatel je povinen získat souhlas uživatele s ukládáním cookies. " + "D" * 300
    assert "cookies" not in t[:700]
    assert extract_practical_facts(t[900:]) == []  # no fact token in that region
    w = build_ctx_window(t, ["cookies souhlas uživatele"])
    assert "cookies" in w  # term-match span widened it in
    assert len(w) <= _CTX_CAP


def test_window_cap_enforced():
    # many deadlines spread across a long text ⇒ many windows, but the result must never exceed the cap
    chunk = "E" * 300 + " do 10 dnů "
    t = chunk * 30  # ~9k chars, dozens of deadline hits
    w = build_ctx_window(t, ["nic"])
    assert len(w) <= _CTX_CAP


def test_window_gap_marker_between_disjoint_excerpts():
    t = "A" * 700 + "X" * 400 + " do 15 dnů " + "B" * 100
    w = build_ctx_window(t, ["nic"])
    assert " … " in w  # head and the far deadline are disjoint
    assert "do 15 dnů" in w


@pytest.mark.parametrize("empty", [None, ""])
def test_window_empty_input(empty):
    assert build_ctx_window(empty, ["x"]) == ""


# ----- (d) OFF byte-identity + served-grain scoping --------------------------------------------------------


def _meta(txt: str, ctx_txt: str | None = None) -> dict:
    m = {"ext": "127/2005", "label": "§ 89", "txt": txt}
    m["ctx_txt"] = ctx_txt if ctx_txt is not None else txt
    return m


def test_build_ctx_off_uses_txt_byte_identical():
    meta = {"p1": _meta("První text.", ctx_txt="PRVNÍ WIDENED — should be ignored when OFF")}
    off = _build_ctx(meta, ["p1"], guard=False, use_ctx_txt=False)
    assert off == "[127/2005 § 89] První text."  # today's exact format from meta['txt']


def test_build_ctx_on_uses_ctx_txt():
    meta = {"p1": _meta("První text.", ctx_txt="První text. … do 15 dnů.")}
    on = _build_ctx(meta, ["p1"], guard=False, use_ctx_txt=True)
    assert "do 15 dnů" in on  # ON reads meta['ctx_txt']
    assert on == "[127/2005 § 89] První text. … do 15 dnů."


async def test_fetch_off_ctx_txt_equals_txt(monkeypatch):
    """_fetch on the OFF path sets ctx_txt == txt and text_md-grain (txt) stays the 700-slice."""
    from types import SimpleNamespace

    import pythia.reasoning.answer as ans

    long_text = "A" * 880 + " nejpozději do 30 dnů " + "B" * 1500  # deadline beyond 700, long text

    class _FakeRow(SimpleNamespace):
        pass

    row = _FakeRow(
        id="p1",
        label="§ 1",
        external_id="1/2000",
        source_title="T",
        jurisdiction="CZ",
        text=long_text,
        bindingness="binding",
        is_authoritative=True,
        temporal_status=None,
        legal_hierarchy="STATUTE",
        pit_status=None,
        stale_vector=None,
    )

    class _FakePort:
        async def fetch_provisions(self, ids):
            return [row]

    monkeypatch.setattr(ans, "NexusPort", lambda: _FakePort())

    # OFF: ctx_txt == txt, both the 700-slice
    meta_off = await ans._fetch(["p1"], ctx_window=False, terms=["deadline"])
    assert meta_off["p1"]["txt"] == long_text[:700]
    assert meta_off["p1"]["ctx_txt"] == meta_off["p1"]["txt"]
    assert "_full" not in meta_off["p1"]  # transient key dropped

    # ON: txt (served grain) UNCHANGED = 700-slice; ctx_txt widened to include the deadline
    meta_on = await ans._fetch(["p1"], ctx_window=True, terms=["nejpozději"])
    assert meta_on["p1"]["txt"] == long_text[:700]  # chip/citation grain untouched
    assert "do 30 dnů" not in meta_on["p1"]["txt"]  # was beyond 700
    assert "do 30 dnů" in meta_on["p1"]["ctx_txt"]  # …now inside the widened ctx
    assert len(meta_on["p1"]["ctx_txt"]) <= _CTX_CAP
    assert "_full" not in meta_on["p1"]


# ----- VL-B-06 (WS6 step 51) — stopwords + word boundaries + fact-window priority (beyond-CAP recovery) -----


def test_stopwords_are_dropped_from_term_source():
    toks = _tokens(["podle zákona musí zaměstnavatel oznámit lhůtu"])
    assert "podle" not in toks and "zakona" not in toks and "musi" not in toks  # boilerplate/function words
    assert "zamestnavatel" in toks and "oznamit" in toks and "lhutu" in toks  # content terms survive


def test_word_boundary_not_substring():
    # a term that occurs only as a SUBSTRING of a longer word yields no span (word-boundary, not substring find)
    assert term_match_spans("nezaplacení", ["placení"]) == []
    assert term_match_spans("řádné placení daně", ["placení"])  # a whole word DOES match


def test_beyond_cap_deadline_is_captured_not_lost():
    """The core VL-B-06 win: a deadline BEYOND the 1,600 cap survives because a window anchors around it —
    whereas the old degenerate text[:cap] slice would have dropped it."""
    filler = "Ustanovení tohoto paragrafu upravuje podrobnosti řízení. " * 40  # ~2,240 chars boilerplate
    full = filler + "Přihlášku je nutné podat do 8 dnů ode dne nástupu."
    assert full.index("do 8 dnů") > _CTX_CAP  # the deadline genuinely sits beyond the cap
    ctx = build_ctx_window(full, ["přihláška", "nástup"], head=_CTX_HEAD, cap=_CTX_CAP)
    assert "do 8 dnů" in ctx  # …yet it is windowed in and survives
    assert "do 8 dnů" not in full[:_CTX_CAP]  # the OLD degenerate text[:cap] would lose it


def test_fact_spans_param_is_the_priority_source():
    """When the caller supplies MP-F practical_fact ROW spans, they are the priority window anchors."""
    full = "A" * 900 + " DEADLINE_MARKER " + "B" * 1200
    s = full.index("DEADLINE_MARKER")
    ctx = build_ctx_window(full, None, head=_CTX_HEAD, cap=_CTX_CAP, fact_spans=[(s, s + len("DEADLINE_MARKER"))])
    assert "DEADLINE_MARKER" in ctx


def test_stopword_only_query_does_not_flood_the_window():
    """A query made ENTIRELY of stopwords/boilerplate anchors NO term windows — a long fact-less provision keeps
    just its head rather than collapsing to text[:cap]."""
    body = "Podle zákona podle ustanovení podle paragrafu. " * 50  # ~2,350 chars, all boilerplate
    ctx = build_ctx_window(body, ["podle", "zákona", "ustanovení"], head=_CTX_HEAD, cap=_CTX_CAP)
    assert ctx == body[:_CTX_HEAD]  # only the head — no flooded term windows
