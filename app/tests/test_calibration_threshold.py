"""Calibration refusal-threshold math (regsuper/calibration/threshold.py).

Why trust-critical: the chosen `refusal_threshold` is the line below which the system REFUSES to answer.
Plan §12.7 optimises precision over recall: "when the system DOES return an answer, ≥95% of those
answers should be correct." If this math is wrong, the system either answers when it should refuse
(false positives a lawyer would have to catch) or refuses everything (useless). `fit_threshold` is a
pure function over a list of eval rows — it reads only `.label_kind`, `.label`, `.rerank_score`, so we
drive it with lightweight stand-ins and never touch the DB.

The contract under test:
  * positives = label_kind in {relevant, partial}  OR  label == 'relevant'
  * eligible  = has one of the above labels (or 'not_relevant') AND rerank_score is not None
  * chosen    = the SMALLEST threshold t whose precision among {rows with score >= t} reaches
                target_precision, AND at least 5 rows are above t (the support floor)
"""

from __future__ import annotations

from dataclasses import dataclass

from regsuper.calibration.threshold import fit_threshold


@dataclass
class _Row:
    """Stand-in for storage.models.RetrievalEval — fit_threshold only touches these three attrs,
    so we avoid constructing a real SQLAlchemy row (and any DB)."""

    rerank_score: float | None = None
    label: str | None = None
    label_kind: str | None = None


def _positive(score: float) -> _Row:
    return _Row(rerank_score=score, label="relevant", label_kind="relevant")


def _negative(score: float) -> _Row:
    return _Row(rerank_score=score, label="not_relevant", label_kind="not_relevant")


# --------------------------------------------------------------------------- empty / degenerate
def test_no_labeled_rows_yields_no_threshold():
    report = fit_threshold([])
    assert report.chosen_threshold is None
    assert report.n_labeled == 0
    assert "nothing to fit" in report.reason


def test_rows_without_rerank_score_are_ineligible():
    rows = [
        _Row(rerank_score=None, label="relevant"),
        _Row(rerank_score=None, label="not_relevant"),
    ]
    report = fit_threshold(rows)
    assert report.n_labeled == 0
    assert report.chosen_threshold is None


def test_all_identical_scores_defaults_to_that_value():
    rows = [_positive(0.5), _positive(0.5), _negative(0.5)]
    report = fit_threshold(rows)
    assert report.chosen_threshold == 0.5
    assert report.chosen_recall == 1.0
    # precision at the single value = relevant / all = 2/3
    assert abs(report.chosen_precision - (2 / 3)) < 1e-6
    assert "identical" in report.reason


# --------------------------------------------------------------------------- the precision sweep
def test_clean_separation_picks_threshold_above_the_negatives():
    """Negatives cluster low, positives cluster high, fully separable. The chosen threshold must sit
    above the negatives so precision among accepted rows is 100% (>= 95%)."""
    rows = [_negative(0.10 + 0.01 * i) for i in range(6)]  # 6 negatives in [0.10, 0.15]
    rows += [_positive(0.80 + 0.01 * i) for i in range(6)]  # 6 positives in [0.80, 0.85]
    report = fit_threshold(rows, target_precision=0.95)
    assert report.chosen_threshold is not None
    # Must exclude every negative → threshold strictly above the top negative (0.15)...
    assert report.chosen_threshold > 0.15
    # ...and no higher than needed to keep >=5 positives above it (support floor = 5 of 6).
    assert report.chosen_threshold <= 0.81
    assert report.chosen_precision == 1.0


def test_support_floor_of_five_is_enforced():
    """Precision can be a perfect 1.0 with only a handful of rows above t, but the fitter requires
    n_above >= 5 before it will trust a threshold. With only 4 positives and no negatives, NO point
    clears the floor → no threshold chosen."""
    rows = [_positive(0.90), _positive(0.91), _positive(0.92), _positive(0.93)]  # 4 only
    report = fit_threshold(rows, target_precision=0.95)
    assert report.chosen_threshold is None
    assert "at least 5" in report.reason


def test_target_precision_unreachable_returns_none():
    """A corpus that's all noise: positives and negatives fully interleaved so no cut reaches 95%
    precision. The fitter must REFUSE to pick a threshold rather than emit a false-confidence number."""
    rows = []
    for i in range(10):
        rows.append(_positive(0.50 + 0.001 * i))
        rows.append(_negative(0.50 + 0.001 * i))  # one negative shadowing each positive
    report = fit_threshold(rows, target_precision=0.95)
    assert report.chosen_threshold is None
    assert "95% precision" in report.reason


def test_lower_target_precision_is_satisfiable_where_higher_is_not():
    """Same interleaved corpus: at 50% precision a threshold exists (half the accepted rows are
    positive), proving the target_precision knob actually binds."""
    rows = []
    for i in range(10):
        rows.append(_positive(0.50 + 0.001 * i))
        rows.append(_negative(0.50 + 0.001 * i))
    report = fit_threshold(rows, target_precision=0.50)
    assert report.chosen_threshold is not None
    assert report.chosen_precision >= 0.50


def test_picks_the_LOWEST_qualifying_threshold():
    """Among all thresholds that hit the precision floor, the fitter must pick the SMALLEST one
    (maximising recall / answer coverage). With clean separation, the chosen point's recall should be
    the full positive set, not a stricter subset."""
    rows = [_negative(0.10), _negative(0.11), _negative(0.12), _negative(0.13), _negative(0.14)]
    rows += [_positive(0.70 + 0.02 * i) for i in range(8)]  # 8 positives spread 0.70..0.84
    report = fit_threshold(rows, target_precision=0.95)
    assert report.chosen_threshold is not None
    # Lowest qualifying t keeps ALL 8 positives above it → recall 1.0.
    assert report.chosen_recall == 1.0
    assert report.chosen_precision == 1.0


def test_partial_label_counts_as_positive():
    """label_kind='partial' is a positive for precision math (it belongs in the answer set). A run of
    'partial' rows above a clean negative band must produce a usable threshold with precision 1.0."""
    rows = [_negative(0.10 + 0.01 * i) for i in range(5)]
    rows += [_Row(rerank_score=0.80 + 0.01 * i, label_kind="partial") for i in range(6)]
    report = fit_threshold(rows, target_precision=0.95)
    assert report.chosen_threshold is not None
    assert report.chosen_threshold > 0.14  # above the negatives
    assert report.chosen_precision == 1.0
    assert report.n_relevant == 6  # all 6 partials counted as positive


def test_curve_is_monotone_in_n_above():
    """Structural sanity on the returned curve: as the threshold rises, the number of rows above it
    is non-increasing. A regression that double-counts or mis-sorts would break this."""
    rows = [_negative(0.10 + 0.05 * i) for i in range(5)]
    rows += [_positive(0.60 + 0.05 * i) for i in range(5)]
    report = fit_threshold(rows)
    n_above_seq = [p.n_above for p in report.curve]
    assert n_above_seq == sorted(n_above_seq, reverse=True)
    # report tallies match the inputs
    assert report.n_relevant == 5
    assert report.n_not_relevant == 5
    assert report.n_labeled == 10
