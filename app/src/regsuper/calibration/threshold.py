"""Fit the refusal threshold from labelled retrieval evaluations.

The labelled `retrieval_eval` rows give a rerank-score distribution for relevant and
not-relevant provisions. The operating threshold is the smallest rerank score t such that, among
all rows scoring at least t, at least 95% are labelled relevant.

In compliance a false positive (the system claims a rule applies when it does not) costs much
more than a false negative (the system refuses something it could have answered). 95% precision
means that when the system does return an answer, at least 95% of those answers are correct.

The result is one numeric `refusal_threshold` for the orchestrator's refusal check, plus the
precision/recall curve for the dashboard and the Streamlit app to plot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from regsuper.storage.database import session_scope
from regsuper.storage.models import RetrievalEval


@dataclass(frozen=True)
class ThresholdPoint:
    threshold: float
    n_above: int
    n_relevant_above: int
    precision: float
    recall: float


@dataclass
class ThresholdReport:
    n_labeled: int
    n_relevant: int
    n_not_relevant: int
    curve: list[ThresholdPoint]
    chosen_threshold: float | None
    chosen_precision: float | None
    chosen_recall: float | None
    reason: str  # human-readable note if no threshold could be picked
    # The cascade applicability floor is a separate calibrated value from `chosen_threshold`
    # (the retrieval refusal gate). It comes from the same precision sweep but is published
    # under its own key, so the two can be re-fit independently. None when the labelled data
    # is insufficient; the runtime accessor then stays on its documented 0.5 fallback.
    cascade_floor: float | None = None
    cascade_floor_precision: float | None = None
    cascade_floor_recall: float | None = None


def _eligible(rows: list[RetrievalEval]) -> list[RetrievalEval]:
    """Return the rows usable for fitting: those with both a rerank_score and a label.

    Both `relevant` and `partial` (from the three-state `label_kind` schema) count as positive
    for the precision maths, because both mean "this provision belongs in the answer set". The
    lawyer cares about the answer set being correct, not about the difference between primary
    and supporting citations.
    """
    return [
        r
        for r in rows
        if (
            (
                r.label_kind in ("relevant", "partial", "not_relevant")
                or r.label in ("relevant", "not_relevant")
            )
            and r.rerank_score is not None
        )
    ]


def _is_positive(r: RetrievalEval) -> bool:
    """True when the lawyer judged this row as belonging to the answer."""
    if r.label_kind in ("relevant", "partial"):
        return True
    if r.label == "relevant":
        return True
    return False


def fit_threshold(
    rows: list[RetrievalEval],
    *,
    target_precision: float = 0.95,
    n_steps: int = 200,
) -> ThresholdReport:
    """Find the smallest rerank_score `t` at which precision reaches `target_precision`.

    Precision is measured over the rows with rerank_score >= t, and a point qualifies only with
    at least 5 such rows. Recall is reported but is not a binding constraint: precision is
    deliberately preferred, because recall is recoverable through human review.
    """
    eligible = _eligible(rows)
    n_relevant = sum(1 for r in eligible if _is_positive(r))
    n_not_relevant = sum(1 for r in eligible if not _is_positive(r))

    if not eligible:
        return ThresholdReport(
            n_labeled=0,
            n_relevant=0,
            n_not_relevant=0,
            curve=[],
            chosen_threshold=None,
            chosen_precision=None,
            chosen_recall=None,
            reason="no labeled rows with rerank_score yet — nothing to fit",
        )

    scores = sorted({float(r.rerank_score) for r in eligible})
    lo, hi = scores[0], scores[-1]
    if lo == hi:
        # Degenerate case: all scores are identical.
        return ThresholdReport(
            n_labeled=len(eligible),
            n_relevant=n_relevant,
            n_not_relevant=n_not_relevant,
            curve=[],
            chosen_threshold=lo,
            chosen_precision=n_relevant / len(eligible) if eligible else 0.0,
            chosen_recall=1.0,
            reason="all rerank_scores identical — defaulting to the single value",
        )

    step = (hi - lo) / max(1, n_steps)
    curve: list[ThresholdPoint] = []
    chosen: ThresholdPoint | None = None

    # Sweep upwards; `chosen` is the first point that meets the target precision, i.e. the
    # lowest threshold that satisfies it.
    t = lo
    while t <= hi + 1e-9:
        above = [r for r in eligible if (r.rerank_score or 0.0) >= t]
        n_above = len(above)
        n_rel_above = sum(1 for r in above if _is_positive(r))
        if n_above == 0:
            precision = 1.0
            recall = 0.0
        else:
            precision = n_rel_above / n_above
            recall = n_rel_above / max(1, n_relevant)
        point = ThresholdPoint(
            threshold=round(t, 6),
            n_above=n_above,
            n_relevant_above=n_rel_above,
            precision=round(precision, 6),
            recall=round(recall, 6),
        )
        curve.append(point)
        if chosen is None and precision >= target_precision and n_above >= 5:
            chosen = point
        t += step

    if chosen is None:
        return ThresholdReport(
            n_labeled=len(eligible),
            n_relevant=n_relevant,
            n_not_relevant=n_not_relevant,
            curve=curve,
            chosen_threshold=None,
            chosen_precision=None,
            chosen_recall=None,
            reason=(
                f"no point reached {target_precision * 100:.0f}% precision with "
                f"at least 5 supporting rows; lower the bar or label more data"
            ),
        )
    # Derive the cascade applicability floor from the same precision sweep that produced
    # `chosen` (the retrieval refusal threshold). The two share the sweep but are published
    # under separate keys, so an independent re-fit of either does not silently move the
    # other. The floor is only set here, on the sufficient-data path; the early returns above
    # leave it None so no bogus floor is written.
    return ThresholdReport(
        n_labeled=len(eligible),
        n_relevant=n_relevant,
        n_not_relevant=n_not_relevant,
        curve=curve,
        chosen_threshold=chosen.threshold,
        chosen_precision=chosen.precision,
        chosen_recall=chosen.recall,
        reason=f"smallest t with precision >= {target_precision:.2f}",
        cascade_floor=chosen.threshold,
        cascade_floor_precision=chosen.precision,
        cascade_floor_recall=chosen.recall,
    )


async def load_labeled_evals() -> list[RetrievalEval]:
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(RetrievalEval).where(
                        RetrievalEval.label.in_(("relevant", "not_relevant"))
                    )
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


def report_to_dict(report: ThresholdReport) -> dict[str, Any]:
    """Convert a report to a JSON-ready dict for the admin endpoint and the Streamlit app."""
    return {
        "n_labeled": report.n_labeled,
        "n_relevant": report.n_relevant,
        "n_not_relevant": report.n_not_relevant,
        "chosen_threshold": report.chosen_threshold,
        "chosen_precision": report.chosen_precision,
        "chosen_recall": report.chosen_recall,
        "reason": report.reason,
        # The separate cascade applicability floor (None when labelled data is insufficient),
        # keyed apart from the refusal threshold above.
        "cascade_floor": report.cascade_floor,
        "cascade_floor_precision": report.cascade_floor_precision,
        "cascade_floor_recall": report.cascade_floor_recall,
        "curve": [
            {
                "threshold": p.threshold,
                "n_above": p.n_above,
                "n_relevant_above": p.n_relevant_above,
                "precision": p.precision,
                "recall": p.recall,
            }
            for p in report.curve
        ],
    }
