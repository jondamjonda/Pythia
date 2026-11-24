"""Cascade applicability floor — producer (WP-031b · A4-10 · ISSUE-10).

Pins the WRITE side: `threshold.fit_threshold` ALSO fits a distinct
`cascade_floor` from the labelled retrieval_eval rows (reusing the same precision
sweep), and `runtime.save_calibrated_threshold` persists it under the distinct key
`cascade_applicability_floor`. The A4-09 accessor must then round-trip it (the key
shapes match across producer and consumer).

Two invariants the floor must NOT break:
  * `refusal_threshold` production stays BYTE-IDENTICAL — adding the floor must not
    perturb the retrieval gate's persisted keys/values.
  * insufficient labelled data -> NO floor is written (the accessor stays on its
    documented 0.5 fallback; no bogus floor).

Pure unit tests — no DB, no network. `fit_threshold` only touches
.rerank_score/.label/.label_kind, so we drive it with lightweight stand-ins
(mirrors test_calibration_threshold.py). `data_dir` is redirected to a tmp path
via the settings cache (mirrors test_refusal_calibration.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from regsuper.calibration import runtime
from regsuper.calibration.threshold import fit_threshold
from regsuper.config import get_settings


@dataclass
class _Row:
    """Stand-in for storage.models.RetrievalEval — fit_threshold only touches these
    three attrs, so we avoid constructing a real SQLAlchemy row (and any DB)."""

    rerank_score: float | None = None
    label: str | None = None
    label_kind: str | None = None


def _positive(score: float) -> _Row:
    return _Row(rerank_score=score, label="relevant", label_kind="relevant")


def _negative(score: float) -> _Row:
    return _Row(rerank_score=score, label="not_relevant", label_kind="not_relevant")


def _separable_rows() -> list[_Row]:
    """A cleanly separable corpus that yields a usable threshold (>=5 positives
    above a precision-1.0 cut), so a floor IS fitted."""
    rows = [_negative(0.10 + 0.01 * i) for i in range(6)]  # 6 negatives in [0.10, 0.15]
    rows += [_positive(0.80 + 0.01 * i) for i in range(6)]  # 6 positives in [0.80, 0.85]
    return rows


@pytest.fixture(autouse=True)
def _tmp_data_dir(tmp_path: Path):
    """Point data_dir at a tmp dir for the whole test (lru_cached settings; mutate,
    don't cache_clear — see test_refusal_calibration.py)."""
    s = get_settings()
    original = s.data_dir
    s.data_dir = tmp_path
    try:
        yield tmp_path
    finally:
        s.data_dir = original


# --------------------------------------------------------------------------- fit produces a floor
def test_fit_produces_cascade_floor_from_same_sweep():
    report = fit_threshold(_separable_rows(), target_precision=0.95)
    assert report.chosen_threshold is not None
    # The floor is fitted from the SAME sweep -> tracks the chosen retrieval cut.
    assert report.cascade_floor == pytest.approx(report.chosen_threshold)
    assert report.cascade_floor_precision == pytest.approx(report.chosen_precision)
    assert report.cascade_floor_recall == pytest.approx(report.chosen_recall)


def test_save_writes_distinct_cascade_key(_tmp_data_dir: Path):
    report = fit_threshold(_separable_rows(), target_precision=0.95)
    runtime.save_calibrated_threshold(report)
    raw = json.loads((_tmp_data_dir / runtime.CALIBRATION_FILENAME).read_text(encoding="utf-8"))
    # Distinct key written, separate from refusal_threshold.
    assert "cascade_applicability_floor" in raw
    assert raw["cascade_applicability_floor"] == pytest.approx(report.cascade_floor)
    assert raw["cascade_applicability_floor"] != "refusal_threshold"


def test_a4_09_accessor_round_trips_the_floor(_tmp_data_dir: Path):
    # The producer writes; the A4-09 accessor reads the SAME distinct key back —
    # proving the key shapes match across producer and consumer.
    report = fit_threshold(_separable_rows(), target_precision=0.95)
    runtime.save_calibrated_threshold(report)
    floor = runtime.get_cascade_refusal_floor()
    assert floor.source == "calibrated"
    assert floor.value == pytest.approx(report.cascade_floor)


def test_refusal_threshold_production_unchanged_by_floor(_tmp_data_dir: Path):
    # Adding the floor must NOT perturb the retrieval refusal gate: the persisted
    # refusal_threshold + its provenance keys match the report exactly, and the
    # A4-09 retrieval accessor still resolves the same calibrated value.
    report = fit_threshold(_separable_rows(), target_precision=0.95)
    runtime.save_calibrated_threshold(report)
    raw = json.loads((_tmp_data_dir / runtime.CALIBRATION_FILENAME).read_text(encoding="utf-8"))
    assert raw["refusal_threshold"] == pytest.approx(report.chosen_threshold)
    assert raw["chosen_precision"] == pytest.approx(report.chosen_precision)
    assert raw["chosen_recall"] == pytest.approx(report.chosen_recall)
    rt = runtime.get_refusal_threshold()
    assert rt.source == "calibrated"
    assert rt.value == pytest.approx(report.chosen_threshold)


# --------------------------------------------------------------------------- insufficient data
def test_insufficient_data_writes_no_floor(_tmp_data_dir: Path):
    # Only 4 positives -> the support floor (n_above >= 5) is never cleared, so the
    # retrieval threshold AND the cascade floor are both None. A refusal_threshold
    # report with no threshold raises (cannot publish); we instead assert the floor
    # is absent from the report and that the accessor stays on its 0.5 fallback.
    rows = [_positive(0.90), _positive(0.91), _positive(0.92), _positive(0.93)]
    report = fit_threshold(rows, target_precision=0.95)
    assert report.chosen_threshold is None
    assert report.cascade_floor is None
    # Nothing was published, so the accessor degrades to the documented fallback.
    floor = runtime.get_cascade_refusal_floor()
    assert floor.source == "fallback"
    assert floor.value == pytest.approx(0.5)


def test_floor_absent_from_file_when_report_has_none(_tmp_data_dir: Path):
    # Construct a report that HAS a refusal threshold but NO cascade floor (the
    # degenerate all-identical-scores path leaves cascade_floor None). save must
    # write refusal_threshold but omit the cascade key entirely — no bogus floor.
    report = fit_threshold([_positive(0.5), _positive(0.5), _negative(0.5)])
    assert report.chosen_threshold is not None  # degenerate path picks the single value
    assert report.cascade_floor is None  # but no floor was fitted
    runtime.save_calibrated_threshold(report)
    raw = json.loads((_tmp_data_dir / runtime.CALIBRATION_FILENAME).read_text(encoding="utf-8"))
    assert "refusal_threshold" in raw
    assert "cascade_applicability_floor" not in raw
    # Accessor stays on the safe 0.5 fallback rather than borrowing the threshold.
    floor = runtime.get_cascade_refusal_floor()
    assert floor.source == "fallback"
    assert floor.value == pytest.approx(0.5)
