"""Conduit rate limiting: a token bucket per (org_id, credential_id), honouring the kill switch.

Runs in the request chain before `meter.precheck`:

* Token bucket per `(org_id, credential_id)`. Over the limit, the caller answers 429 with
  `Retry-After` (the same status as quota exhaustion). Buckets are process-local; a
  Redis-backed distributed limiter is a possible later addition (Redis is an optional
  dependency and is not imported here).
* Kill switch. When the global flat-file kill switch (`observability.killswitch`) is engaged,
  every Conduit tool returns 503. This differs from `conduit_enabled=False`, which stops the
  service from starting at all; the kill switch moves a running service to 503.

Abuse heuristics, such as alerting when a caller keeps tripping the reserved-advice refusal,
are planned but not implemented here.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from regsuper.observability import killswitch

_WP = "WP-062 (Stage-2 Conduit build)"

# Default bucket geometry (conservative; per-plan overrides are not wired yet). Capacity is the
# burst size (that many requests are admitted, then the next gets 429); the refill rate is the
# steady token replenishment per second.
_DEFAULT_CAPACITY = 60.0
_DEFAULT_REFILL_PER_S = 1.0


@dataclass(slots=True)
class RateLimitDecision:
    """Outcome of a rate-limit check.

    When `allowed` is False the caller raises 429 with `retry_after` seconds. When
    `killed` is True the caller returns 503 (kill switch engaged) for every tool.
    """

    allowed: bool
    retry_after: int | None = None  # seconds; set on a 429
    killed: bool = False  # kill switch engaged: 503 for every tool


@dataclass(slots=True)
class _Bucket:
    """A single token bucket: `tokens` available, last refilled at `updated`."""

    tokens: float
    updated: float


# Buckets keyed by (org_id, credential_id), guarded by a lock so concurrent requests in a worker
# do not race on the token arithmetic. Process-local: each worker process has its own limiter.
_BUCKETS: dict[tuple[str, str], _Bucket] = {}
_LOCK = threading.Lock()
_CAPACITY = _DEFAULT_CAPACITY
_REFILL_PER_S = _DEFAULT_REFILL_PER_S


def configure(*, capacity: float | None = None, refill_per_s: float | None = None) -> None:
    """Set the bucket geometry (used by tests, and the hook for future per-plan limits).

    Clears the live buckets so each `(org_id, credential_id)` starts full after a change.
    """
    global _CAPACITY, _REFILL_PER_S
    with _LOCK:
        if capacity is not None:
            _CAPACITY = float(capacity)
        if refill_per_s is not None:
            _REFILL_PER_S = float(refill_per_s)
        _BUCKETS.clear()


def reset() -> None:
    """Clear all bucket state and restore the default geometry (for test isolation)."""
    global _CAPACITY, _REFILL_PER_S
    with _LOCK:
        _CAPACITY = _DEFAULT_CAPACITY
        _REFILL_PER_S = _DEFAULT_REFILL_PER_S
        _BUCKETS.clear()


def _now() -> float:
    return time.monotonic()


async def check(org_id: str, credential_id: str) -> RateLimitDecision:
    """Consume one token from the `(org_id, credential_id)` bucket.

    The kill switch is checked first: if engaged, returns `killed=True` (the caller answers
    503) without consuming a token. Otherwise the bucket is refilled for the elapsed time and
    the request is admitted if at least one token remains. Over the limit, returns
    `allowed=False` with `retry_after`, and the caller answers 429 with `Retry-After`. Buckets
    are separate per credential, so one credential exhausting its budget never throttles
    another.
    """
    if killswitch.is_killed():
        return RateLimitDecision(allowed=False, killed=True)

    key = (org_id, credential_id)
    now = _now()
    with _LOCK:
        bucket = _BUCKETS.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=_CAPACITY, updated=now)
            _BUCKETS[key] = bucket
        else:
            elapsed = max(0.0, now - bucket.updated)
            bucket.tokens = min(_CAPACITY, bucket.tokens + elapsed * _REFILL_PER_S)
            bucket.updated = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return RateLimitDecision(allowed=True)

        # Out of tokens: compute when one token will have refilled.
        deficit = 1.0 - bucket.tokens
        retry_after = 1 if _REFILL_PER_S <= 0 else max(1, int(deficit / _REFILL_PER_S + 0.999))
        return RateLimitDecision(allowed=False, retry_after=retry_after)


def kill_switch_engaged() -> bool:
    """Return True when the global kill switch is engaged (every tool then returns 503).

    This is the runtime flat-file kill switch, distinct from the `conduit_enabled` setting,
    which stops the service from starting at all.
    """
    return killswitch.is_killed()


__all__ = [
    "RateLimitDecision",
    "check",
    "configure",
    "kill_switch_engaged",
    "reset",
]
