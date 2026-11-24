"""Per-key WEEKLY budget-cap ladder — WARN → SOFT-BLOCK → HARD-BLOCK → killswitch.

This is a trust-critical money gate (UI Build Plan §5.2/§5.3): a runaway key must NOT keep
spending. The ladder lives in `observability/limits.py`:

    _cap_tier(pct):   ok <80 · warn 80–99 · soft 100–124 · hard ≥125   (pct = 100*spend/cap)
    _enforce_weekly_key_cap:
        cap <= 0            → no-op (uncapped key)
        warn (80–99%)       → emit budget.key.warn, PROCEED (no raise)
        soft (100–124%)     → emit limits.spend.exceeded, RAISE SpendLimitExceeded
        hard (>=125%)       → emit + killswitch.engage() + RAISE SpendLimitExceeded

Everything here is fully mocked — no DB, no network. We patch:
  * limits.get_limits            → in-memory cap config
  * limits._weekly_spent_by_key  → in-memory per-key spend (the only DB read in this path)
  * limits.emit                  → capture emitted event codes
  * limits.killswitch.engage     → assert the hard-stop fires exactly when expected

asyncio_mode=auto (pyproject) → async test functions run directly.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from regsuper.observability import limits


# --------------------------------------------------------------------------- helpers
@contextmanager
def _patched(*, caps: dict[str, float], spent: dict[str, float]):
    """Patch the cap config + weekly-spend reader + side-effects (emit, killswitch.engage).

    Yields a dict capturing `events` (list of emitted codes) and `killswitch` (list of reasons).
    """
    captured: dict[str, list] = {"events": [], "killswitch": []}

    async def fake_emit(code, *_a, **_k):
        captured["events"].append(code)
        return None

    async def fake_spent_by_key():
        return dict(spent)

    def fake_engage(reason="manual"):
        captured["killswitch"].append(reason)
        return {"active": True, "reason": reason}

    def fake_get_limits():
        base = dict(limits.DEFAULT_LIMITS)
        base["weekly_key_caps_usd"] = dict(caps)
        return base

    with (
        patch.object(limits, "get_limits", fake_get_limits),
        patch.object(limits, "_weekly_spent_by_key", fake_spent_by_key),
        patch.object(limits, "emit", fake_emit),
        patch.object(limits.killswitch, "engage", fake_engage),
    ):
        yield captured


# --------------------------------------------------------------------------- _cap_tier math
def test_cap_tier_boundaries():
    # Exact ladder boundaries — these constants are the contract, pin them.
    assert limits._cap_tier(0.0) == "ok"
    assert limits._cap_tier(79.999) == "ok"
    assert limits._cap_tier(80.0) == "warn"
    assert limits._cap_tier(99.999) == "warn"
    assert limits._cap_tier(100.0) == "soft"
    assert limits._cap_tier(124.999) == "soft"
    assert limits._cap_tier(125.0) == "hard"
    assert limits._cap_tier(1000.0) == "hard"


# --------------------------------------------------------------------------- the four rungs
async def test_uncapped_key_is_noop():
    # No cap configured for the key → no enforcement, no events, no killswitch.
    with _patched(caps={}, spent={"openai:abc": 999.0}) as cap:
        await limits._enforce_weekly_key_cap("openai:abc", projected_cost_usd=10.0)
    assert cap["events"] == []
    assert cap["killswitch"] == []


async def test_cap_zero_is_noop():
    # An explicit 0 cap means "uncapped" (see DEFAULT comment) — must not block.
    with _patched(caps={"openai:abc": 0.0}, spent={"openai:abc": 50.0}) as cap:
        await limits._enforce_weekly_key_cap("openai:abc", projected_cost_usd=5.0)
    assert cap["events"] == []
    assert cap["killswitch"] == []


async def test_below_warn_proceeds_silently():
    # 50/100 = 50% → ok rung: proceeds, no event.
    with _patched(caps={"openai:abc": 100.0}, spent={"openai:abc": 40.0}) as cap:
        await limits._enforce_weekly_key_cap("openai:abc", projected_cost_usd=10.0)  # → 50%
    assert cap["events"] == []
    assert cap["killswitch"] == []


async def test_warn_emits_and_proceeds():
    # 85/100 = 85% → warn rung: emits budget.key.warn but does NOT raise.
    with _patched(caps={"openai:abc": 100.0}, spent={"openai:abc": 80.0}) as cap:
        await limits._enforce_weekly_key_cap("openai:abc", projected_cost_usd=5.0)  # → 85%
    assert cap["events"] == ["budget.key.warn"]
    assert cap["killswitch"] == []  # warn never engages the kill-switch


async def test_soft_block_raises_without_killswitch():
    # 110/100 = 110% → soft rung: raises SpendLimitExceeded, kill-switch stays OFF
    # (other keys must keep working through a soft block).
    with _patched(caps={"openai:abc": 100.0}, spent={"openai:abc": 100.0}) as cap:
        with pytest.raises(limits.SpendLimitExceeded) as ei:
            await limits._enforce_weekly_key_cap("openai:abc", projected_cost_usd=10.0)  # → 110%
    assert "soft-block" in str(ei.value)
    assert cap["events"] == ["limits.spend.exceeded"]
    assert cap["killswitch"] == []  # CRITICAL: soft must NOT halt the whole pipeline


async def test_hard_block_raises_and_engages_killswitch():
    # 130/100 = 130% → hard rung: raises AND engages the global kill-switch.
    with _patched(caps={"openai:abc": 100.0}, spent={"openai:abc": 120.0}) as cap:
        with pytest.raises(limits.SpendLimitExceeded) as ei:
            await limits._enforce_weekly_key_cap("openai:abc", projected_cost_usd=10.0)  # → 130%
    assert "hard-block" in str(ei.value)
    # both the spend-exceeded AND the killswitch.engaged event fire
    assert "limits.spend.exceeded" in cap["events"]
    assert "killswitch.engaged" in cap["events"]
    assert len(cap["killswitch"]) == 1  # the hard stop fired exactly once
    assert "openai:abc" in cap["killswitch"][0]  # reason names the runaway key


async def test_projected_cost_pushes_over_the_line():
    # 99 spent, cap 100 → 99% (warn) on its own, but a $2 projected call → 101% (soft).
    # The gate must price IN the call we are about to make, not just what's already spent.
    with _patched(caps={"openai:abc": 100.0}, spent={"openai:abc": 99.0}) as cap:
        with pytest.raises(limits.SpendLimitExceeded):
            await limits._enforce_weekly_key_cap("openai:abc", projected_cost_usd=2.0)  # → 101%
    assert "limits.spend.exceeded" in cap["events"]


async def test_other_keys_unaffected_by_one_runaway():
    # A different key over its cap must not block the key we're enforcing (per-key isolation).
    caps = {"openai:runaway": 10.0, "openai:healthy": 100.0}
    spent = {"openai:runaway": 999.0, "openai:healthy": 10.0}  # healthy at 10%
    with _patched(caps=caps, spent=spent) as cap:
        await limits._enforce_weekly_key_cap("openai:healthy", projected_cost_usd=5.0)  # → 15%
    assert cap["events"] == []
    assert cap["killswitch"] == []


# --------------------------------------------------------------------------- _week_start (UTC Monday)
def test_week_start_is_utc_monday_midnight():
    # A Wednesday → Monday of the same ISO week, midnight UTC.
    wed = datetime(2026, 6, 17, 14, 30, 45, tzinfo=UTC)  # 2026-06-17 is a Wednesday
    monday = limits._week_start(wed)
    assert monday == datetime(2026, 6, 15, 0, 0, 0, tzinfo=UTC)
    assert monday.weekday() == 0
    # A Monday maps to itself at midnight.
    mon = datetime(2026, 6, 15, 9, 0, 0, tzinfo=UTC)
    assert limits._week_start(mon) == datetime(2026, 6, 15, 0, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- full enforce_spend_limit path
async def test_enforce_spend_limit_resolves_provider_key_and_hard_blocks():
    """End-to-end through the public gate: provider 'openai' resolves to its active key, whose weekly
    cap is breached at the HARD rung → raises + engages the kill-switch. Daily/monthly/provider caps are
    all off (0) so only the weekly per-key rung fires. spend_summary + the weekly reader are mocked (no DB)."""
    captured: dict[str, list] = {"events": [], "killswitch": []}

    async def fake_emit(code, *_a, **_k):
        captured["events"].append(code)

    async def fake_summary():
        return {
            "today_usd": 0.0,
            "month_usd": 0.0,
            "today_by_provider": [],
            "month_by_provider": [],
        }

    async def fake_spent_by_key():
        return {"openai:THEKEY": 200.0}  # already way over a $100 cap

    def fake_get_limits():
        base = dict(limits.DEFAULT_LIMITS)
        base["weekly_key_caps_usd"] = {"openai:THEKEY": 100.0}
        return base

    def fake_engage(reason="manual"):
        captured["killswitch"].append(reason)
        return {"active": True}

    with (
        patch.object(limits, "get_limits", fake_get_limits),
        patch.object(limits, "spend_summary", fake_summary),
        patch.object(limits, "_weekly_spent_by_key", fake_spent_by_key),
        patch.object(limits, "emit", fake_emit),
        patch.object(limits.killswitch, "engage", fake_engage),
        patch.object(limits.keys, "active_key_id", lambda provider: "openai:THEKEY"),
    ):
        with pytest.raises(limits.SpendLimitExceeded):
            await limits.enforce_spend_limit(provider="openai", projected_cost_usd=1.0)

    assert captured["killswitch"], "hard weekly breach must engage the kill-switch"
    assert "killswitch.engaged" in captured["events"]


async def test_block_when_over_false_disables_all_gates():
    # The operator escape hatch: block_when_over=False short-circuits BEFORE any DB read or cap check.
    def fake_get_limits():
        base = dict(limits.DEFAULT_LIMITS)
        base["block_when_over"] = False
        base["weekly_key_caps_usd"] = {"openai:THEKEY": 1.0}  # would otherwise hard-block
        return base

    async def boom_summary():
        raise AssertionError("spend_summary must not be called when block_when_over=False")

    with (
        patch.object(limits, "get_limits", fake_get_limits),
        patch.object(limits, "spend_summary", boom_summary),
    ):
        # Returns cleanly — no raise, no DB.
        await limits.enforce_spend_limit(provider="openai", projected_cost_usd=10_000.0)
