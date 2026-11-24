"""VL-G-01 (WS3 step 25) — repair-spend sink + unified stage vocabulary.

Pins: an LLM call issued from the redraft/entailment WORKER THREAD records its usage into the current
request's sink (the contextvar propagates via copy_context — otherwise the spend was silently lost); the
/metrics by_stage whitelist now surfaces the claim-verify/repair lanes; the stage constants are the one source.
"""

from __future__ import annotations

from pythia import telemetry
from pythia.reasoning import loop_stages
from pythia.reasoning.answer import (
    _make_sync_entailment_call,
    _make_sync_redraft_batch_call,
    _make_sync_redraft_call,
)
from pythia.reasoning.loop_budget import LoopBudget


class _LLM:
    """A fake cheap LLM whose .text records usage exactly as the real adapter does."""

    async def text(self, prompt, stage=None):
        telemetry.record_llm_usage(stage, "cheap-model", 10, 5)
        return "kvótový úryvek"


def test_entailment_spend_captured_through_worker_thread():
    sink = telemetry.new_usage_sink()  # installs a fresh usage_var sink on THIS context
    call = _make_sync_entailment_call(_LLM(), LoopBudget(cap=2))
    call("TVRZENÍ: x\nUSTANOVENÍ: y", [{"claim": "x", "provision_ids": ["P1"]}])
    # the worker-thread call's usage reached this request's sink ⇒ the contextvar propagated (VL-G-01a)
    stages = [u["stage"] for u in sink]
    assert loop_stages.STAGE_CLAIM_ENTAILMENT in stages


def test_redraft_spend_captured_through_worker_thread():
    sink = telemetry.new_usage_sink()
    call = _make_sync_redraft_call(_LLM())
    out = call("uprav toto tvrzení")
    assert out == "kvótový úryvek"
    assert loop_stages.STAGE_REPAIR in [u["stage"] for u in sink]


def test_batched_redraft_spend_captured_through_worker_thread():
    """VL-E-03 (WS5 step 45): the BATCHED redraft bridge captures spend through the worker thread too (same
    STAGE_REPAIR stage, same usage_var propagation) and splits the reply into one redraft per item."""
    sink = telemetry.new_usage_sink()
    call = _make_sync_redraft_batch_call(_LLM())
    items = [object(), object()]  # two placeholder items ⇒ two output slots
    out = call("[1]\n...\n[2]\n...", items)
    assert isinstance(out, list) and len(out) == 2
    assert loop_stages.STAGE_REPAIR in [u["stage"] for u in sink]


def test_by_stage_whitelist_surfaces_claim_lanes():
    telemetry.record_llm_usage(loop_stages.STAGE_REPAIR, "m", 5, 3)  # ≥1 call ⇒ must appear
    telemetry.record_llm_usage(loop_stages.STAGE_CLAIM_ENTAILMENT, "m", 5, 3)
    by_stage = telemetry.metrics()["llm_usage"]["by_stage"]
    assert loop_stages.STAGE_REPAIR in by_stage
    assert loop_stages.STAGE_CLAIM_ENTAILMENT in by_stage


def test_stage_constants_keep_legacy_values():
    # back-compat: the emitted strings are unchanged, so the OFF stage-name set is byte-identical.
    assert loop_stages.STAGE_INTAKE == "decompose"
    assert loop_stages.STAGE_STRICT_RETRY == "generate_retry"
    assert loop_stages.STAGE_REPAIR == "claim_repair_redraft"
    assert loop_stages.CANONICAL["decompose"] == "intake"
