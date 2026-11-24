"""Entry-point helpers that turn entitlements and quotas into a metered analysis run.

The analyze entry points (`api/projects.py`, and `api/profiles.py` by reuse) call these helpers
to combine the read side (resolver) with the write side (quota reservation, run records and
billing). They are used only when `entitlements_enabled` is on; with the flag off, the entry
points skip them entirely.

Provided helpers:

* ``resolve_for_entry(org_id)``: resolve the org's tier, fail closed. Returns ``None`` when
  entitlements are off so the caller can skip metering.
* ``clamp_layers(requested, entitlement)``: cap the requested layers at the tier's depth. A
  Lite org asking for L4 runs L2 and gets a ``layers_capped`` note, never a 403 or a crash.
* ``answer_mode_for(entitlement)``: the tier's answer mode.
* ``open_analysis_run(...)`` / ``finish_analysis_run(...)``: create and close the
  ``analysis_run`` row. On success the run's cost is rolled up as the sum of
  ``api_call.cost_usd`` for that run.
* ``reserve_for_run(...)`` / ``refund_for_run(...)``: reserve quota before any work (raising
  ``QuotaExceeded`` with 429) and refund it when a run is refused or fails.

Call order the metered path must follow:

1. ``resolve_for_entry``, then ``clamp_layers`` (note only), then ``answer_mode_for``.
2. ``open_analysis_run`` (status 'running').
3. ``reserve_for_run``, which raises 429 before any cascade work starts.
4. Run the cascade.
5. On success: ``finish_analysis_run(status='done')`` (rolls up cost) and record usage.
   On refusal or failure: ``refund_for_run``, then ``finish_analysis_run`` with status
   'refused' or 'failed'.

Invariants:

* ``org_id`` is never null (``analysis_run``, ``usage_counter`` and ``billing_event`` all
  declare it NOT NULL). The real ``Principal.org_id`` is passed through.
* Fail closed: a resolver error propagates and denies; it never becomes a default grant. The
  no-subscription floor (``lite``) is a documented resolution, not a fail-open.
* Quota is reserved before the cascade, so an over-quota request writes zero ``api_call`` rows:
  nothing is spent because nothing ran.
* These helpers run in the caller's session and only ``flush()``; the caller owns the commit.
  The ``quota.exceeded`` billing event is emitted inside ``check_and_reserve_quota``, so it
  must not be emitted again here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from regsuper.config import get_settings
from regsuper.entitlements.models import EffectiveEntitlement
from regsuper.entitlements.resolver import effective_entitlement
from regsuper.observability import limits
from regsuper.storage.database import session_scope
from regsuper.storage.models import AnalysisRun

if TYPE_CHECKING:  # keep the async-session import out of module load
    from sqlalchemy.ext.asyncio import AsyncSession

# Depth ranks, used to intersect a requested layer set with the tier's cap.
_DEPTH_RANK: dict[str, int] = {"L2": 2, "L3": 3, "L4": 4}

# Quota metrics. Every analysis consumes the monthly `analyses` allowance; an analysis that
# actually runs L4 also consumes the monthly `analyses_L4` sub-allowance. Other callers that
# meter analyses must use these exact strings.
_METRIC_ANALYSES = "analyses"
_METRIC_ANALYSES_L4 = "analyses_L4"


def entitlements_on() -> bool:
    """Return True when entitlement enforcement and metering are enabled.

    ``getattr`` keeps this safe against a partially stubbed ``Settings`` (some unit tests
    inject a namespace without the field), in which case it defaults to off.
    """
    return bool(getattr(get_settings(), "entitlements_enabled", False))


async def resolve_for_entry(
    org_id: UUID, *, session: AsyncSession | None = None
) -> EffectiveEntitlement | None:
    """Resolve the org's effective entitlement for an entry point.

    Returns ``None`` when ``entitlements_enabled`` is off, so the caller runs the unclamped,
    unmetered path. When on, it resolves the tier fail closed: a resolver error propagates for
    the caller to turn into a denial and is never swallowed into a default grant. A dedicated
    session is opened when none is supplied so the lookup does not entangle the caller's
    transaction.
    """
    if not entitlements_on():
        return None
    if session is not None:
        return await effective_entitlement(session, org_id)
    async with session_scope() as s:
        return await effective_entitlement(s, org_id)


def clamp_layers(
    requested: list[str], entitlement: EffectiveEntitlement | None
) -> tuple[list[str], str | None]:
    """Intersect the requested layer list with the tier's depth cap.

    A Lite org (cap ``L2``) asking for ``[L2, L3, L4]`` runs ``[L2]`` and gets a
    ``layers_capped`` note; it is never a 403 or a crash. The order of the requested layers is
    preserved and only layers above the cap are dropped. Returns
    ``(executed_layers, note_or_None)``.

    When ``entitlement`` is None (entitlements off), the requested list is returned unchanged
    with no note.
    """
    if entitlement is None:
        return list(requested), None
    cap_rank = _DEPTH_RANK.get(entitlement.max_depth, 4)
    executed = [layer for layer in requested if _DEPTH_RANK.get(layer, 99) <= cap_rank]
    dropped = [layer for layer in requested if layer not in executed]
    if not dropped:
        return executed, None
    note = (
        f"layers_capped: tier '{entitlement.plan_code}' is limited to "
        f"{entitlement.max_depth}; dropped {','.join(dropped)} "
        f"(ran {','.join(executed) or 'none'})"
    )
    return executed, note


def answer_mode_for(entitlement: EffectiveEntitlement | None) -> str | None:
    """Return the tier's answer mode: 'information' for Lite, 'compliance' for higher tiers.

    Returns ``None`` when entitlements are off; no ``analysis_run`` row is written then, so
    there is nothing to stamp.
    """
    return None if entitlement is None else entitlement.answer_mode


async def open_analysis_run(
    session: AsyncSession,
    *,
    org_id: UUID,
    project_id: UUID | None,
    entitlement: EffectiveEntitlement | None,
    requested_layers: list[str],
    executed_layers: list[str],
) -> int | None:
    """Create one ``analysis_run`` row (status='running') and return its id.

    Returns ``None`` and writes nothing when ``entitlement`` is None (entitlements off).
    Otherwise inserts a row with the real ``org_id`` (NOT NULL), a snapshot of the tier, the
    requested and executed depth, and the tier's ``answer_mode``. Runs in the caller's session;
    the caller owns the commit (this only flushes to populate the id).
    """
    if entitlement is None:
        return None
    requested_depth = _highest_depth(requested_layers)
    run = AnalysisRun(
        org_id=org_id,
        project_id=project_id,
        tier=entitlement.plan_code,
        requested_depth=requested_depth,
        requested_layers=list(requested_layers),
        executed_layers=list(executed_layers),
        status="running",
        answer_mode=entitlement.answer_mode,
        started_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()  # populate run.id for threading into the cascade
    return run.id


async def finish_analysis_run(
    session: AsyncSession,
    run_id: int | None,
    *,
    status: str,
    executed_layers: list[str] | None = None,
    error: str | None = None,
) -> None:
    """Close the run: set status and finished_at, and snapshot executed_layers.

    On a successful 'done' the run's ``cost_usd`` is rolled up as the sum of ``api_call.cost_usd``
    for this run. Every cascade ``api_call`` is tagged with the run id, so the sum is the run's
    true spend.

    No-op when ``run_id`` is None (entitlements off, so no run row exists). Runs in the
    caller's session (flush only; the caller commits).
    """
    if run_id is None:
        return
    from sqlalchemy import text as _sql_text

    run = await session.get(AnalysisRun, run_id)
    if run is None:
        return
    run.status = status
    run.finished_at = datetime.now(UTC)
    if executed_layers is not None:
        run.executed_layers = list(executed_layers)
    if error is not None:
        run.error = error[:2000]
    if status == "done":
        # Roll the per-run spend from the api_call rows tagged with this run_id.
        cost = (
            await session.execute(
                _sql_text(
                    "SELECT COALESCE(SUM(cost_usd), 0) FROM api_call WHERE analysis_run_id = :rid"
                ),
                {"rid": run_id},
            )
        ).scalar_one()
        run.cost_usd = float(cost or 0.0)
    await session.flush()


async def reserve_for_run(
    session: AsyncSession,
    *,
    org_id: UUID,
    entitlement: EffectiveEntitlement | None,
    executed_layers: list[str],
) -> list[str]:
    """Reserve quota for the run before any work starts, and return the reserved metrics.

    No-op (returns ``[]``) when ``entitlement`` is None (entitlements off). Otherwise reserves
    the monthly ``analyses`` allowance and, when L4 actually runs, the ``analyses_L4``
    sub-allowance, passing the resolved limit (-1 means unlimited) unchanged as ``limit=``.
    ``check_and_reserve_quota`` raises ``QuotaExceeded`` (429) before the caller starts the
    cascade, so an over-quota request writes no ``api_call`` rows, and it emits the
    ``quota.exceeded`` billing event itself (so it is not emitted here).

    The returned metrics let the caller refund exactly what was reserved if the run is refused
    or fails. If the ``analyses_L4`` reservation raises 429, the ``analyses`` unit is refunded
    before the error propagates, so the counter reflects only admitted requests.
    """
    if entitlement is None:
        return []
    reserved: list[str] = []
    await limits.check_and_reserve_quota(
        session, org_id, _METRIC_ANALYSES, limit=entitlement.analyses_per_month
    )
    reserved.append(_METRIC_ANALYSES)
    if "L4" in executed_layers:
        try:
            await limits.check_and_reserve_quota(
                session,
                org_id,
                _METRIC_ANALYSES_L4,
                limit=entitlement.analyses_per_month,
            )
            reserved.append(_METRIC_ANALYSES_L4)
        except limits.QuotaExceeded:
            # Refund the already-reserved analyses unit so the counter reflects
            # only admitted requests, then propagate the 429.
            await limits.refund_quota(session, org_id, _METRIC_ANALYSES)
            raise
    return reserved


async def refund_for_run(
    session: AsyncSession,
    *,
    org_id: UUID,
    reserved_metrics: list[str],
) -> None:
    """Refund every metric reserved by ``reserve_for_run``.

    A refused or failed run must not consume the user's quota. No-op for an empty list;
    counters never go below zero.
    """
    for metric in reserved_metrics:
        await limits.refund_quota(session, org_id, metric)


async def record_run_usage(
    session: AsyncSession,
    *,
    org_id: UUID,
    reserved_metrics: list[str],
    cost_usd: float,
) -> None:
    """Add the run's spend to the org's usage counters (the count was already reserved).

    No-op when nothing was reserved or the cost is zero. The cost goes on the primary
    ``analyses`` counter; the L4 sub-counter tracks a count only, not a separate spend bucket.
    """
    if not reserved_metrics or not cost_usd:
        return
    await limits.record_usage(session, org_id, _METRIC_ANALYSES, cost_usd=float(cost_usd))


def _highest_depth(layers: list[str]) -> str | None:
    """Return the deepest layer in ``layers`` (L4, then L3, then L2), or None.

    Used for the run row's ``requested_depth`` snapshot; that column only accepts L2, L3 or L4.
    """
    best: tuple[int, str] | None = None
    for layer in layers:
        rank = _DEPTH_RANK.get(layer)
        if rank is not None and (best is None or rank > best[0]):
            best = (rank, layer)
    return best[1] if best else None
