"""VL-G-06 — the answer-handler BUDGET PRECHECK (the spend gate BEFORE the engine call).

What this pins (the manifest acceptance + the cardinal invariants):
  * flag ON + a $0.01 test cap ⇒ the answer REFUSES (a structured AnswerResponse refuse frame, NOT
    a 500) BEFORE any engine/sidecar call — the sidecar mock is asserted NEVER called.
  * the reserved `queries` quota unit is IDEMPOTENTLY refunded (net-zero) on the refusal.
  * flag OFF (default) ⇒ ZERO precheck calls — byte-identical (the sidecar IS called, no gate runs).
  * ORDER PIN — the precheck runs AFTER check_and_reserve_quota and IMMEDIATELY BEFORE the sidecar
    call (entitlement → INPUT gate → clarify → quota reserve → PRECHECK → sidecar); a 429 from the
    quota reserve wins over the precheck (the reserve runs first).

The router is mounted on a bare FastAPI app (the test_answer_endpoint pattern) — no lifespan, no DB
at import; every seam (the flag, the sidecar, entitlements/metering, the spend gate) is patched.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from regsuper.api import answer as answer_api

_ANON_ORG = UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
def _client() -> TestClient:
    app = FastAPI()
    app.include_router(answer_api.router)
    return TestClient(app, raise_server_exceptions=True)


def _settings(*, answer_budget_precheck_enabled: bool = False) -> SimpleNamespace:
    """A getattr-safe settings namespace for the seams the handler reads (answer surface ON)."""
    return SimpleNamespace(
        answer_endpoint_enabled=True,
        compliance_guard_enabled=False,
        compliance_guard_mode="shadow",
        require_auth=False,
        entitlements_enabled=False,
        answer_shadow_weak_coverage_hedged=False,
        answer_profile_context_enabled=False,
        answer_clarify_enabled=False,
        answer_practical_block_enabled=False,
        answer_intake_panel_enabled=False,
        authority_registry_enabled=False,
        answer_budget_precheck_enabled=answer_budget_precheck_enabled,
    )


def _sound_answer() -> dict:
    """A minimal sound, grounded, sectioned engine answer (one real chip)."""
    return {
        "refused": False,
        "verdict": "ok",
        "drafted_answer": "## Legislativní rámec\nViz § 5 [1].",
        "cited_provisions": [{"provision_id": "p-1"}],
        "citations_map": [{"start": 0, "end": 5, "provision_id": "p-1", "label": "§ 5"}],
        "sections": [
            {
                "kind": "legislative",
                "heading": "Legislativní rámec",
                "text": "Viz § 5.",
                "citations_map": [{"start": 0, "end": 5, "provision_id": "p-1", "label": "§ 5"}],
            },
        ],
        "render_mode": "full",
        "band": "full",
        "meta": {"route": "sound", "intent": "legal", "coverage": "covered"},
    }


def _fake_session_scope():
    """A DB-free async CM standing in for storage.session_scope (the reserve/refund helpers are patched)."""

    @asynccontextmanager
    async def _cm():
        yield object()

    return _cm()


def _entitled_seams(sidecar, *, reserve, refund_calls):
    """Patch the entitlement/metering/limits seams so a `queries` unit IS reserved (entitlements resolve)
    and the refund helper records its calls. Returns the list of active `patch` context managers."""
    from regsuper.observability import limits as limits_mod

    ent = SimpleNamespace(queries_per_day=10, analyses_per_month=3, plan_code="lite")

    async def fake_resolve(org_id, *, session=None):
        return ent

    async def spy_refund(session, *, org_id, reserved_metrics):
        refund_calls.append(",".join(reserved_metrics))

    return [
        patch.object(answer_api.pythia, "answer", sidecar),
        patch.object(answer_api.metering, "resolve_for_entry", fake_resolve),
        patch.object(answer_api.metering, "refund_for_run", spy_refund),
        patch.object(limits_mod, "check_and_reserve_quota", reserve),
        patch.object(answer_api, "session_scope", lambda: _fake_session_scope()),
    ]


# ---------------------------------------------------------------------------
# 1. flag ON + a $0.01 test cap ⇒ REFUSE before any engine call + idempotent refund
# ---------------------------------------------------------------------------
def test_precheck_on_refuses_before_engine_and_refunds() -> None:
    """The REAL enforce_spend_limit path: a $0.01 daily cap vs the ~$1.50 C9 worst-case projection ⇒
    the precheck raises SpendLimitExceeded, the handler returns a STRUCTURED refuse frame BEFORE the
    sidecar is ever called, and the reserved `queries` unit is refunded."""
    from regsuper.observability import limits as limits_mod

    sidecar = AsyncMock(return_value=_sound_answer())
    refund_calls: list[str] = []

    async def reserve(session, org_id, metric, *, limit, now=None):
        return 1

    # A genuine $0.01 daily cap with zero spend today → the ~$1.50 projection breaches it.
    tiny_limits = {"block_when_over": True, "daily_usd": 0.01, "monthly_usd": 0.0}

    async def zero_spend():
        return {
            "today_usd": 0.0,
            "month_usd": 0.0,
            "today_by_provider": [],
            "month_by_provider": [],
        }

    seams = _entitled_seams(sidecar, reserve=reserve, refund_calls=refund_calls)
    with (
        patch.object(
            answer_api, "get_settings", lambda: _settings(answer_budget_precheck_enabled=True)
        ),
        patch.object(limits_mod, "get_limits", lambda: tiny_limits),
        patch.object(limits_mod, "spend_summary", zero_spend),
    ):
        for cm in seams:
            cm.__enter__()
        try:
            resp = _client().post("/api/answer", json={"query": "musím vést evidenci?"})
        finally:
            for cm in reversed(seams):
                cm.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text  # a structured refuse frame, NOT a 500
    body = resp.json()
    assert body["refused"] is True
    assert body["render_mode"] == "refuse"
    assert body["title"] and body["body"]  # the honest refuse-frame copy
    sidecar.assert_not_called()  # the engine/sidecar was NEVER called — zero spend
    assert refund_calls == ["queries"], "the reserved queries unit must be refunded (net-zero)"


def test_precheck_kill_switch_refuses_before_engine() -> None:
    """A KillSwitchEngaged from the precheck is ALSO mapped to the structured refuse frame (not a 500)
    and refunds the reserved unit — the sidecar is never called."""
    from regsuper.observability.killswitch import KillSwitchEngaged

    sidecar = AsyncMock(return_value=_sound_answer())
    refund_calls: list[str] = []

    async def reserve(session, org_id, metric, *, limit, now=None):
        return 1

    def _killed():
        raise KillSwitchEngaged("switch on")

    seams = _entitled_seams(sidecar, reserve=reserve, refund_calls=refund_calls)
    with (
        patch.object(
            answer_api, "get_settings", lambda: _settings(answer_budget_precheck_enabled=True)
        ),
        patch.object(answer_api, "assert_not_killed", _killed),
    ):
        for cm in seams:
            cm.__enter__()
        try:
            resp = _client().post("/api/answer", json={"query": "x"})
        finally:
            for cm in reversed(seams):
                cm.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    assert resp.json()["refused"] is True
    sidecar.assert_not_called()
    assert refund_calls == ["queries"]


def test_precheck_refund_is_idempotent_no_double_refund() -> None:
    """The refund fires EXACTLY once (reserved_metrics.clear() guard) — a second refund of this unit is
    structurally impossible even though the refused frame returns immediately after."""
    from regsuper.observability import limits as limits_mod

    sidecar = AsyncMock(return_value=_sound_answer())
    refund_calls: list[str] = []

    async def reserve(session, org_id, metric, *, limit, now=None):
        return 1

    tiny_limits = {"block_when_over": True, "daily_usd": 0.01, "monthly_usd": 0.0}

    async def zero_spend():
        return {
            "today_usd": 0.0,
            "month_usd": 0.0,
            "today_by_provider": [],
            "month_by_provider": [],
        }

    seams = _entitled_seams(sidecar, reserve=reserve, refund_calls=refund_calls)
    with (
        patch.object(
            answer_api, "get_settings", lambda: _settings(answer_budget_precheck_enabled=True)
        ),
        patch.object(limits_mod, "get_limits", lambda: tiny_limits),
        patch.object(limits_mod, "spend_summary", zero_spend),
    ):
        for cm in seams:
            cm.__enter__()
        try:
            _client().post("/api/answer", json={"query": "x"})
        finally:
            for cm in reversed(seams):
                cm.__exit__(None, None, None)

    assert refund_calls == ["queries"], "the reserved unit is refunded exactly ONCE"


# ---------------------------------------------------------------------------
# 2. flag OFF (default) ⇒ zero precheck calls, byte-identical
# ---------------------------------------------------------------------------
def test_precheck_off_is_byte_identical_no_gate_calls() -> None:
    """With the flag OFF the precheck NEVER runs (assert_not_killed / enforce_spend_limit are never
    called) and the sidecar IS called — the handler is byte-identical to the pre-VL-G-06 baseline."""
    sidecar = AsyncMock(return_value=_sound_answer())
    gate_calls: list[str] = []

    def spy_kill():
        gate_calls.append("kill")

    async def spy_enforce(*, provider=None, projected_cost_usd=0.0, **kwargs):
        gate_calls.append("enforce")

    with (
        patch.object(
            answer_api, "get_settings", lambda: _settings(answer_budget_precheck_enabled=False)
        ),
        patch.object(answer_api.pythia, "answer", sidecar),
        patch.object(answer_api, "assert_not_killed", spy_kill),
        patch.object(answer_api, "enforce_spend_limit", spy_enforce),
    ):
        resp = _client().post("/api/answer", json={"query": "x"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["refused"] is False
    sidecar.assert_called_once()  # the engine WAS called (no gate short-circuit)
    assert gate_calls == [], "flag OFF ⇒ the precheck must make ZERO gate calls"


# ---------------------------------------------------------------------------
# 3. ORDER PIN — precheck AFTER check_and_reserve_quota, BEFORE the sidecar call
# ---------------------------------------------------------------------------
def test_order_pin_precheck_after_reserve_before_sidecar() -> None:
    """Call-order pin: reserve → precheck (kill + enforce) → sidecar. We spy on all four and assert
    the exact interleaving so the precheck can never drift before the reserve or after the engine."""
    from regsuper.observability import limits as limits_mod

    calls: list[str] = []

    async def spy_reserve(session, org_id, metric, *, limit, now=None):
        calls.append("reserve")
        return 1

    def spy_kill():
        calls.append("kill")

    async def spy_enforce(*, provider=None, projected_cost_usd=0.0, **kwargs):
        calls.append(f"enforce:{provider}:{projected_cost_usd}")

    async def spy_answer(*args, **kwargs):
        calls.append("sidecar")
        return _sound_answer()

    ent = SimpleNamespace(queries_per_day=10, analyses_per_month=3, plan_code="lite")

    async def fake_resolve(org_id, *, session=None):
        return ent

    with (
        patch.object(
            answer_api, "get_settings", lambda: _settings(answer_budget_precheck_enabled=True)
        ),
        patch.object(answer_api.pythia, "answer", spy_answer),
        patch.object(answer_api.metering, "resolve_for_entry", fake_resolve),
        patch.object(limits_mod, "check_and_reserve_quota", spy_reserve),
        patch.object(answer_api, "assert_not_killed", spy_kill),
        patch.object(answer_api, "enforce_spend_limit", spy_enforce),
        patch.object(answer_api, "session_scope", lambda: _fake_session_scope()),
    ):
        resp = _client().post("/api/answer", json={"query": "x"})

    assert resp.status_code == 200, resp.text
    assert calls == [
        "reserve",
        "kill",
        f"enforce:{answer_api._PROVIDER_ENGINE_LLM}:{answer_api._ANSWER_ENGINE_WORST_CASE_COST_USD}",
        "sidecar",
    ], calls


def test_order_pin_over_quota_429_wins_over_precheck() -> None:
    """A 429 from the quota reserve WINS over the precheck: the reserve runs first and raises, so the
    precheck (and the sidecar) are never reached — zero spend, no gate call."""
    from regsuper.observability import limits as limits_mod

    reached: list[str] = []

    async def over_quota(session, org_id, metric, *, limit, now=None):
        raise limits_mod.QuotaExceeded(metric, limit, count=limit + 1)

    def spy_kill():
        reached.append("kill")

    async def spy_enforce(*, provider=None, projected_cost_usd=0.0, **kwargs):
        reached.append("enforce")

    async def spy_answer(*args, **kwargs):
        reached.append("sidecar")
        return _sound_answer()

    ent = SimpleNamespace(queries_per_day=3, analyses_per_month=3, plan_code="lite")

    async def fake_resolve(org_id, *, session=None):
        return ent

    with (
        patch.object(
            answer_api, "get_settings", lambda: _settings(answer_budget_precheck_enabled=True)
        ),
        patch.object(answer_api.pythia, "answer", spy_answer),
        patch.object(answer_api.metering, "resolve_for_entry", fake_resolve),
        patch.object(limits_mod, "check_and_reserve_quota", over_quota),
        patch.object(answer_api, "assert_not_killed", spy_kill),
        patch.object(answer_api, "enforce_spend_limit", spy_enforce),
        patch.object(answer_api, "session_scope", lambda: _fake_session_scope()),
    ):
        try:
            _client().post("/api/answer", json={"query": "x"})
        except limits_mod.QuotaExceeded as exc:
            assert exc.status_code == 429
    assert reached == [], "the 429 reserve wins — neither the precheck nor the sidecar runs"
