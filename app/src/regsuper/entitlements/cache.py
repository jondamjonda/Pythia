"""Per-org entitlement cache with a 60-second TTL.

Once `entitlements_enabled` is on, `effective_entitlement` runs on the request hot path (every
guarded route and the Conduit metering precheck resolve the org's tier). The join is cheap but
runs on every request; a short TTL absorbs repeated reads within a burst. A plan change never
serves a stale tier on this process because `billing.on_subscription_changed(org_id)`
invalidates the entry immediately.

Design:

* Module-level dict keyed by `org_id`, holding `(EffectiveEntitlement, expiry)`.
* TTL is 60 seconds (`_TTL_SECONDS`). A `get()` past expiry is a miss and prunes the entry.
* `invalidate(org_id)` drops one org; `clear()` drops all (for test isolation).
* The time source is injectable (`now`) so the TTL is testable without sleeping.
* In-process only, with no cross-replica coherence: across replicas a tier read may be up to
  60 seconds stale, which is the documented bound.

Only the resolver reads or writes this cache, and the resolver runs only when
`entitlements_enabled` is on.
"""

from __future__ import annotations

import time
from threading import Lock
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:  # needed only for annotations
    from regsuper.entitlements.models import EffectiveEntitlement

# How long a resolved entitlement stays cached, in seconds.
_TTL_SECONDS: float = 60.0

# Maps org_id to (entitlement, monotonic expiry time). Guarded by _LOCK so concurrent resolves
# for the same org cannot corrupt the dict (each set, get and pop is cheap).
_CACHE: dict[UUID, tuple[EffectiveEntitlement, float]] = {}
_LOCK = Lock()


def _now() -> float:
    """Monotonic clock for TTL math (immune to wall-clock jumps)."""
    return time.monotonic()


def get(org_id: UUID, *, now: float | None = None) -> EffectiveEntitlement | None:
    """Return the cached entitlement for ``org_id`` if present and unexpired.

    A hit past its TTL is treated as a miss and pruned. ``now`` (monotonic
    seconds) is injectable for deterministic TTL tests.
    """
    ts = _now() if now is None else now
    with _LOCK:
        hit = _CACHE.get(org_id)
        if hit is None:
            return None
        ent, expiry = hit
        if ts >= expiry:
            # Expired: prune so the dict does not grow unbounded with stale entries.
            _CACHE.pop(org_id, None)
            return None
        return ent


def put(
    org_id: UUID,
    entitlement: EffectiveEntitlement,
    *,
    now: float | None = None,
    ttl: float = _TTL_SECONDS,
) -> None:
    """Cache ``entitlement`` for ``org_id`` with a fresh ``ttl`` (default 60s)."""
    ts = _now() if now is None else now
    with _LOCK:
        _CACHE[org_id] = (entitlement, ts + ttl)


def invalidate(org_id: UUID) -> None:
    """Drop ``org_id``'s cached entitlement.

    Called by `billing.on_subscription_changed` so the next `effective_entitlement` resolves
    the new plan instead of waiting out the TTL. Idempotent: dropping an absent org is a no-op.
    """
    with _LOCK:
        _CACHE.pop(org_id, None)


def clear() -> None:
    """Drop the entire cache. Primarily for test isolation."""
    with _LOCK:
        _CACHE.clear()
