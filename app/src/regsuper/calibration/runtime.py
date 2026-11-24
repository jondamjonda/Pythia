"""Runtime application of the calibrated refusal threshold.

`calibration/threshold.py` fits the operating threshold from labelled `retrieval_eval` rows.
This module persists the fitted value to a small JSON file in `data_dir` (the runtime-mutable
config pattern also used by `observability/limits.py`) and loads it at request time. A file
rather than a config or env change, because `get_settings()` is frozen by `@lru_cache` at
process start, the operator must be able to publish a new threshold after a labelling round
without a redeploy, and the file survives restarts and can be audited on disk.

Read path (every request): `get_refusal_threshold()` returns the persisted value when present
and valid, else `settings.refusal_threshold` (the documented 0.6 placeholder), tagged with its
source so logs and the UI show which one is live. Write path: `save_calibrated_threshold()`.
Publish path: `python -m regsuper.calibration.runtime publish` loads the labelled rows, fits and
persists in one step. (The Streamlit labelling app only previews a fit; it does not persist.)

The threshold is a cross-encoder (Cohere rerank) score cutoff. Without `COHERE_API_KEY` the
rerank is skipped, every eval row has `rerank_score=None`, the fit produces no threshold and
saving raises, so a keyless process can neither refuse on a calibrated score nor publish one.
It stays on the 0.6 fallback, which `warn_if_fallback_threshold()` can report at boot.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from regsuper.config import get_settings

if TYPE_CHECKING:
    from regsuper.calibration.threshold import ThresholdReport


def _logger():
    """Return this module's structlog logger, importing logging only when something is logged.

    The read accessors need nothing beyond the settings, so a caller outside the app process
    (engine/scripts/corpus_depth_standard.py, whose environment has no structlog) can resolve
    the live floor by importing this module.
    """
    from regsuper.observability.logging_config import get_logger

    return get_logger(__name__)


CALIBRATION_FILENAME = "refusal_calibration.json"

# Hard bounds for a sane rerank-score threshold. Cohere rerank-v3.5 scores are in [0, 1];
# anything outside that range is corrupt and is ignored in favour of the fallback, rather than
# silently disabling refusal (a threshold below 0 never refuses) or refusing everything
# (a threshold above 1).
_MIN_VALID = 0.0
_MAX_VALID = 1.0


@dataclass(frozen=True)
class RefusalThreshold:
    """A resolved threshold and where it came from, for logging and the admin UI."""

    value: float
    source: Literal["calibrated", "fallback"]
    # Provenance (only populated for `calibrated`); None on the fallback path.
    fitted_at: str | None = None
    n_labeled: int | None = None
    chosen_precision: float | None = None
    chosen_recall: float | None = None


def calibration_path() -> Path:
    return get_settings().data_dir / CALIBRATION_FILENAME


def _read_file() -> dict[str, Any] | None:
    p = calibration_path()
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def get_refusal_threshold() -> RefusalThreshold:
    """Resolve the live refusal threshold at request time.

    Uses the persisted calibrated value when present and numerically valid, otherwise
    `settings.refusal_threshold` (the documented 0.6 placeholder).

    Never raises: a missing or corrupt file degrades to the fallback so retrieval keeps
    working, and the `source` field tells the caller which value is in effect.
    """
    raw = _read_file()
    if raw is not None:
        val = raw.get("refusal_threshold")
        if isinstance(val, (int, float)) and _MIN_VALID <= float(val) <= _MAX_VALID:
            return RefusalThreshold(
                value=float(val),
                source="calibrated",
                fitted_at=raw.get("fitted_at"),
                n_labeled=raw.get("n_labeled"),
                chosen_precision=raw.get("chosen_precision"),
                chosen_recall=raw.get("chosen_recall"),
            )
    # Documented fallback: the placeholder lives in config.py so a single edit
    # changes the floor used before any calibration has been published.
    return RefusalThreshold(
        value=float(get_settings().refusal_threshold),
        source="fallback",
    )


def get_cascade_refusal_floor() -> RefusalThreshold:
    """Resolve the live cascade-applicability refusal floor at request time.

    The cascade's applicability gate is a different decision from the retrieval refusal gate
    served by `get_refusal_threshold`, so it has its own calibrated key,
    `cascade_applicability_floor`, rather than reusing `refusal_threshold`. Keeping them
    separate lets each be re-fit and published without silently moving the other.

    Uses the persisted `cascade_applicability_floor` when present and numerically valid,
    otherwise `settings.cascade_refusal_floor` (the documented 0.5 fallback). Until a fit
    writes that key, the floor stays at 0.5.

    Safety: this floor must not disable refusal. A persisted value outside [0, 1] is treated as
    corrupt and replaced by the conservative 0.5 fallback instead of being trusted.

    Never raises: a missing or corrupt file degrades to the fallback so the cascade keeps
    working, and `source` tells the caller which value is in effect.
    """
    raw = _read_file()
    if raw is not None:
        val = raw.get("cascade_applicability_floor")
        if isinstance(val, (int, float)) and _MIN_VALID <= float(val) <= _MAX_VALID:
            return RefusalThreshold(
                value=float(val),
                source="calibrated",
                fitted_at=raw.get("fitted_at"),
                n_labeled=raw.get("n_labeled"),
                chosen_precision=raw.get("cascade_floor_precision"),
                chosen_recall=raw.get("cascade_floor_recall"),
            )
    # Documented fallback: 0.5 is the safe floor (see `cascade_refusal_floor` in config.py).
    # An out-of-range persisted value lands here too, so refusal is never silently disabled.
    return RefusalThreshold(
        value=float(get_settings().cascade_refusal_floor),
        source="fallback",
    )


def save_calibrated_threshold(report: ThresholdReport) -> RefusalThreshold:
    """Persist a fitted threshold so the next request loads it.

    Raises ValueError when the report has no usable threshold (e.g. not enough labelled
    data) or it is out of range, so a None is never published silently; the system then
    keeps using the documented fallback.
    """
    if report.chosen_threshold is None:
        raise ValueError(f"cannot publish threshold: fit produced none ({report.reason})")
    val = float(report.chosen_threshold)
    if not (_MIN_VALID <= val <= _MAX_VALID):
        raise ValueError(
            f"fitted threshold {val} outside valid range "
            f"[{_MIN_VALID}, {_MAX_VALID}] — refusing to publish"
        )
    record = {
        "refusal_threshold": val,
        "fitted_at": datetime.now(UTC).isoformat(),
        "n_labeled": report.n_labeled,
        "n_relevant": report.n_relevant,
        "n_not_relevant": report.n_not_relevant,
        "chosen_precision": report.chosen_precision,
        "chosen_recall": report.chosen_recall,
        "reason": report.reason,
    }
    # Also publish the cascade applicability floor under its own key
    # (`cascade_applicability_floor`, read by get_cascade_refusal_floor) when the fit produced
    # one. The retrieval keys above are written the same way whether or not a floor is present.
    # When the labelled data was insufficient, `report.cascade_floor` is None and no floor key
    # is written, so the accessor stays on its documented 0.5 fallback rather than trusting a
    # bogus value.
    if report.cascade_floor is not None and (
        _MIN_VALID <= float(report.cascade_floor) <= _MAX_VALID
    ):
        record["cascade_applicability_floor"] = float(report.cascade_floor)
        record["cascade_floor_precision"] = report.cascade_floor_precision
        record["cascade_floor_recall"] = report.cascade_floor_recall
    p = calibration_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(record, indent=2), encoding="utf-8")
    # Extension point: with an event bus, publishing a `refusal_threshold.updated` event here
    # would let consumers (cascade, admin UI) hot-reload the new value instead of re-reading
    # the file. For now the file write above is the only side effect.
    return get_refusal_threshold()


def warn_if_fallback_threshold() -> RefusalThreshold:
    """Log a warning when the live refusal threshold is still the uncalibrated 0.6 fallback.

    Lets an operator who relies on refusal see that no fit has been published. Returns the
    resolved threshold so a caller can also act on it.

    Safe to call at startup: it only reads the calibration file and settings and never raises.
    It is purely an observability aid and is not invoked automatically; call it from the app
    boot sequence (api/main.py) to make the fallback visible.
    """
    rt = get_refusal_threshold()
    if rt.source == "fallback":
        settings = get_settings()
        _logger().warning(
            "refusal_threshold_uncalibrated_fallback",
            value=rt.value,
            cohere_key_present=bool(settings.cohere_api_key),
            hint=(
                "no calibrated refusal threshold published — using the 0.6 "
                "fallback. Run `python -m regsuper.calibration.runtime publish` "
                "after labelling. NOTE: without COHERE_API_KEY rerank is skipped, "
                "so no calibrated score can ever be fitted or published."
            ),
        )
    return rt


async def publish_calibrated_threshold_from_db() -> RefusalThreshold:
    """Load labelled `retrieval_eval` rows, fit a threshold and persist it in one step.

    This is the production path from labelled data to a live calibrated threshold. Raises
    ValueError (from `save_calibrated_threshold`) when the fit yields no usable threshold, e.g.
    too few labelled rows, or every row has `rerank_score=None` because COHERE_API_KEY is
    unset, so the operator gets a clear failure instead of silently keeping the fallback.
    """
    # Imported lazily to keep a DB and model dependency off the hot read path
    # (get_refusal_threshold is called on every request).
    from regsuper.calibration.threshold import fit_threshold, load_labeled_evals

    rows = await load_labeled_evals()
    report = fit_threshold(rows)
    resolved = save_calibrated_threshold(report)
    _logger().info(
        "refusal_threshold_published",
        value=resolved.value,
        source=resolved.source,
        n_labeled=report.n_labeled,
        chosen_precision=report.chosen_precision,
    )
    return resolved


def _cli(argv: list[str]) -> int:
    """Minimal CLI: `publish` fits and persists; `show` prints the live value."""
    cmd = argv[1] if len(argv) > 1 else "show"
    if cmd == "publish":
        try:
            rt = asyncio.run(publish_calibrated_threshold_from_db())
        except ValueError as exc:
            print(f"publish failed: {exc}", file=sys.stderr)
            return 1
        print(
            f"published refusal_threshold={rt.value} (source={rt.source}) -> {calibration_path()}"
        )
        return 0
    if cmd == "show":
        rt = get_refusal_threshold()
        print(f"refusal_threshold={rt.value} source={rt.source}")
        return 0
    print(f"unknown command {cmd!r}; use 'publish' or 'show'", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover - operator CLI entrypoint
    raise SystemExit(_cli(sys.argv))
