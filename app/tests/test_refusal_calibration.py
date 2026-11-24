"""Refusal-threshold calibration: persistence + request-time load + skip behaviour.

Pins the bridge that was missing — `fit_threshold` computes a value, `runtime`
persists it, and the orchestrator loads it at request time (not the frozen 0.6
placeholder). Also pins the under-refusal fix: when rerank is skipped we flag (or
refuse) instead of silently passing low-confidence retrieval through.

Pure unit tests — no DB, no network. `data_dir` is redirected to a tmp path via
the settings cache so the JSON file lands in a sandbox.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from regsuper.calibration import runtime
from regsuper.calibration.threshold import ThresholdReport
from regsuper.config import get_settings


@pytest.fixture(autouse=True)
def _tmp_data_dir(tmp_path: Path):
    """Point data_dir at a tmp dir for the whole test.

    `get_settings()` is lru_cached, so we mutate `data_dir` on the cached instance
    (NOT cache_clear() — that would rebuild Settings with the default ./data and
    leak the test JSON into the real project dir)."""
    s = get_settings()
    original = s.data_dir
    s.data_dir = tmp_path
    try:
        yield tmp_path
    finally:
        s.data_dir = original


def _report(threshold: float | None, *, reason: str = "ok") -> ThresholdReport:
    return ThresholdReport(
        n_labeled=42,
        n_relevant=30,
        n_not_relevant=12,
        curve=[],
        chosen_threshold=threshold,
        chosen_precision=0.96 if threshold is not None else None,
        chosen_recall=0.71 if threshold is not None else None,
        reason=reason,
    )


def test_fallback_when_no_file():
    # No calibration file -> documented 0.6 placeholder from config, tagged.
    rt = runtime.get_refusal_threshold()
    assert rt.source == "fallback"
    assert rt.value == pytest.approx(get_settings().refusal_threshold)


def test_save_then_load_returns_calibrated_value():
    runtime.save_calibrated_threshold(_report(0.42))
    rt = runtime.get_refusal_threshold()
    assert rt.source == "calibrated"
    assert rt.value == pytest.approx(0.42)
    assert rt.n_labeled == 42
    assert rt.chosen_precision == pytest.approx(0.96)


def test_save_refuses_none_threshold():
    with pytest.raises(ValueError):
        runtime.save_calibrated_threshold(_report(None, reason="not enough data"))


def test_save_refuses_out_of_range_threshold():
    with pytest.raises(ValueError):
        runtime.save_calibrated_threshold(_report(1.7))


def test_corrupt_file_degrades_to_fallback(_tmp_data_dir: Path):
    (_tmp_data_dir / runtime.CALIBRATION_FILENAME).write_text("{ not json", encoding="utf-8")
    rt = runtime.get_refusal_threshold()
    assert rt.source == "fallback"


def test_out_of_range_persisted_value_is_ignored(_tmp_data_dir: Path):
    # A hand-edited/corrupt value of 0.0 would silently DISABLE refusal — must be
    # rejected at read time in favour of the fallback, never trusted.
    (_tmp_data_dir / runtime.CALIBRATION_FILENAME).write_text(
        '{"refusal_threshold": 9.9}', encoding="utf-8"
    )
    rt = runtime.get_refusal_threshold()
    assert rt.source == "fallback"
