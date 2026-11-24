"""Startup cap-sanity guard — WP-027 (A4-03), src/regsuper/limits.py.

`_validate_limits(cfg)` is a WARNING-ONLY boot check: it emits a single
`limits.suspiciously_low` event when the daily spend cap is set below
`SUSPICIOUS_DAILY_USD` ($10) — the live $1 test caps are the thing it catches.
This suite pins the contract:

  * a $1 daily cap with limits_low_ok=False → emits EXACTLY ONE alarm;
  * the same cap with limits_low_ok=True    → SUPPRESSED (no event);
  * the guard NEVER raises (low cap, zero cap, malformed cap, no-arg boot path);
  * DEFAULT_LIMITS is left untouched (the enforcement module owns it).

Everything is offline: we patch limits.get_settings() to flip limits_low_ok and
capture limits.emit() in-memory (no DB, no network).

asyncio_mode=auto (pyproject) → async test functions run directly.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from regsuper import limits as guard
from regsuper.observability import limits as enforce


@contextmanager
def _patched(*, limits_low_ok: bool):
    """Patch get_settings() (to flip limits_low_ok) and capture emitted events.

    Yields the list of (code, payload) tuples emitted during the block.
    """
    events: list[tuple[str, dict]] = []

    async def fake_emit(code, *_a, module=None, level=None, **payload):
        # Mirror the real emit() signature so module/level are peeled off and `payload`
        # holds only the business fields (the assertion below pins level out of payload).
        events.append((code, payload))
        return None

    def fake_get_settings():
        return SimpleNamespace(limits_low_ok=limits_low_ok)

    with (
        patch.object(guard, "get_settings", fake_get_settings),
        patch.object(guard, "emit", fake_emit),
    ):
        yield events


# --------------------------------------------------------------------------- the alarm fires
async def test_low_daily_cap_fires_once():
    # $1/$1 live test caps + limits_low_ok=False → exactly one alarm, with the daily value.
    with _patched(limits_low_ok=False) as events:
        await guard._validate_limits({"daily_usd": 1.0, "monthly_usd": 1.0})
    codes = [c for c, _ in events]
    assert codes == ["limits.suspiciously_low"], "exactly one suspiciously-low alarm"
    assert events[0][1]["daily_usd"] == 1.0
    assert events[0][1].get("level", None) is None  # level is an emit kwarg, not payload


async def test_alarm_routes_to_polarius_at_warn_level():
    # The alarm is WARNING-ONLY and rides the polarius channel (limits.* prefix).
    captured: dict = {}

    async def fake_emit(code, *, module=None, level="info", **payload):
        captured.update(code=code, module=module, level=level, payload=payload)

    def fake_get_settings():
        return SimpleNamespace(limits_low_ok=False)

    with (
        patch.object(guard, "get_settings", fake_get_settings),
        patch.object(guard, "emit", fake_emit),
    ):
        await guard._validate_limits({"daily_usd": 1.0})
    assert captured["code"] == "limits.suspiciously_low"
    assert captured["module"] == "polarius"
    assert captured["level"] == "warn"
    assert captured["payload"]["daily_usd"] == 1.0


# --------------------------------------------------------------------------- suppression
async def test_suppressed_when_limits_low_ok_true():
    # Same $1 cap, but the operator acknowledged it → no event at all.
    with _patched(limits_low_ok=True) as events:
        await guard._validate_limits({"daily_usd": 1.0, "monthly_usd": 1.0})
    assert events == [], "limits_low_ok=True must suppress the alarm"


# --------------------------------------------------------------------------- non-firing cases
async def test_zero_daily_cap_is_not_flagged():
    # daily_usd=0 means "no daily cap" (per DEFAULT_LIMITS) — that is intentional, not suspicious.
    with _patched(limits_low_ok=False) as events:
        await guard._validate_limits({"daily_usd": 0.0})
    assert events == []


async def test_at_or_above_floor_is_not_flagged():
    # The floor itself ($10) and anything above it is plausible → silent.
    with _patched(limits_low_ok=False) as events:
        await guard._validate_limits({"daily_usd": guard.SUSPICIOUS_DAILY_USD})
        await guard._validate_limits({"daily_usd": 25.0})  # the real DEFAULT_LIMITS value
    assert events == []


async def test_default_limits_daily_cap_is_not_flagged():
    # The shipped default ($25/day) must never trip the guard.
    with _patched(limits_low_ok=False) as events:
        await guard._validate_limits(dict(enforce.DEFAULT_LIMITS))
    assert events == []


# --------------------------------------------------------------------------- never raises
async def test_never_raises_on_low_cap():
    # The whole point: a low cap WARNS, it does not raise / block boot.
    with _patched(limits_low_ok=False):
        await guard._validate_limits({"daily_usd": 1.0})  # would raise if the guard were strict


async def test_never_raises_on_malformed_cap():
    # A non-numeric daily_usd must be swallowed, not crash startup.
    with _patched(limits_low_ok=False) as events:
        await guard._validate_limits({"daily_usd": "oops"})
        await guard._validate_limits({"daily_usd": None})
        await guard._validate_limits({})  # missing key entirely
    assert events == [], "malformed/absent caps produce no alarm and no crash"


async def test_validate_startup_limits_never_raises_even_if_emit_throws():
    # The lifespan wrapper must absorb ANY guard failure so boot is never blocked.
    async def boom_emit(*_a, **_k):
        raise RuntimeError("emit blew up")

    def fake_get_settings():
        return SimpleNamespace(limits_low_ok=False)

    with (
        patch.object(guard, "get_settings", fake_get_settings),
        patch.object(guard, "emit", boom_emit),
        patch.object(guard, "get_limits", lambda: {"daily_usd": 1.0}),
    ):
        # Must return cleanly despite emit raising underneath.
        await guard.validate_startup_limits()


# --------------------------------------------------------------------------- DEFAULT_LIMITS untouched
def test_default_limits_unchanged():
    # The guard reads DEFAULT_LIMITS via the enforcement module but must NEVER mutate it.
    snapshot = copy.deepcopy(enforce.DEFAULT_LIMITS)
    # Run the guard against a copy of the defaults; the canonical dict must be identical afterwards.

    async def _run():
        with _patched(limits_low_ok=False):
            await guard._validate_limits(dict(enforce.DEFAULT_LIMITS))

    import asyncio

    asyncio.run(_run())
    assert enforce.DEFAULT_LIMITS == snapshot, "DEFAULT_LIMITS must not be mutated by the guard"
    # Pin the specific keys this WP cares about so a future edit to the defaults is caught.
    assert enforce.DEFAULT_LIMITS["daily_usd"] == 25.0
    assert enforce.DEFAULT_LIMITS["monthly_usd"] == 250.0
