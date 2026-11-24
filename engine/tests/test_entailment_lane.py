"""VL-D-05 (WS3 step 22) — the ambiguous-band entailment lane callable.

Pins: the bridge reserves a LoopBudget slot BEFORE the model call (C9); it fences the provision/claim as
«UNTRUSTED»; it labels its spend stage 'claim_entailment'; budget exhaustion RAISES so V-CIT degrades to its
advisory lane (never a fabricated FAIL); and, fed into V-CIT's _run_entailment, a verbatim quote yields a
'pass' entailment verdict. OFF (no callable in evidence) ⇒ V-CIT never calls a model.
"""

from __future__ import annotations

import pytest

from pythia.reasoning import v_cit
from pythia.reasoning.answer import _make_sync_entailment_call
from pythia.reasoning.claim_ledger import Claim
from pythia.reasoning.loop_budget import LoopBudget


class _LLM:
    """Records the prompts + stages it was asked to complete; returns a fixed quote."""

    def __init__(self, quote="do 15 dnů"):
        self.quote = quote
        self.calls: list[tuple[str, str]] = []

    async def text(self, prompt, stage=None):
        self.calls.append((prompt, stage))
        return self.quote


def test_bridge_reserves_budget_fences_and_labels_stage():
    llm = _LLM()
    budget = LoopBudget(cap=3)
    call = _make_sync_entailment_call(llm, budget)
    out = call("TVRZENÍ: x\nUSTANOVENÍ: y", [{"claim": "x", "provision_ids": ["P1"]}])
    assert out == ["do 15 dnů"]
    assert budget.used == 1  # took ONE slot before the call
    prompt, stage = llm.calls[0]
    assert stage == "claim_entailment"  # spend labeled
    assert "«UNTRUSTED»" in prompt and "«/UNTRUSTED»" in prompt  # fenced
    assert "TVRZENÍ: x" in prompt  # the v_cit-formatted body is carried through


def test_budget_exhaustion_raises_for_advisory_fallback():
    llm = _LLM()
    budget = LoopBudget(cap=0)
    call = _make_sync_entailment_call(llm, budget)
    with pytest.raises(RuntimeError):
        call("prompt", [{"claim": "x", "provision_ids": ["P1"]}])
    assert llm.calls == []  # the model was NEVER called
    assert budget.exceeded is True


def test_empty_model_answer_becomes_empty_quote():
    llm = _LLM(quote="")
    call = _make_sync_entailment_call(llm, LoopBudget(cap=1))
    assert call("p", [{"claim": "x", "provision_ids": ["P1"]}]) == [""]


def test_v_cit_entailment_lane_pass_via_the_bridge():
    # Feed the bridge callable into V-CIT's ambiguous-band lane. The model returns a quote that IS verbatim in
    # the provision → V-CIT counts it 'supported' → an entailment PASS verdict (reason entailment_supported).
    llm = _LLM(quote="oznámit do 15 dnů")
    evidence = {"entailment_llm": _make_sync_entailment_call(llm, LoopBudget(cap=2))}
    claim = Claim(
        id="c0",
        span=(0, 20),
        text="Podat do 15 dnů.",
        slot="lhuty",
        type="deadline",
        type_source="regex",
        chips=[{"provision_id": "P1"}],
        normative=True,
    )
    v = v_cit._run_entailment(claim, ["Ustanovení říká: oznámit do 15 dnů od rozhodnutí."], evidence)
    assert v is not None and v.status == "pass" and v.reason == "entailment_supported"


def test_v_cit_entailment_absent_callable_is_none_no_model():
    # OFF-equivalent: no entailment_llm in evidence ⇒ _run_entailment returns None (advisory fallback), no call.
    claim = Claim(
        id="c0",
        span=(0, 10),
        text="Podat do 15 dnů.",
        slot="lhuty",
        type="deadline",
        type_source="regex",
        chips=[{"provision_id": "P1"}],
        normative=True,
    )
    assert v_cit._run_entailment(claim, ["cokoliv"], {}) is None
