"""Mid-cascade kill-switch / spend-cap ABORT (post-MVP audit finding).

Trust-critical: when the kill-switch is engaged (or a weekly spend cap hard-blocks) PART-WAY through an
L2/L3 run, the cascade MUST stop — not catch the signal per-provision and keep burning the budget on
every remaining provision.

The regression these tests pin:
  * `evaluate_provision` / `extract_obligations` USED to wrap their LLM call in a broad
    `except Exception` that turned KillSwitchEngaged / SpendLimitExceeded into a per-item `error` string;
    the orchestration loop then recorded a sample error and CONTINUED to the next provision.
  * The fix re-raises those two exception types specifically (a hard stop), so they propagate out of the
    per-item coroutine → asyncio.gather → run_l2/run_l3, aborting the whole run.

Fully mocked — no DB, no network, no OpenAI. We patch the orchestration's DB/LLM seams and replace the
per-provision LLM step with a fake that raises the abort signal on a chosen call index.

asyncio_mode=auto (pyproject) → async test functions run directly.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from regsuper.analysis import l2_applicability as l2
from regsuper.analysis import l3_obligations as l3
from regsuper.analysis.l1_candidates import L1Candidate
from regsuper.observability.killswitch import KillSwitchEngaged
from regsuper.observability.limits import SpendLimitExceeded


# --------------------------------------------------------------------------- shared fakes
def _candidates(n: int) -> list[L1Candidate]:
    return [
        L1Candidate(
            provision_id=uuid4(),
            label=f"§{i}",
            legal_hierarchy="zakon",
            jurisdiction="CZ",
            source_external_id=f"ext-{i}",
            source_title="Act",
            priority_weight=1,
        )
        for i in range(n)
    ]


@asynccontextmanager
async def _fake_session_scope():
    # The orchestrators open one session for the up-front doc_lookup; that query result is unused once
    # we abort, so a session whose execute() returns no rows is enough.
    class _Result:
        def all(self):
            return []

    class _Session:
        async def execute(self, *_a, **_k):
            return _Result()

    yield _Session()


def _patch_common(module):
    """Patch the DB/LLM seams shared by run_l2 and run_l3 so the orchestrator runs without a DB/network.
    Returns the patch context managers as a list for the caller to enter."""
    return [
        patch.object(module, "require_cascade_law_source", lambda layer: None),
        patch.object(module, "get_project", _fake_get_project),
        patch.object(module, "session_scope", _fake_session_scope),
        patch.object(module, "emit", _fake_emit),
        patch.object(
            module,
            "get_settings",
            lambda: SimpleNamespace(openai_api_key="sk-test", law_source="regsuper"),
        ),
        patch.object(module, "AsyncOpenAI", lambda **_k: object()),
    ]


async def _fake_get_project(_pid):
    return SimpleNamespace(id=uuid4(), org_id=uuid4())


async def _fake_emit(*_a, **_k):
    return None


# --------------------------------------------------------------------------- L2 abort
@pytest.mark.parametrize("abort_exc", [KillSwitchEngaged("kill"), SpendLimitExceeded("over")])
async def test_run_l2_aborts_mid_cascade_and_does_not_evaluate_all(abort_exc):
    cands = _candidates(5)
    calls: list = []

    async def fake_evaluate(*, client, project, provision_id, model, analysis_run_id=None):
        calls.append(provision_id)
        # 3rd evaluation trips the hard stop. With the fix this propagates and halts the run; the
        # pre-fix bug would have caught it and let the loop reach all 5.
        if len(calls) == 3:
            raise abort_exc
        return l2.ApplicabilityCall(
            provision_id=provision_id,
            label="§",
            source_external_id="ext",
            result=l2.ApplicabilityResult(applicable=False, confidence=0.9),
            error=None,
            latency_ms=1,
            provision_text="text",
        )

    ctx = _patch_common(l2)
    ctx += [
        patch.object(l2, "_existing_l2_provisions", _fake_empty_set),
        patch.object(l2, "evaluate_provision", fake_evaluate),
    ]
    with _enter_all(ctx):
        with pytest.raises(type(abort_exc)):
            # concurrency=1 makes the abort deterministic: provisions run one at a time, so exactly the
            # first 3 are evaluated and the 4th/5th are never reached.
            await l2.run_l2(uuid4(), candidates=cands, max_concurrency=1)

    assert len(calls) == 3, (
        "run_l2 must STOP at the abort signal, not evaluate the remaining provisions"
    )
    assert len(calls) < len(cands)


# --------------------------------------------------------------------------- L3 abort
@pytest.mark.parametrize("abort_exc", [KillSwitchEngaged("kill"), SpendLimitExceeded("over")])
async def test_run_l3_aborts_mid_cascade_and_does_not_extract_all(abort_exc):
    pids = [uuid4() for _ in range(5)]
    calls: list = []

    async def fake_extract(
        *, client, project_id, provision_id, model, org_id=None, analysis_run_id=None
    ):
        calls.append(provision_id)
        if len(calls) == 3:
            raise abort_exc
        return l3.L3Call(
            provision_id=provision_id,
            source_document_id=uuid4(),
            obligations=[],
            error=None,
            latency_ms=1,
            provision_text="text",
        )

    ctx = _patch_common(l3)
    ctx += [
        patch.object(l3, "_l2_applicable_provisions", _make_async_return(pids)),
        patch.object(l3, "_existing_l3_provisions", _fake_empty_set),
        patch.object(l3, "extract_obligations", fake_extract),
    ]
    with _enter_all(ctx):
        with pytest.raises(type(abort_exc)):
            await l3.run_l3(uuid4(), max_concurrency=1)

    assert len(calls) == 3, (
        "run_l3 must STOP at the abort signal, not extract the remaining provisions"
    )
    assert len(calls) < len(pids)


# --------------------------------------------------------------------------- regression guard
async def test_ordinary_error_does_NOT_abort_the_run():
    """A normal per-provision failure (bad JSON, schema error, provision-not-found) is recorded and the
    loop CONTINUES — only the two abort signals halt the cascade. This pins that we narrowed the catch,
    not broadened it."""
    cands = _candidates(4)
    calls: list = []

    async def fake_evaluate(*, client, project, provision_id, model, analysis_run_id=None):
        calls.append(provision_id)
        # Every call "fails" the ordinary way (result=None, error set). The loop must reach all 4.
        return l2.ApplicabilityCall(
            provision_id=provision_id,
            label="§",
            source_external_id="ext",
            result=None,
            error="json decode: boom",
            latency_ms=1,
            provision_text="text",
        )

    ctx = _patch_common(l2)
    ctx += [
        patch.object(l2, "_existing_l2_provisions", _fake_empty_set),
        patch.object(l2, "evaluate_provision", fake_evaluate),
    ]
    with _enter_all(ctx):
        report = await l2.run_l2(uuid4(), candidates=cands, max_concurrency=1)

    assert len(calls) == len(cands), "ordinary per-item errors must NOT abort the run"
    assert report.n_errors == len(cands)


# --------------------------------------------------------------------------- tiny test plumbing
async def _fake_empty_set(_pid):
    return set()


def _make_async_return(value):
    async def _f(_pid):
        return value

    return _f


class _enter_all:
    """Enter a list of context managers and exit them all (test helper)."""

    def __init__(self, ctxs):
        self._ctxs = ctxs

    def __enter__(self):
        for c in self._ctxs:
            c.__enter__()
        return self

    def __exit__(self, *exc):
        for c in reversed(self._ctxs):
            c.__exit__(*exc)
        return False
