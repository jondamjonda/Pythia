"""AQ-02 — the engine /v1/answer BOUNDARY pre-call spend/kill-switch gate.

What this proves (all DB-free, no real network / Postgres / sidecar):
  1. flag OFF (default) ⇒ answer() POSTs to the sidecar with NO gate call — byte-identical:
     killswitch.assert_not_killed / limits.enforce_spend_limit are NEVER invoked.
  2. flag ON + over-cap (enforce_spend_limit raises SpendLimitExceeded) ⇒ NO POST to the sidecar
     (the pool is never awaited) and SpendLimitExceeded propagates out of answer().
  3. flag ON + kill switch engaged (assert_not_killed raises KillSwitchEngaged) ⇒ NO POST,
     KillSwitchEngaged propagates.
  4. flag ON + under cap + switch clear ⇒ the POST happens normally and the answer returns.
  5. Only answer() is gated: retrieve() (LLM-free, $0) does NOT call the gate even with the flag ON.

The gate sits in client.answer()::_do BEFORE `_pool().post(...)`, so it raises with ZERO spend
(before the sidecar call). `_track` is exercised for real (its persist/emit are stubbed DB-free).
"""

from __future__ import annotations

import pytest

from regsuper.observability.killswitch import KillSwitchEngaged
from regsuper.observability.limits import SpendLimitExceeded
from regsuper.pythia import client as pc


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _FakePool:
    """Records every POST/GET so a test can assert the sidecar was (not) called."""

    def __init__(self, payload):
        self._payload = payload
        self.post_calls: list[tuple] = []
        self.get_calls: list[tuple] = []

    async def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return _FakeResponse(self._payload)

    async def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return _FakeResponse(self._payload)


@pytest.fixture
def wired(monkeypatch):
    """Wire the client for a DB-free call: a fake pool, a fake settings object with the flag,
    a base-url stub, and no-op persist/emit so the real `_track` runs without Postgres.

    Returns a helper that installs a given `gate_enabled` flag and records gate invocations,
    yielding (pool, calls) where `calls` is the list of gate calls actually made."""
    pool = _FakePool({"drafted_answer": "ok", "render_mode": "full"})
    monkeypatch.setattr(pc, "_pool", lambda: pool)
    monkeypatch.setattr(pc, "_base_url", lambda: "http://sidecar")

    # DB-free: stub the boundary telemetry so real `_track` never touches Postgres.
    async def _noop_persist(**kwargs):
        return None

    async def _noop_emit(*args, **kwargs):
        return None

    import regsuper.observability.api_tracker as _api_tracker
    import regsuper.observability.events as _events

    monkeypatch.setattr(_api_tracker, "_persist_row", _noop_persist)
    monkeypatch.setattr(_events, "emit", _noop_emit)
    # _track / _write_engine_llm_rows import emit locally from the events module, so patching
    # the module attribute is enough. api_tracker is imported inside _track the same way.
    return pool


def _set_flag(monkeypatch, *, enabled: bool):
    class _S:
        pythia_boundary_spend_gate_enabled = enabled

    monkeypatch.setattr(pc, "get_settings", lambda: _S())


def _spy_gate(monkeypatch):
    """Replace killswitch.assert_not_killed + limits.enforce_spend_limit with recording spies.
    Returns the shared call-log list."""
    calls: list[str] = []
    from regsuper.observability import killswitch, limits

    def _assert_not_killed():
        calls.append("killswitch")

    async def _enforce(*, provider=None, **kwargs):
        calls.append(f"limits:{provider}")

    monkeypatch.setattr(killswitch, "assert_not_killed", _assert_not_killed)
    monkeypatch.setattr(limits, "enforce_spend_limit", _enforce)
    return calls


# ---------------------------------------------------------------------------
# 1. flag OFF ⇒ POST happens, gate NEVER called (byte-identical).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flag_off_posts_without_gate(wired, monkeypatch):
    pool = wired
    _set_flag(monkeypatch, enabled=False)
    calls = _spy_gate(monkeypatch)

    result = await pc.answer("q")

    assert result == {"drafted_answer": "ok", "render_mode": "full"}
    assert len(pool.post_calls) == 1, "the sidecar POST must happen when the gate is OFF"
    assert pool.post_calls[0][0] == "http://sidecar/v1/answer"
    assert calls == [], "no gate call may run when the flag is OFF (byte-identical)"


# ---------------------------------------------------------------------------
# 2. flag ON + over-cap ⇒ NO POST, SpendLimitExceeded propagates.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flag_on_over_cap_blocks_before_post(wired, monkeypatch):
    pool = wired
    _set_flag(monkeypatch, enabled=True)
    from regsuper.observability import killswitch, limits

    monkeypatch.setattr(killswitch, "assert_not_killed", lambda: None)

    async def _enforce(*, provider=None, **kwargs):
        # VL-G-06 re-point: the gate now enforces the engine-LLM provider caps, not 'openai'.
        assert provider == "pythia_engine_llm"
        raise SpendLimitExceeded("daily cap hit")

    monkeypatch.setattr(limits, "enforce_spend_limit", _enforce)

    with pytest.raises(SpendLimitExceeded):
        await pc.answer("q")

    assert pool.post_calls == [], "no sidecar POST may happen once the spend gate raises"


# ---------------------------------------------------------------------------
# 3. flag ON + kill switch engaged ⇒ NO POST, KillSwitchEngaged propagates.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flag_on_kill_switch_blocks_before_post(wired, monkeypatch):
    pool = wired
    _set_flag(monkeypatch, enabled=True)
    from regsuper.observability import killswitch, limits

    def _assert_not_killed():
        raise KillSwitchEngaged("switch on")

    enforce_called: list[bool] = []

    async def _enforce(*, provider=None, **kwargs):
        enforce_called.append(True)

    monkeypatch.setattr(killswitch, "assert_not_killed", _assert_not_killed)
    monkeypatch.setattr(limits, "enforce_spend_limit", _enforce)

    with pytest.raises(KillSwitchEngaged):
        await pc.answer("q")

    assert pool.post_calls == [], "no sidecar POST may happen once the kill switch raises"
    assert enforce_called == [], "kill-switch check must run FIRST (before enforce_spend_limit)"


# ---------------------------------------------------------------------------
# 4. flag ON + under cap + switch clear ⇒ POST happens normally.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flag_on_under_cap_posts_normally(wired, monkeypatch):
    pool = wired
    _set_flag(monkeypatch, enabled=True)
    calls = _spy_gate(monkeypatch)

    result = await pc.answer("q")

    assert result == {"drafted_answer": "ok", "render_mode": "full"}
    assert len(pool.post_calls) == 1, "the POST must happen once the gate passes"
    assert calls == ["killswitch", "limits:pythia_engine_llm"], (
        "both gate checks must run, kill switch first, then the engine-LLM-provider spend cap"
    )


# ---------------------------------------------------------------------------
# 5. retrieve() (LLM-free, $0) is NOT gated even with the flag ON.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_retrieve_is_not_gated(wired, monkeypatch):
    pool = wired
    _set_flag(monkeypatch, enabled=True)
    calls = _spy_gate(monkeypatch)

    await pc.retrieve("q")

    assert len(pool.post_calls) == 1, "retrieve must POST to the sidecar"
    assert pool.post_calls[0][0] == "http://sidecar/v1/retrieve"
    assert calls == [], "retrieve() is LLM-free ($0) and must never call the spend/kill gate"
