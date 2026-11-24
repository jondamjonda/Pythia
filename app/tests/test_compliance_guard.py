"""B2b-02 — the fail-closed compliance guard (the ČAK reserved-advice moat).

THE load-bearing invariant: the guard NEVER fails open to 'information'. Any Tier-2 failure (model/client
unavailable, unparseable, low confidence) → needs_review; an un-evaluable refusal fold → refuse=True. Tier-1
is a deterministic CZ+EN regex that needs no model. Ships in shadow (classifies+logs, blocks no one).
"""

import contextlib
from unittest.mock import patch

import pytest

from regsuper.analysis import compliance_guard as G


@contextlib.asynccontextmanager
async def _dummy_session():
    yield object()  # the audit write is best-effort; we stub the session + log_event so no DB is needed


def _no_llm():
    """patch tracked_chat_completion to RAISE — simulates the Tier-2 model/client being unavailable."""

    async def _raise(*_a, **_k):
        raise RuntimeError("cheap model unavailable")

    return patch.object(G, "tracked_chat_completion", _raise)


def _llm_returns(answer_mode: str, confidence: float):
    class _Msg:  # minimal OpenAI-response shape
        def __init__(self, c):
            self.message = type("m", (), {"content": c})

    class _Resp:
        def __init__(self, c):
            self.choices = [_Msg(c)]

    import json

    async def _ok(*_a, **_k):
        return _Resp(
            json.dumps({"answer_mode": answer_mode, "confidence": confidence, "reason": "x"})
        )

    return patch.object(G, "tracked_chat_completion", _ok)


# ───────────────────────────────────────────────────────────────────── Tier-1 (deterministic, no model)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        # G14 determination (2026-06-29): reserved = ACT ON BEHALF (represent / file / sue FOR me) ONLY.
        "Zastupte mě prosím u soudu v mém sporu se sousedem.",
        "Podejte za mě žalobu na bývalého zaměstnavatele.",
        "Jednejte za mě v tom řízení.",
        "Please represent me in court against my landlord.",
        "file a claim for me with the court",
    ],
)
async def test_tier1_reserved_advice_no_model_call(text):
    """A clear reserved-advice request is caught by Tier-1 — reserved_advice, decided by the rule, NO LLM."""
    with patch.object(G, "session_scope", _dummy_session), patch.object(G, "log_event", None):

        async def _boom(*_a, **_k):
            raise AssertionError("Tier-2 must NOT run for a Tier-1 hit")

        with patch.object(G, "tracked_chat_completion", _boom):
            v = await G.classify_answer_mode(text=text, where="input")
    assert v.answer_mode == "reserved_advice" and v.decided_by == "rule:advice-guard-v1"


# ───────────────────────────────────────────────────────────────────── Tier-2 + FAIL-CLOSED (the moat)
@pytest.mark.asyncio
async def test_ambiguous_with_dead_model_fails_closed_to_needs_review():
    """The cardinal test: an ambiguous query + an unavailable Tier-2 model must NOT fall to 'information' —
    it fails CLOSED to needs_review."""
    with (
        patch.object(G, "session_scope", _dummy_session),
        patch.object(G, "log_event", None),
        _no_llm(),
    ):
        v = await G.classify_answer_mode(
            text="Jaké povinnosti má zaměstnavatel podle zákoníku práce?", where="input"
        )
    assert v.answer_mode == "needs_review"
    assert v.answer_mode != "information"


@pytest.mark.asyncio
async def test_ambiguous_low_confidence_fails_closed():
    """A Tier-2 verdict below the 0.6 floor fails closed to needs_review (never a confident-looking pass)."""
    with (
        patch.object(G, "session_scope", _dummy_session),
        patch.object(G, "log_event", None),
        _llm_returns("information", 0.40),
    ):
        v = await G.classify_answer_mode(
            text="something genuinely ambiguous about the law", where="output"
        )
    assert v.answer_mode == "needs_review"


@pytest.mark.asyncio
async def test_ambiguous_confident_llm_verdict_is_honoured():
    """When Tier-2 IS available + confident, its verdict is used (information stays information, reserved
    stays reserved)."""
    with (
        patch.object(G, "session_scope", _dummy_session),
        patch.object(G, "log_event", None),
        _llm_returns("information", 0.92),
    ):
        v = await G.classify_answer_mode(
            text="What are the GDPR record-keeping requirements in general?", where="input"
        )
    assert v.answer_mode == "information" and v.confidence == 0.92
    with (
        patch.object(G, "session_scope", _dummy_session),
        patch.object(G, "log_event", None),
        _llm_returns("reserved_advice", 0.88),
    ):
        v2 = await G.classify_answer_mode(text="ambiguous-but-actually-advice", where="input")
    assert v2.answer_mode == "reserved_advice"


@pytest.mark.asyncio
async def test_empty_input_is_needs_review_never_information():
    with patch.object(G, "session_scope", _dummy_session), patch.object(G, "log_event", None):
        v = await G.classify_answer_mode(text="   ", where="input")
    assert v.answer_mode == "needs_review"


# ───────────────────────────────────────────────────────────────────── project_refusal fold (pure)
def test_project_refusal_folds_and_fails_closed():
    rr_refused = type("R", (), {"refused": True, "flagged": False})()
    rr_clean = type("R", (), {"refused": False, "flagged": False})()
    assert G.project_refusal(rr_refused, []).refuse is True
    assert G.project_refusal(rr_clean, []).refuse is False
    assert (
        G.project_refusal(rr_clean, [{"finding_type": "refusal"}]).refuse is True
    )  # cascade refusal
    assert G.project_refusal(None, 5).refuse is True  # un-evaluable → fail-closed


def test_disclaimer_mapping():
    assert G.disclaimer_for("reserved_advice") == G.DISCLAIMER_RESERVED_ADVICE
    assert G.disclaimer_for("needs_review") == G.DISCLAIMER_INFO_LOW_CONFIDENCE
    assert G.disclaimer_for("information", confidence=0.4) == G.DISCLAIMER_INFO_LOW_CONFIDENCE
    assert G.disclaimer_for("information", confidence=0.9) == G.DISCLAIMER_INFO
