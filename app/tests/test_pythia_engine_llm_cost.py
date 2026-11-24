"""AQ-01 — the boundary writes costed `pythia_engine_llm` rows for each engine
`audit_trace.llm_usage` entry, and NOTHING for the default (ollama) empty path.

DB-free: `api_tracker._persist_row`, `pricing.estimate_cost`, and `events.emit`
are monkeypatched so no Postgres / engine / LLM is touched. Proves:
  1. N usage entries ⇒ N extra provider='pythia_engine_llm' rows, correct cost,
     same org_id/analysis_run_id, cached-token subtraction respected.
  2. Empty / missing / malformed llm_usage ⇒ ZERO extra rows (byte-identical
     default path) and the boundary call still returns the result.
  3. The committed default price book now prices gpt-5.5 / gpt-5.4-mini non-zero.
"""

from __future__ import annotations

import pytest

from regsuper.pythia import client as pc


class _Recorder:
    """Captures every _persist_row call as a dict of its kwargs."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def persist(self, **kwargs):
        self.rows.append(kwargs)
        return None


@pytest.fixture
def _patched(monkeypatch):
    rec = _Recorder()
    # api_tracker / pricing / events are imported LAZILY inside _track and
    # _write_engine_llm_rows, so patch the source modules directly.
    from regsuper.observability import api_tracker, events, pricing

    monkeypatch.setattr(api_tracker, "_persist_row", rec.persist, raising=False)

    # Deterministic, cheap cost so the assertion is exact.
    async def _fake_cost(model, *, kind="token", tokens_input=0, tokens_output=0, tokens_cached=0):
        return round(0.001 * tokens_input + 0.002 * tokens_output, 6)

    monkeypatch.setattr(pricing, "estimate_cost", _fake_cost, raising=False)

    async def _noop_emit(*a, **k):
        return None

    monkeypatch.setattr(events, "emit", _noop_emit, raising=False)
    return rec


async def _run_track(result):
    async def _fn():
        return result

    return await pc._track("answer", _fn, org_id="ORG-1", analysis_run_id=42)


@pytest.mark.asyncio
async def test_two_usage_entries_write_two_costed_rows(_patched):
    result = {
        "drafted_answer": "…",
        "audit_trace": {
            "llm_usage": [
                {
                    "stage": "generate",
                    "model": "gpt-5.5",
                    "tokens_input": 1000,
                    "tokens_output": 500,
                    "tokens_cached": 200,
                },
                {
                    "stage": "verify_faithfulness",
                    "model": "gpt-5.4-mini",
                    "tokens_input": 300,
                    "tokens_output": 50,
                    "tokens_cached": 0,
                },
            ]
        },
    }
    out = await _run_track(result)
    assert out is result  # boundary returns the sidecar result unchanged

    rec = _patched
    transport = [r for r in rec.rows if r["provider"] == "pythia"]
    engine = [r for r in rec.rows if r["provider"] == "pythia_engine_llm"]
    assert len(transport) == 1  # the zero-cost transport row STAYS
    assert transport[0]["cost_usd"] == 0.0
    assert len(engine) == 2  # one costed row per usage entry

    r0 = engine[0]
    assert r0["model"] == "gpt-5.5"
    assert r0["tokens_input"] == 1000
    assert r0["tokens_output"] == 500
    assert r0["tokens_cached"] == 200
    assert r0["cost_usd"] == round(0.001 * 1000 + 0.002 * 500, 6)
    assert r0["org_id"] == "ORG-1"
    assert r0["analysis_run_id"] == 42
    # VL-G-02 (WS3 step 28): per-stage source (provider still pythia_engine_llm so rollups are untouched).
    assert r0["source"] == "pythia.answer.generate"
    assert engine[1]["source"] == "pythia.answer.verify_faithfulness"
    assert r0["endpoint"] == "answer"
    # Secret-safety: no prompt/response text persisted.
    assert r0["request_json"] == {}
    assert r0["response_json"] == {}

    assert engine[1]["model"] == "gpt-5.4-mini"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "audit",
    [
        {},  # no llm_usage key
        {"llm_usage": []},  # empty list (default ollama path)
        {"llm_usage": None},  # None
        {"llm_usage": "oops"},  # non-list
    ],
)
async def test_no_extra_rows_on_empty_or_missing_usage(_patched, audit):
    result = {"drafted_answer": "…", "audit_trace": audit}
    out = await _run_track(result)
    assert out is result
    engine = [r for r in _patched.rows if r["provider"] == "pythia_engine_llm"]
    assert engine == []  # byte-identical: NO new rows
    # The transport row still writes.
    assert any(r["provider"] == "pythia" for r in _patched.rows)


@pytest.mark.asyncio
async def test_non_dict_result_writes_no_engine_rows(_patched):
    out = await _run_track(["not", "a", "dict"])
    assert out == ["not", "a", "dict"]
    assert [r for r in _patched.rows if r["provider"] == "pythia_engine_llm"] == []


@pytest.mark.asyncio
async def test_malformed_entries_are_skipped_but_valid_ones_survive(_patched):
    result = {
        "audit_trace": {
            "llm_usage": [
                {"stage": "decompose"},  # missing model → skip
                {"model": ""},  # empty model → skip
                {"model": 123},  # non-str model → skip
                "not-a-dict",  # non-dict → skip
                {
                    "model": "gpt-5.5",
                    "tokens_input": "bad",  # bad ints coerce → 0
                    "tokens_output": None,
                },
            ]
        }
    }
    await _run_track(result)
    engine = [r for r in _patched.rows if r["provider"] == "pythia_engine_llm"]
    assert len(engine) == 1
    assert engine[0]["model"] == "gpt-5.5"
    assert engine[0]["tokens_input"] == 0
    assert engine[0]["tokens_output"] == 0


# --- (b) committed prices now price the gpt-5.x models non-zero -----------------
@pytest.mark.asyncio
async def test_committed_prices_include_gpt_5x():
    from regsuper.observability import pricing

    pricing._invalidate_cache()
    cost_55 = await pricing.estimate_cost(
        "gpt-5.5", kind="token", tokens_input=1_000_000, tokens_output=0
    )
    cost_54 = await pricing.estimate_cost(
        "gpt-5.4-mini", kind="token", tokens_input=1_000_000, tokens_output=0
    )
    assert cost_55 == pytest.approx(5.0)  # [5.0, 30.0, 0.5] input price
    assert cost_54 == pytest.approx(0.5)  # [0.5, 2.0, 0.05] input price
    # Output + cached folding also non-trivial.
    cost_out = await pricing.estimate_cost(
        "gpt-5.5", kind="token", tokens_input=0, tokens_output=1_000_000
    )
    assert cost_out == pytest.approx(30.0)
    pricing._invalidate_cache()
