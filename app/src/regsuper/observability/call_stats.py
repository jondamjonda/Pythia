"""External call counts and citation health, for the control room's Polarius (app) panel.

* Citation health, headed by the cascade's grounded yield: of the findings an analysis cascade
  produced, the share whose citation checked out (verified or fuzzy) rather than looking suspect
  (not_found or empty). It measures not just that the cascade ran but that its output can be
  trusted.
* Call counts: how many external calls the app initiates, per provider and model, over a
  window, with errors and a per-minute rate. The Pythia engine is counted as a zero-cost
  provider, but only for the app's calls into it; the engine's own internal LLM spend is never
  added to these numbers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text

from regsuper.storage.database import session_scope
from regsuper.storage.models import ApiCall


async def citation_health() -> dict[str, Any]:
    """Summarise citation verdicts over all findings that have one, including the grounded yield."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT payload->>'citation_verdict' AS v, count(*) AS n "
                    "FROM finding WHERE payload ? 'citation_verdict' GROUP BY 1"
                )
            )
        ).all()
    counts = {(v or "unknown"): int(n) for v, n in rows}
    grounded = counts.get("verified", 0) + counts.get("fuzzy", 0)
    suspect = counts.get("not_found", 0) + counts.get("empty", 0)
    total = grounded + suspect
    yield_pct = (100.0 * grounded / total) if total else None
    unverified_rate = (100.0 * counts.get("not_found", 0) / total) if total else 0.0
    return {
        "by_verdict": counts,
        "total": total,
        "grounded": grounded,
        "cascade_grounded_yield_pct": round(yield_pct, 1) if yield_pct is not None else None,
        "unverified_citation_rate_pct": round(unverified_rate, 1),
    }


async def call_counts(window_seconds: int = 86_400) -> dict[str, Any]:
    """Return the volume of external calls the app made, per provider and model, in the window."""
    since = datetime.now(UTC) - timedelta(seconds=window_seconds)
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    ApiCall.provider,
                    ApiCall.model,
                    func.count(ApiCall.id),
                    func.coalesce(func.sum(ApiCall.cost_usd), 0.0),
                    func.coalesce(func.sum(ApiCall.latency_ms), 0),
                    func.count().filter(ApiCall.status != "ok"),
                )
                .where(ApiCall.ts >= since)
                .group_by(ApiCall.provider, ApiCall.model)
                .order_by(func.count(ApiCall.id).desc())
            )
        ).all()
    by_model = []
    by_provider: dict[str, dict[str, Any]] = {}
    total_calls = total_errors = 0
    total_cost = 0.0
    for prov, model, n, cost, lat, errs in rows:
        n, cost, errs = int(n), float(cost), int(errs)
        by_model.append(
            {
                "provider": prov,
                "model": model,
                "n": n,
                "cost_usd": round(cost, 4),
                "errors": errs,
                "avg_latency_ms": round(lat / n) if n else 0,
            }
        )
        p = by_provider.setdefault(prov, {"provider": prov, "n": 0, "cost_usd": 0.0, "errors": 0})
        p["n"] += n
        p["cost_usd"] = round(p["cost_usd"] + cost, 4)
        p["errors"] += errs
        total_calls += n
        total_errors += errs
        total_cost += cost
    minutes = max(window_seconds / 60.0, 1.0)
    return {
        "window_seconds": window_seconds,
        "total_calls": total_calls,
        "total_errors": total_errors,
        "total_cost_usd": round(total_cost, 4),
        "calls_per_min": round(total_calls / minutes, 2),
        "by_provider": sorted(by_provider.values(), key=lambda x: -x["n"]),
        "by_model": by_model,
        # The pythia provider counts only the app's calls into the engine. The engine's internal LLM
        # spend lives in its own database (shown on the Pythia panel) and is never added to these
        # counts.
        "note": "Pythia calls are boundary calls Polarius initiates — not summable with the sidecar's "
        "internal LLM spend (different layer).",
    }
