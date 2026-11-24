"""B3 Pillar P1 — practical_fact substrate + fact_lookup + gap_kind (additive, byte-identical OFF).

Pure/mock unit tests (no live DB, no live LLM). Proves:
  (A) gap_kind — the canonical enum is stable, str-valued, and is_gap_kind guards.
  (B) enrich_practical gate — gate_row REJECTS a non-substring / malformed candidate, ACCEPTS an exact slice;
      the regex wave produces verbatim spans on a fixture; the --llm wave is drop-don't-repair (a model span
      that echoes the wrong string is never trusted — value_raw is always OUR slice, malformed spans dropped).
  (C) fact_lookup (C12) — rows FIRST when the table has rows; live-regex FALLBACK when it has none; the
      fact_type filter is honoured; empty/absent provision ⇒ [].

All async DB access is mocked with a tiny fake pool, so nothing touches Postgres or Ollama.
"""

from __future__ import annotations

import pytest

from pythia.ingest import enrich_practical as ep
from pythia.reasoning import fact_lookup as fl
from pythia.reasoning.gap_kind import GAP_KINDS, GapKind, is_gap_kind


# ===== (A) gap_kind ========================================================================================


def test_gap_kind_is_str_enum_and_wire_stable():
    assert GapKind.NO_FACT_ROW == "no_fact_row"
    assert GapKind.DECREE_ABSENT.value == "decree_absent"
    # the canonical set contains the contract-named members
    for name in (
        "no_fact_row",
        "no_text_support",
        "decree_absent",
        "authority_registry_absent",
        "soft_law_absent",
        "case_law_absent",
    ):
        assert name in GAP_KINDS


def test_gap_kinds_unique_and_guarded():
    assert len(GAP_KINDS) == len(set(GAP_KINDS))
    assert is_gap_kind("no_fact_row") is True
    assert is_gap_kind("totally_made_up") is False
    # str-mixin ⇒ json-serialises as the bare string
    import json

    assert json.dumps(GapKind.CASE_LAW_ABSENT) == '"case_law_absent"'


# ===== (B) enrich_practical — the insert-time gate + waves ==================================================

_SAMPLE = (
    "Poplatník je povinen podat oznámení správci daně nejpozději do 30 dnů ode dne "
    "zahájení činnosti. Za porušení lze uložit pokutu do výše 500 000 Kč. "
    "Oznámení se podává na tiskopisu vydaném úřadem."
)


def _cand(text_slice, start, end, method="regex", ver="v", ftype="deadline", value=None):
    return ep._Candidate(
        provision_id="00000000-0000-0000-0000-000000000001",
        fact_type=ftype,
        value_raw=(value if value is not None else text_slice),
        char_start=start,
        char_end=end,
        value_norm=None,
        extraction_method=method,
        extractor_version=ver,
    )


def test_gate_accepts_exact_slice_rejects_non_substring():
    text = "abcdefghij"
    ok = _cand("cde", 2, 5)  # text[2:5] == "cde"
    assert ep.gate_row(text, ok) is True
    bad_value = _cand(None, 2, 5, value="XYZ")  # span ok but value_raw != slice
    assert ep.gate_row(text, bad_value) is False
    bad_span_neg = _cand("a", -1, 1)  # negative start
    assert ep.gate_row(text, bad_span_neg) is False
    bad_span_oob = _cand("j", 9, 99)  # end past text length
    assert ep.gate_row(text, bad_span_oob) is False
    bad_span_empty = _cand("", 3, 3)  # zero-width
    assert ep.gate_row(text, bad_span_empty) is False


def test_regex_wave_produces_verbatim_spans():
    cands = ep.regex_candidates("pid", _SAMPLE)
    assert cands, "expected the fixture to yield regex facts"
    for c in cands:
        # every candidate slices back to its value_raw AND passes the gate
        assert _SAMPLE[c.char_start : c.char_end] == c.value_raw
        assert ep.gate_row(_SAMPLE, c) is True
        assert c.extraction_method == "regex"
        assert c.extractor_version == ep.REGEX_VERSION
    ftypes = {c.fact_type for c in cands}
    assert {"deadline", "authority", "penalty", "form"} & ftypes


@pytest.mark.asyncio
async def test_llm_wave_is_drop_dont_repair():
    text = "Oznámení do 30 dnů."  # a deadline lives at chars 9..17 → "do 30 dnů"
    good_span = (text.index("do 30 dnů"), text.index("do 30 dnů") + len("do 30 dnů"))

    class _FakeLLM:
        def __init__(self, payload):
            self._payload = payload

        async def json(self, prompt, schema, **kw):
            return self._payload

    # (1) a VALID span → one accepted candidate whose value_raw is OUR slice (never a model echo)
    llm_ok = _FakeLLM({"deadlines": [{"char_start": good_span[0], "char_end": good_span[1]}]})
    cands = await ep.llm_candidates(llm_ok, "pid", text)
    assert len(cands) == 1
    c = cands[0]
    assert c.value_raw == text[c.char_start : c.char_end] == "do 30 dnů"
    assert ep.gate_row(text, c) is True
    assert c.extraction_method == "llm"

    # (2) a MALFORMED span (end past text) → dropped, not repaired
    llm_bad = _FakeLLM({"deadlines": [{"char_start": 0, "char_end": 9999}]})
    assert await ep.llm_candidates(llm_bad, "pid", text) == []

    # (3) LLM raises → [] (provision simply skipped)
    class _Boom:
        async def json(self, *a, **k):
            raise RuntimeError("model down")

    assert await ep.llm_candidates(_Boom(), "pid", text) == []


# ===== (C) fact_lookup (contract C12) — rows-first then live-regex fallback ================================


class _FakeConn:
    def __init__(self, rows, text):
        self._rows = rows
        self._text = text

    async def fetch(self, sql, *args):
        # the C12 rows query: filter by optional fact_type ($2)
        ftype = args[1]
        rows = self._rows
        if ftype is not None:
            rows = [r for r in rows if r["fact_type"] == ftype]
        return rows

    async def fetchval(self, sql, *args):
        return self._text


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acq()


def _wire(monkeypatch, rows, text):
    pool = _FakePool(_FakeConn(rows, text))

    async def _get_pool():
        return pool

    monkeypatch.setattr(fl, "get_pool", _get_pool)

    # The live-regex FALLBACK now reads the provision text THROUGH NexusPort.fetch_provisions (not raw SQL —
    # the port-boundary discipline), so mock the port to serve the same `text`.
    class _Prov:
        def __init__(self, t):
            self.text = t

    class _FakePort:
        async def fetch_provisions(self, ids):
            return [_Prov(text)] if text is not None else []

    monkeypatch.setattr(fl, "NexusPort", lambda: _FakePort())


@pytest.mark.asyncio
async def test_lookup_returns_rows_first(monkeypatch):
    rows = [
        {
            "fact_type": "deadline",
            "value_raw": "do 30 dnů",
            "char_start": 5,
            "char_end": 14,
            "value_norm": '{"days": 30}',
            "extraction_method": "regex",
        },
    ]
    _wire(monkeypatch, rows, text="irrelevant when rows exist")
    out = await fl.lookup("pid")
    assert len(out) == 1
    assert out[0].source == "table"
    assert out[0].value_raw == "do 30 dnů"
    assert out[0].value_norm == {"days": 30}  # jsonb-as-str coerced to dict


@pytest.mark.asyncio
async def test_lookup_falls_back_to_live_regex_when_no_rows(monkeypatch):
    _wire(monkeypatch, rows=[], text=_SAMPLE)
    out = await fl.lookup("pid")
    assert out, "fallback should mine facts from the raw text"
    assert all(f.source == "regex" for f in out)
    for f in out:
        assert _SAMPLE[f.char_start : f.char_end] == f.value_raw  # verbatim in the fallback too


@pytest.mark.asyncio
async def test_lookup_fallback_honours_fact_type_filter(monkeypatch):
    _wire(monkeypatch, rows=[], text=_SAMPLE)
    out = await fl.lookup("pid", fact_type="penalty")
    assert out
    assert all(f.fact_type == "penalty" for f in out)


@pytest.mark.asyncio
async def test_lookup_empty_when_no_rows_and_no_text(monkeypatch):
    _wire(monkeypatch, rows=[], text=None)
    assert await fl.lookup("pid") == []
