"""Operator limits (storage cap, API spend caps, resource ceilings) and per-org quotas.

Limits are stored as JSON in `data/limits.json`, so they survive restarts and the dashboard can
edit them without a code change. Every tracked API call goes through `enforce_spend_limit()`,
which refuses to proceed once the configured budget is used up.

Spend limits are soft: they raise an exception rather than stopping the process, so the
operator sees a clear error in the UI. The kill switch (`killswitch`) is the hard stop.

Besides the global daily and monthly caps there are per-provider caps
(`daily_<provider>_usd` and `monthly_<provider>_usd` for openai, cohere and anthropic). Set one to
0 to disable it; the global caps still apply. They are useful for ring-fencing one vendor's bill.
Per-API-key weekly caps escalate from a warning to a block and, at 125%, to the kill switch.

The second half of the module enforces per-org commercial quotas (see the section header
below).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func, select, text

from regsuper.config import get_settings
from regsuper.observability import keys, killswitch
from regsuper.observability.events import emit
from regsuper.storage.database import session_scope
from regsuper.storage.models import ApiCall

if TYPE_CHECKING:  # annotation-only import
    from sqlalchemy.ext.asyncio import AsyncSession

LIMITS_FILENAME = "limits.json"

# Auto-ingestion on/off. The authoritative state is a key in limits.json (so it survives restarts
# without a database migration). Every change is also mirrored to a plain file in data/ that the
# engine reads at the start of each cycle, the same shared-directory handshake as KILL_SWITCH and
# gpu_mps.json. The app is the only writer; the engine never writes the flag.
AUTO_INGEST_FLAG_KEY = "auto_ingestion_enabled"
AUTO_INGEST_MIRROR_FILENAME = "AUTO_INGEST_ENABLED"

# Providers whose spend is tracked. Adding one means:
#   1. add per-provider entries to DEFAULT_LIMITS,
#   2. tag api_call rows with `provider=<name>` from the new wrapper.
KNOWN_PROVIDERS: tuple[str, ...] = ("openai", "cohere", "anthropic", "pythia", "conduit")

DEFAULT_LIMITS: dict[str, Any] = {
    "storage_gb": 50,  # data_dir cap in GB; 0 = no limit
    "daily_usd": 25.0,  # global API spend per UTC day; 0 = no limit
    "monthly_usd": 250.0,  # global API spend per UTC month; 0 = no limit
    "block_when_over": True,  # refuse new API calls once over a cap
    # Auto-ingestion master switch, off by default. Mirrored to data/AUTO_INGEST_ENABLED on every
    # change so the engine can read it.
    AUTO_INGEST_FLAG_KEY: False,
    # Per-provider caps; 0 = no provider-specific cap (the global caps still apply).
    "daily_openai_usd": 0.0,
    "monthly_openai_usd": 0.0,
    "daily_cohere_usd": 0.0,
    "monthly_cohere_usd": 0.0,
    "daily_anthropic_usd": 0.0,
    "monthly_anthropic_usd": 0.0,
    # Per-API-key weekly caps, {key_id: usd}. A key without an entry has no weekly cap.
    "weekly_key_caps_usd": {},
    # Hard resource ceilings, enforced by resource_guard's gates and watchdog (macOS has no
    # cgroups). 0 turns a ceiling off.
    "resource_caps": {
        "min_free_disk_gb": 5.0,  # refuse a heavy write if free disk would drop below this
        "max_db_size_gb": 0.0,  # 0 = no DB-size ceiling
        "max_mps_mem_gb": 0.0,  # 0 = no MPS ceiling (advisory; only during a live GNN run)
        "cpu_refuse_pct": 0.0,  # 0 = never refuse new analysis on CPU
        "mem_refuse_pct": 95.0,  # refuse new heavy work above this RAM%
        "watchdog_enabled": True,
    },
}


def limits_path() -> Path:
    return get_settings().data_dir / LIMITS_FILENAME


def get_limits() -> dict[str, Any]:
    p = limits_path()
    if not p.exists():
        return dict(DEFAULT_LIMITS)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_LIMITS)
    merged = dict(DEFAULT_LIMITS)
    for k, v in raw.items():
        if k not in DEFAULT_LIMITS:
            continue
        # Deep-merge the nested dict keys so a partial saved file keeps the default sub-keys.
        if isinstance(DEFAULT_LIMITS[k], dict) and isinstance(v, dict):
            base = dict(DEFAULT_LIMITS[k])
            base.update(v)
            merged[k] = base
        else:
            merged[k] = v
    return merged


def set_limits(patch: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist a partial update.

    Nested dict keys (weekly_key_caps_usd, resource_caps) are merged, so sub-keys the patch omits
    are left unchanged. Unknown keys and invalid numbers are ignored.
    """
    current = get_limits()
    for k, v in patch.items():
        if k not in DEFAULT_LIMITS:
            continue
        if k in ("block_when_over", AUTO_INGEST_FLAG_KEY):
            current[k] = bool(v)
        elif isinstance(DEFAULT_LIMITS[k], dict):
            base = dict(current.get(k) or {})
            if isinstance(v, dict):
                for sk, sv in v.items():
                    base[sk] = sv  # caller-supplied sub-values pass through (usd floats / bool)
            current[k] = base
        else:
            try:
                current[k] = float(v) if isinstance(DEFAULT_LIMITS[k], float) else int(v)
            except (TypeError, ValueError):
                continue
    p = limits_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


# ── Auto-ingestion on/off ─────────────────────────────────────────────────────────────────────────
def auto_ingest_mirror_path() -> Path:
    """Return the path of the mirror file the engine reads at the start of every cycle.

    It contains '1' when auto-ingestion is enabled and '0' when it is disabled. Only the app writes
    it.
    """
    return get_settings().data_dir / AUTO_INGEST_MIRROR_FILENAME


def get_auto_ingest_enabled() -> bool:
    """Return the authoritative auto-ingestion flag from limits.json."""
    return bool(get_limits().get(AUTO_INGEST_FLAG_KEY, False))


def _write_auto_ingest_mirror(enabled: bool) -> None:
    """Mirror the flag to data/AUTO_INGEST_ENABLED.

    The file is always written, never deleted, so the engine sees an explicit '1' or '0' rather than
    an ambiguous absence. A missing file counts as disabled, so even a failed write fails safe.
    """
    p = auto_ingest_mirror_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("1" if enabled else "0", encoding="utf-8")


def set_auto_ingest_enabled(enabled: bool) -> bool:
    """Set the auto-ingestion switch in limits.json and mirror it for the engine.

    Returns the persisted value. This is the only write path for the flag (both admin endpoints use
    it), so the stored value and the mirror file cannot diverge.
    """
    enabled = bool(enabled)
    set_limits({AUTO_INGEST_FLAG_KEY: enabled})
    _write_auto_ingest_mirror(enabled)
    return get_auto_ingest_enabled()


async def spend_summary() -> dict[str, Any]:
    """Return today's and this month's spend, with per-source and per-provider breakdowns.

    The admin dashboard renders its bars and sortable table from this one payload.
    """
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = today_start.replace(day=1)

    async with session_scope() as session:
        today_total = (
            await session.execute(
                select(func.coalesce(func.sum(ApiCall.cost_usd), 0.0)).where(
                    ApiCall.ts >= today_start
                )
            )
        ).scalar_one()
        month_total = (
            await session.execute(
                select(func.coalesce(func.sum(ApiCall.cost_usd), 0.0)).where(
                    ApiCall.ts >= month_start
                )
            )
        ).scalar_one()
        by_source = (
            await session.execute(
                select(
                    ApiCall.source,
                    func.count(ApiCall.id),
                    func.coalesce(func.sum(ApiCall.cost_usd), 0.0),
                )
                .where(ApiCall.ts >= today_start)
                .group_by(ApiCall.source)
                .order_by(func.sum(ApiCall.cost_usd).desc())
            )
        ).all()
        by_provider_today = (
            await session.execute(
                select(
                    ApiCall.provider,
                    func.count(ApiCall.id),
                    func.coalesce(func.sum(ApiCall.cost_usd), 0.0),
                )
                .where(ApiCall.ts >= today_start)
                .group_by(ApiCall.provider)
            )
        ).all()
        by_provider_month = (
            await session.execute(
                select(
                    ApiCall.provider,
                    func.count(ApiCall.id),
                    func.coalesce(func.sum(ApiCall.cost_usd), 0.0),
                )
                .where(ApiCall.ts >= month_start)
                .group_by(ApiCall.provider)
            )
        ).all()

    today_provider = {p: float(c or 0.0) for p, _, c in by_provider_today}
    today_provider_calls = {p: int(n or 0) for p, n, _ in by_provider_today}
    month_provider = {p: float(c or 0.0) for p, _, c in by_provider_month}
    month_provider_calls = {p: int(n or 0) for p, n, _ in by_provider_month}

    return {
        "today_usd": float(today_total or 0.0),
        "month_usd": float(month_total or 0.0),
        "today_by_source": [
            {"source": s, "n_calls": n, "cost_usd": float(c)} for s, n, c in by_source
        ],
        "today_by_provider": [
            {
                "provider": p,
                "n_calls": today_provider_calls.get(p, 0),
                "cost_usd": today_provider.get(p, 0.0),
            }
            for p in sorted(
                set(today_provider.keys()) | set(KNOWN_PROVIDERS),
                key=lambda p: -today_provider.get(p, 0.0),
            )
        ],
        "month_by_provider": [
            {
                "provider": p,
                "n_calls": month_provider_calls.get(p, 0),
                "cost_usd": month_provider.get(p, 0.0),
            }
            for p in sorted(
                set(month_provider.keys()) | set(KNOWN_PROVIDERS),
                key=lambda p: -month_provider.get(p, 0.0),
            )
        ],
    }


class SpendLimitExceeded(RuntimeError):
    """An API call would push spend over a daily, monthly or weekly budget."""


def _provider_cap(limits: dict[str, Any], provider: str, window: str) -> float:
    """Return the `daily_<provider>_usd` or `monthly_<provider>_usd` cap (0 means none)."""
    return float(limits.get(f"{window}_{provider}_usd") or 0.0)


async def enforce_spend_limit(
    *,
    projected_cost_usd: float = 0.0,
    provider: str | None = None,
    api_key_id: str | None = None,
) -> None:
    """Raise `SpendLimitExceeded` if this call would breach a daily, monthly or weekly budget.

    Pass `provider` ("openai", "cohere" or "anthropic") to also enforce that provider's daily and
    monthly caps; the provider's active key is then checked against its weekly cap too, unless
    `api_key_id` is given explicitly. `projected_cost_usd=0.0` is fine: the check also covers what
    has already been spent, which makes it a useful guard before a batch run.
    """
    limits = get_limits()
    if not limits.get("block_when_over", True):
        return
    summary = await spend_summary()
    daily = float(limits.get("daily_usd") or 0)
    monthly = float(limits.get("monthly_usd") or 0)

    if daily > 0 and summary["today_usd"] + projected_cost_usd > daily:
        raise SpendLimitExceeded(
            f"daily spend ${summary['today_usd']:.4f} + "
            f"${projected_cost_usd:.4f} > ${daily:.2f} limit — clear via "
            f"/admin/api/limits or unset block_when_over"
        )
    if monthly > 0 and summary["month_usd"] + projected_cost_usd > monthly:
        raise SpendLimitExceeded(
            f"monthly spend ${summary['month_usd']:.4f} + "
            f"${projected_cost_usd:.4f} > ${monthly:.2f} limit"
        )

    if provider:
        provider_today = next(
            (
                row["cost_usd"]
                for row in summary["today_by_provider"]
                if row["provider"] == provider
            ),
            0.0,
        )
        provider_month = next(
            (
                row["cost_usd"]
                for row in summary["month_by_provider"]
                if row["provider"] == provider
            ),
            0.0,
        )
        pcap_d = _provider_cap(limits, provider, "daily")
        pcap_m = _provider_cap(limits, provider, "monthly")
        if pcap_d > 0 and provider_today + projected_cost_usd > pcap_d:
            raise SpendLimitExceeded(
                f"{provider} daily spend ${provider_today:.4f} + "
                f"${projected_cost_usd:.4f} > ${pcap_d:.2f} provider cap"
            )
        if pcap_m > 0 and provider_month + projected_cost_usd > pcap_m:
            raise SpendLimitExceeded(
                f"{provider} monthly spend ${provider_month:.4f} + "
                f"${projected_cost_usd:.4f} > ${pcap_m:.2f} provider cap"
            )

    # Per-key weekly cap. Without an explicit key, use the provider's active key, so every caller
    # that passes a provider gets weekly enforcement automatically.
    kid = api_key_id or (keys.active_key_id(provider) if provider else None)
    if kid:
        await _enforce_weekly_key_cap(kid, projected_cost_usd)


# ── Weekly per-key budget caps (UTC ISO week, from Monday 00:00) ──────────────────────────────────
def _week_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=midnight.weekday())  # Monday of the current ISO week


def _cap_tier(pct: float) -> str:
    """Map weekly spend as a percentage of cap to a tier.

    Below 80 is ok, 80 to 99 warn, 100 to 124 soft (blocked), and 125 or more hard (blocked plus the
    kill switch).
    """
    if pct >= 125:
        return "hard"
    if pct >= 100:
        return "soft"
    if pct >= 80:
        return "warn"
    return "ok"


async def _weekly_spent_by_key() -> dict[str, float]:
    """Return {key_id: USD spent since Monday 00:00 UTC}."""
    monday = _week_start()
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ApiCall.api_key_id, func.coalesce(func.sum(ApiCall.cost_usd), 0.0))
                .where(ApiCall.ts >= monday)
                .group_by(ApiCall.api_key_id)
            )
        ).all()
    return {(k or "unknown"): float(c or 0.0) for k, c in rows}


async def weekly_summary() -> dict[str, Any]:
    """Return per-key weekly spend against cap, for the budget grid.

    Includes every key with a cap or any spend, plus the registered keys, so a configured but
    unused key still gets a budget line.
    """
    caps: dict[str, Any] = get_limits().get("weekly_key_caps_usd") or {}
    spent = await _weekly_spent_by_key()
    registry = keys.get_registry()
    all_ids = set(spent) | {k for k in caps} | set(registry)
    out = []
    for kid in sorted(all_ids):
        cap = float(caps.get(kid) or 0.0)
        s = float(spent.get(kid, 0.0))
        pct = (100.0 * s / cap) if cap > 0 else 0.0
        out.append(
            {
                "key_id": kid,
                "label": keys.label_for(kid),
                "provider": (registry.get(kid) or {}).get("provider") or kid.split(":", 1)[0],
                "spent_usd": round(s, 4),
                "cap_usd": cap,
                "pct": round(pct, 1),
                "tier": _cap_tier(pct) if cap > 0 else "uncapped",
                "legacy": kid.startswith("legacy:"),
            }
        )
    out.sort(key=lambda r: (-(r["pct"]), -r["spent_usd"]))
    return {"week_start": _week_start().isoformat(), "keys": out}


async def _enforce_weekly_key_cap(api_key_id: str, projected_cost_usd: float) -> None:
    """Enforce a key's weekly cap.

    From 80 to 99% of the cap it emits a warning and lets the call proceed; from 100 to 124% it
    blocks the call; at 125% or more it blocks and also engages the kill switch. A soft block
    affects only that key; other keys keep working.
    """
    caps: dict[str, Any] = get_limits().get("weekly_key_caps_usd") or {}
    cap = float(caps.get(api_key_id) or 0.0)
    if cap <= 0:
        return
    spent = (await _weekly_spent_by_key()).get(api_key_id, 0.0)
    projected = spent + projected_cost_usd
    pct = 100.0 * projected / cap
    tier = _cap_tier(pct)
    if tier == "warn":
        await emit(
            "budget.key.warn",
            module="polarius",
            level="warn",
            key_id=api_key_id,
            pct=pct,
            cap=cap,
            spent=projected,
        )
        return
    if tier in ("soft", "hard"):
        await emit(
            "limits.spend.exceeded",
            module="polarius",
            level="error",
            key_id=api_key_id,
            spent=projected,
            cap=cap,
            tier=tier,
        )
        if tier == "hard":
            killswitch.engage(f"weekly budget for {api_key_id} ≥125% of cap")
            await emit(
                "killswitch.engaged",
                module="polarius",
                level="error",
                reason=f"weekly budget runaway on {api_key_id}",
            )
        raise SpendLimitExceeded(
            f"weekly budget for {api_key_id} ${projected:.4f} > ${cap:.2f} ({tier}-block)"
        )


# ============================================================================
# Per-org quotas: atomic reservation, -1 for unlimited, 429 before any spend
# ============================================================================
# The commercial quota code, separate from the operator-wide spend caps above. It
# enforces the per-org tier limits resolved by entitlements (queries_per_day,
# analyses_per_month). Quotas follow the `entitlements_enabled` flag; there is no
# separate quota flag, since quotas mean nothing without tier limits. With the flag
# off, nothing calls these functions.
#
# Invariants:
# * 429 before spend: check_and_reserve_quota raises QuotaExceeded (429) before any
#   LLM call. Callers reserve first, then spend; a refused or failed run refunds.
# * Atomic: the reservation is a single INSERT ... ON CONFLICT ... DO UPDATE ...
#   RETURNING, followed by a check and, if over, a refund. Under N parallel
#   reservations at limit K exactly K succeed, with no read-modify-write race.
# * -1 means unlimited: the reservation still increments (for usage reporting) but
#   never raises.
# * org_id is never null (usage_counter.org_id and billing_event.org_id are NOT
#   NULL); callers always pass the real org.
# * Going over quota emits one `quota.exceeded` billing event before raising, so the
#   status code and the billing signal always go together.

# Which UTC period each metric is counted over. 'queries' and 'api_calls' are daily;
# 'analyses' and 'analyses_L4' are monthly. Unknown metrics default to 'month', the
# larger and therefore more conservative bucket.
_METRIC_PERIOD_KIND: dict[str, str] = {
    "queries": "day",
    "analyses": "month",
    "analyses_L4": "month",
    "api_calls": "day",
}


class QuotaExceeded(RuntimeError):
    """This request would exceed a per-org tier quota.

    Carries the machine-readable code and the HTTP status, so a call site (the analyze path, the
    Conduit precheck) can answer 429 with a structured body rather than dropping the request or
    failing with a 500. The reservation that triggered it has already been refunded, so the
    counter reflects only admitted requests.
    """

    status_code = 429
    code = "quota.exceeded"

    def __init__(self, metric: str, limit: int, *, count: int | None = None) -> None:
        self.metric = metric
        self.limit = limit
        self.count = count
        super().__init__(
            f"quota exceeded for {metric!r}: limit={limit}"
            + (f" count={count}" if count is not None else "")
        )


def _period_kind(metric: str) -> str:
    return _METRIC_PERIOD_KIND.get(metric, "month")


def period_start(metric: str, now: datetime | None = None) -> date:
    """Return the start date of ``metric``'s current UTC period at ``now``.

    Daily metrics start today (UTC); monthly metrics start on the first of the UTC month.
    Returned as a ``date`` to match ``usage_counter.period_start``.
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    today = now.astimezone(UTC).date()
    if _period_kind(metric) == "month":
        return today.replace(day=1)
    return today


async def check_and_reserve_quota(
    session: AsyncSession,
    org_id: UUID,
    metric: str,
    *,
    limit: int,
    now: datetime | None = None,
) -> int:
    """Atomically reserve one unit of ``metric`` quota for ``org_id``, before any spend.

    Reserves with an atomic increment, checks the returned count against ``limit``, and refunds
    and raises if over. This is correct under concurrency: of N parallel reservations at limit K,
    exactly K succeed and the final count is K.

    * ``limit == -1`` means unlimited: the counter is incremented (for usage reporting) and the
      new count returned; it never raises.
    * If the count exceeds ``limit``, the increment is refunded, one ``quota.exceeded`` billing
      event and one ``tier.quota_exceeded`` observability event are emitted, and
      ``QuotaExceeded`` (429) is raised before any LLM spend.

    Returns the reserved count on success. ``org_id`` must be the principal's real org (NOT
    NULL). The caller owns the surrounding transaction.
    """
    pk = _period_kind(metric)
    ps = period_start(metric, now)

    # Atomic reservation: insert or increment, returning the new count.
    reserved = (
        await session.execute(
            text(
                """
                INSERT INTO usage_counter
                    (org_id, metric, period_kind, period_start, count, cost_usd)
                VALUES (:org, :metric, :pk, :ps, 1, 0)
                ON CONFLICT (org_id, metric, period_kind, period_start)
                DO UPDATE SET count = usage_counter.count + 1
                RETURNING count
                """
            ),
            {"org": str(org_id), "metric": metric, "pk": pk, "ps": ps},
        )
    ).scalar_one()

    # -1 means unlimited: the reservation stands (usage is still counted) and never raises.
    if limit == -1:
        return int(reserved)

    if reserved > limit:
        # Over quota: refund the increment so the counter reflects only admitted requests, then
        # emit the billing event and raise 429.
        await session.execute(
            text(
                """
                UPDATE usage_counter
                   SET count = count - 1
                 WHERE org_id = :org AND metric = :metric
                   AND period_kind = :pk AND period_start = :ps
                """
            ),
            {"org": str(org_id), "metric": metric, "pk": pk, "ps": ps},
        )
        # One quota.exceeded billing event, so the status code and billing signal go together.
        from regsuper.billing.events import emit_billing_event

        await emit_billing_event(
            session,
            org_id,
            "quota.exceeded",
            payload={"metric": metric, "limit": limit, "count": int(reserved)},
        )
        await emit(
            "tier.quota_exceeded",
            module="polarius",
            level="warn",
            metric=metric,
            limit=limit,
            used=int(reserved),
        )
        raise QuotaExceeded(metric, limit, count=int(reserved))

    return int(reserved)


async def record_usage(
    session: AsyncSession,
    org_id: UUID,
    metric: str,
    *,
    cost_usd: float = 0.0,
    now: datetime | None = None,
) -> None:
    """Add ``cost_usd`` to the org's counter for ``metric``.

    The count was already incremented by ``check_and_reserve_quota``; this only accumulates
    spend, so the counter's cost tracks alongside ``analysis_run.cost_usd``. It never changes the
    count. If no counter row exists yet (cost recorded without a prior reservation, e.g. for an
    unmetered telemetry metric), the row is created with count 0.
    """
    if not cost_usd:
        return
    pk = _period_kind(metric)
    ps = period_start(metric, now)
    await session.execute(
        text(
            """
            INSERT INTO usage_counter
                (org_id, metric, period_kind, period_start, count, cost_usd)
            VALUES (:org, :metric, :pk, :ps, 0, :cost)
            ON CONFLICT (org_id, metric, period_kind, period_start)
            DO UPDATE SET cost_usd = usage_counter.cost_usd + EXCLUDED.cost_usd
            """
        ),
        {"org": str(org_id), "metric": metric, "pk": pk, "ps": ps, "cost": cost_usd},
    )


async def refund_quota(
    session: AsyncSession,
    org_id: UUID,
    metric: str,
    *,
    now: datetime | None = None,
) -> None:
    """Give back one reserved unit of ``metric`` after a refused or failed run.

    A refusal must not consume the user's quota, so the caller refunds the unit it reserved. The
    count never goes below zero, so a double refund is harmless.
    """
    pk = _period_kind(metric)
    ps = period_start(metric, now)
    await session.execute(
        text(
            """
            UPDATE usage_counter
               SET count = GREATEST(count - 1, 0)
             WHERE org_id = :org AND metric = :metric
               AND period_kind = :pk AND period_start = :ps
            """
        ),
        {"org": str(org_id), "metric": metric, "pk": pk, "ps": ps},
    )


async def org_usage_summary(
    session: AsyncSession,
    org_id: UUID,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the org's current usage per metric, for the frontend and billing views.

    Returns ``{metric: {count, cost_usd, period_kind, period_start}}`` for the current period of
    each metric the org has a counter for. Limits and remaining allowance are deliberately not
    computed here (the caller combines this with the resolved entitlement), so this function
    knows nothing about entitlements and only reads the org's own rows.
    """
    now = now or datetime.now(UTC)
    rows = (
        await session.execute(
            text(
                """
                SELECT metric, period_kind, period_start, count, cost_usd
                  FROM usage_counter
                 WHERE org_id = :org
                """
            ),
            {"org": str(org_id)},
        )
    ).all()
    out: dict[str, Any] = {}
    for metric, pk, ps, count, cost in rows:
        # Only the current period for each metric; skip earlier ones.
        if ps != period_start(metric, now):
            continue
        out[metric] = {
            "count": int(count or 0),
            "cost_usd": float(cost or 0.0),
            "period_kind": pk,
            "period_start": ps.isoformat() if hasattr(ps, "isoformat") else str(ps),
        }
    return out
