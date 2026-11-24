"""Cascade applicability floor — request-time accessor (WP-031 · A4-09 · ISSUE-10).

Pins `calibration/runtime.get_cascade_refusal_floor`, the read side of the
retrieval-threshold-vs-cascade-floor DECOUPLING. The cascade's applicability gate
reads its OWN calibrated key `cascade_applicability_floor` — NOT the retrieval
`refusal_threshold` — falling back to the documented 0.5 placeholder
(`settings.cascade_refusal_floor`) until a producer publishes the distinct key.

SAFETY contract under test: the floor must NEVER disable refusal, so an
out-of-bounds persisted value is clamped to the 0.5 fallback rather than trusted.
The accessor must also leave `get_refusal_threshold` BYTE-UNTOUCHED — the two keys
are independent.

Pure unit tests — no DB, no network. `data_dir` is redirected to a tmp path via
the settings cache so the JSON file lands in a sandbox (mirrors
test_refusal_calibration.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from regsuper.calibration import runtime
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


def _write(payload: dict, _dir: Path) -> None:
    (_dir / runtime.CALIBRATION_FILENAME).write_text(json.dumps(payload), encoding="utf-8")


def test_fallback_is_half_when_no_file():
    # No calibration file -> documented 0.5 placeholder from config, tagged.
    floor = runtime.get_cascade_refusal_floor()
    assert floor.source == "fallback"
    assert floor.value == pytest.approx(0.5)
    assert floor.value == pytest.approx(get_settings().cascade_refusal_floor)


def test_reads_the_distinct_key_when_present(_tmp_data_dir: Path):
    # A published distinct key is read as 'calibrated'.
    _write(
        {
            "refusal_threshold": 0.6,
            "cascade_applicability_floor": 0.42,
            "n_labeled": 42,
            "cascade_floor_precision": 0.96,
            "cascade_floor_recall": 0.71,
        },
        _tmp_data_dir,
    )
    floor = runtime.get_cascade_refusal_floor()
    assert floor.source == "calibrated"
    assert floor.value == pytest.approx(0.42)
    assert floor.n_labeled == 42
    assert floor.chosen_precision == pytest.approx(0.96)
    assert floor.chosen_recall == pytest.approx(0.71)


def test_floor_reads_distinct_key_not_refusal_threshold(_tmp_data_dir: Path):
    # The retrieval refusal_threshold is present but NO cascade key -> the floor
    # must NOT borrow refusal_threshold; it falls back to 0.5 (decoupling).
    _write({"refusal_threshold": 0.6, "n_labeled": 42}, _tmp_data_dir)
    floor = runtime.get_cascade_refusal_floor()
    assert floor.source == "fallback"
    assert floor.value == pytest.approx(0.5)


def test_out_of_range_floor_clamps_to_half(_tmp_data_dir: Path):
    # A hand-edited/corrupt floor of 9.9 (or 0.0) would mis-gate the cascade — it
    # must be rejected at read time in favour of the safe 0.5 fallback, never
    # trusted. Refusal is never silently disabled.
    _write({"cascade_applicability_floor": 9.9}, _tmp_data_dir)
    floor = runtime.get_cascade_refusal_floor()
    assert floor.source == "fallback"
    assert floor.value == pytest.approx(0.5)


def test_corrupt_file_degrades_to_fallback(_tmp_data_dir: Path):
    (_tmp_data_dir / runtime.CALIBRATION_FILENAME).write_text("{ not json", encoding="utf-8")
    floor = runtime.get_cascade_refusal_floor()
    assert floor.source == "fallback"
    assert floor.value == pytest.approx(0.5)


def test_refusal_threshold_accessor_is_unchanged(_tmp_data_dir: Path):
    # Adding the cascade key must not perturb the retrieval refusal accessor: with
    # only the cascade key present, refusal_threshold still resolves to its 0.6
    # fallback (it does not read the floor).
    _write({"cascade_applicability_floor": 0.42}, _tmp_data_dir)
    rt = runtime.get_refusal_threshold()
    assert rt.source == "fallback"
    assert rt.value == pytest.approx(get_settings().refusal_threshold)
