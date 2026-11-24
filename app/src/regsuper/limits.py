"""Boot-time warning for a daily spend cap that looks like a leftover test value.

Development writes $1/$1 test caps into `data/limits.json`. If a build is promoted to staging or
prod without raising them, every real analysis trips `enforce_spend_limit` within a few calls. This
check only warns: it never raises or blocks boot, and `settings.limits_low_ok` silences it. Spend
enforcement lives in `observability/limits.py`, which owns DEFAULT_LIMITS (read-only here).
"""

from __future__ import annotations

from typing import Any

from regsuper.config import get_settings
from regsuper.observability.events import emit
from regsuper.observability.limits import get_limits

# The floor below which a daily spend cap looks like a leftover test value rather
# than a real budget. The development caps are $1; $10 is the smallest daily cap
# treated as plausibly intentional for a production run.
SUSPICIOUS_DAILY_USD: float = 10.0


async def _validate_limits(cfg: dict[str, Any]) -> None:
    """Warn once if the daily spend cap looks suspiciously low.

    `cfg` is the merged limits dict, as returned by `observability.limits.get_limits()`. Emits
    `limits.suspiciously_low` when `daily_usd` is positive but below SUSPICIOUS_DAILY_USD, unless
    the operator has acknowledged it with `settings.limits_low_ok`. A `daily_usd` of 0 means no
    daily cap (see DEFAULT_LIMITS) and is not flagged. Never raises.

    The `limits.*` event prefix routes to the control-room channel, which has a friendly template
    for this event (see observability/events.py::_MODULE_PREFIXES).
    """
    if get_settings().limits_low_ok:
        return  # operator has acknowledged a low cap — stay silent
    try:
        daily_usd = float(cfg.get("daily_usd") or 0.0)
    except (TypeError, ValueError):
        return  # a malformed cap is not this guard's concern — never raise
    # 0 means no daily cap; only a positive but low cap is suspicious.
    if 0 < daily_usd < SUSPICIOUS_DAILY_USD:
        await emit(
            "limits.suspiciously_low",
            module="polarius",
            level="warn",
            daily_usd=daily_usd,
        )


async def validate_startup_limits() -> None:
    """Load the live limits from `data/limits.json` and run the low-cap check.

    Called from the FastAPI lifespan (api/main.py). Any failure is swallowed so the check can
    never take down boot, even though `_validate_limits` already does not raise.
    """
    try:
        await _validate_limits(get_limits())
    except Exception:
        return
