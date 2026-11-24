"""WF-6 Increment 1 — temporal freshness (is the cited law CURRENT?) pure core + scorer table.

Covers the deterministic verdict, the boundary-date arithmetic, the temporal_status dialect handling, and — most
importantly — the two SAFETY INVARIANTS that make this a SIGNAL, never a filter:

  INV-HONEST (never-falsely-assert-current): missing / NULL temporal data yields ``unknown``, NEVER ``current``.
              A missing column must not read as "in force". Checked exhaustively over the NULL combinations.
  INV-SIGNAL (never-drop): the function only ANNOTATES — there is no verdict, and no input, that removes/hides a
              finding. Every verdict returns a fully-populated annotation dict; the finding is always kept.

Plus a constructed scorer table mapping (valid_from, valid_to, temporal_status, as_of) -> expected verdict that
exercises EVERY branch, both temporal_status dialects (app hyphenated + engine UPPER), every NULL combination, and
the inclusive boundary (valid_to == as_of, valid_from == as_of). Pure — no DB, no network, no LLM, no clock.

DATA GAP (recorded): the applicability_gold.json fixture carries ONLY {actor, modal, statement, confidence} per
obligation — it has NO provision temporal columns (valid_from / valid_to / temporal_status). WF-6 is a
PROVISION-temporal axis, so there is nothing in the gold slice to score against. Per the spec, this suite uses
CONSTRUCTED Provision-like inputs (the scorer table below) that name each legal case the verdict must cover. A
real-corpus eval is deferred to the wiring increment, when provision temporal columns are joined to findings.
"""

from __future__ import annotations

from datetime import date
from typing import get_args

import pytest

from regsuper.analysis.temporal_freshness import (
    Verdict,
    temporal_freshness,
)

_VERDICTS = set(get_args(Verdict))

# A fixed analysis date used across the table (no clock reads — passed in explicitly).
AS_OF = date(2026, 6, 29)


# ── THE SCORER TABLE: (valid_from, valid_to, temporal_status, as_of) -> expected verdict ────────────────────
# Each row names the legal case it covers. Dates relative to AS_OF = 2026-06-29.
_D = date  # alias for brevity
_CASES: list[tuple[str, _D | None, _D | None, str | None, _D, str]] = [
    # id,                              valid_from,       valid_to,         temporal_status,      as_of,  expected
    # ── current: positive in-window evidence ───────────────────────────────────────────────────────────────
    ("open_window_both_bounds", _D(2020, 1, 1), _D(2030, 1, 1), None, AS_OF, "current"),
    ("open_window_lower_only", _D(2020, 1, 1), None, None, AS_OF, "current"),
    ("open_window_upper_only", None, _D(2030, 1, 1), None, AS_OF, "current"),
    ("status_in_force_app", None, None, "in-force", AS_OF, "current"),
    ("status_in_force_engine_upper", None, None, "IN_FORCE", AS_OF, "current"),
    ("status_effective_word", None, None, "effective", AS_OF, "current"),
    (
        "in_force_with_redundant_status",
        _D(2019, 5, 5),
        _D(2031, 5, 5),
        "in-force",
        AS_OF,
        "current",
    ),
    # ── superseded: window closed strictly before as_of ──────────────────────────────────────────────────────
    ("superseded_by_valid_to", _D(2010, 1, 1), _D(2024, 12, 31), None, AS_OF, "superseded"),
    ("superseded_status_app", _D(2010, 1, 1), _D(2024, 1, 1), "superseded", AS_OF, "superseded"),
    ("superseded_day_before", None, _D(2026, 6, 28), None, AS_OF, "superseded"),
    (
        "superseded_even_if_status_says_inforce",
        _D(2010, 1, 1),
        _D(2024, 1, 1),
        "in-force",
        AS_OF,
        "superseded",
    ),
    # ── future_effective: not yet in force ───────────────────────────────────────────────────────────────────
    ("future_by_valid_from", _D(2027, 1, 1), None, None, AS_OF, "future_effective"),
    (
        "future_by_valid_from_with_to",
        _D(2027, 1, 1),
        _D(2040, 1, 1),
        None,
        AS_OF,
        "future_effective",
    ),
    ("future_day_after", _D(2026, 6, 30), None, None, AS_OF, "future_effective"),
    ("future_status_no_dates", None, None, "future-effective", AS_OF, "future_effective"),
    ("future_status_engine_upper", None, None, "FUTURE_EFFECTIVE", AS_OF, "future_effective"),
    # ── annulled: status declares a repeal (dispositive even with no dates / open window) ───────────────────────
    ("annulled_app", None, None, "annulled", AS_OF, "annulled"),
    ("annulled_engine_upper", None, None, "ANNULLED", AS_OF, "annulled"),
    ("repealed_word", _D(2010, 1, 1), None, "repealed", AS_OF, "annulled"),
    ("abrogated_word", None, None, "abrogated", AS_OF, "annulled"),
    (
        "annulled_overrides_open_window",
        _D(2010, 1, 1),
        _D(2099, 1, 1),
        "ANNULLED",
        AS_OF,
        "annulled",
    ),
    # ── unknown: no verifiable evidence (HONEST — never silently 'current') ──────────────────────────────────
    ("all_null", None, None, None, AS_OF, "unknown"),
    ("blank_status_only", None, None, "   ", AS_OF, "unknown"),
    ("unrecognised_status_no_dates", None, None, "PROVISIONAL", AS_OF, "unknown"),
    # ── boundary dates (inclusive window) ────────────────────────────────────────────────────────────────────
    (
        "valid_to_equals_as_of_is_current",
        _D(2010, 1, 1),
        AS_OF,
        None,
        AS_OF,
        "current",
    ),  # last in-force day
    (
        "valid_from_equals_as_of_is_current",
        AS_OF,
        None,
        None,
        AS_OF,
        "current",
    ),  # first in-force day
    ("both_bounds_equal_as_of", AS_OF, AS_OF, None, AS_OF, "current"),  # single-day window, today
]


@pytest.mark.parametrize("case", _CASES, ids=[c[0] for c in _CASES])
def test_scorer_table(case):
    cid, vf, vt, status, as_of, expected = case
    out = temporal_freshness(vf, vt, status, as_of)
    assert out["verdict"] == expected, (
        f"{cid}: expected {expected}, got {out['verdict']} ({out['reason']})"
    )
    assert out["verdict"] in _VERDICTS


# ── boundary arithmetic, called out explicitly (the spec asks for valid_to == as_of) ────────────────────────
def test_boundary_valid_to_equals_as_of_is_current_not_superseded():
    """INCLUSIVE end: the as-of date is the LAST in-force day. valid_to == as_of must be `current`, not stale."""
    out = temporal_freshness(date(2020, 1, 1), AS_OF, None, AS_OF)
    assert out["verdict"] == "current"
    assert out["stale_since"] is None


def test_boundary_valid_to_one_day_before_is_superseded():
    out = temporal_freshness(date(2020, 1, 1), date(2026, 6, 28), None, AS_OF)
    assert out["verdict"] == "superseded"
    assert out["stale_since"] == date(2026, 6, 28)


def test_boundary_valid_from_equals_as_of_is_current_not_future():
    """INCLUSIVE start: the as-of date is the FIRST in-force day. valid_from == as_of must be `current`."""
    out = temporal_freshness(AS_OF, None, None, AS_OF)
    assert out["verdict"] == "current"
    assert out["effective_as_of"] is None


def test_boundary_valid_from_one_day_after_is_future():
    out = temporal_freshness(date(2026, 6, 30), None, None, AS_OF)
    assert out["verdict"] == "future_effective"
    assert out["effective_as_of"] == date(2026, 6, 30)


# ── the two annotation-detail fields are set on (and only on) the right verdicts ────────────────────────────
def test_stale_since_set_only_for_superseded():
    sup = temporal_freshness(date(2010, 1, 1), date(2024, 1, 1), None, AS_OF)
    assert sup["verdict"] == "superseded" and sup["stale_since"] == date(2024, 1, 1)
    # not set on the others
    for vf, vt, st in [
        (date(2020, 1, 1), date(2030, 1, 1), None),  # current
        (date(2027, 1, 1), None, None),  # future
        (None, None, "annulled"),  # annulled
        (None, None, None),
    ]:  # unknown
        out = temporal_freshness(vf, vt, st, AS_OF)
        assert out["stale_since"] is None, f"stale_since leaked onto {out['verdict']}"


def test_effective_as_of_set_only_for_future_effective():
    fut = temporal_freshness(date(2027, 1, 1), None, None, AS_OF)
    assert fut["verdict"] == "future_effective" and fut["effective_as_of"] == date(2027, 1, 1)
    for vf, vt, st in [
        (date(2020, 1, 1), date(2030, 1, 1), None),  # current
        (date(2010, 1, 1), date(2024, 1, 1), None),  # superseded
        (None, None, "annulled"),  # annulled
        (None, None, None),
    ]:  # unknown
        out = temporal_freshness(vf, vt, st, AS_OF)
        assert out["effective_as_of"] is None, f"effective_as_of leaked onto {out['verdict']}"


# ── THE HONESTY INVARIANT: missing data is `unknown`, never a silent `current` ──────────────────────────────
def test_no_temporal_data_is_unknown_never_current():
    """INV-HONEST: with NO temporal evidence we return `unknown`, never `current`. A missing column must not read
    as 'in force' — that would be an unverified safety claim."""
    out = temporal_freshness(None, None, None, AS_OF)
    assert out["verdict"] == "unknown"
    assert out["verdict"] != "current"


def test_blank_and_whitespace_status_is_unknown():
    for blank in ("", "   ", "\t", None):
        out = temporal_freshness(None, None, blank, AS_OF)
        assert out["verdict"] == "unknown", (
            f"blank status {blank!r} should be unknown, got {out['verdict']}"
        )


def test_unrecognised_status_without_dates_is_unknown_not_current():
    """An unrecognised non-empty status (neither in-force nor a kill word) with no dates must NOT be coerced to
    `current` — it falls through to the honest `unknown`."""
    out = temporal_freshness(None, None, "SOME_UNMODELLED_STATE", AS_OF)
    assert out["verdict"] == "unknown"


@pytest.mark.parametrize("vf", [None, date(2020, 1, 1), date(2027, 1, 1)])
@pytest.mark.parametrize("vt", [None, date(2024, 1, 1), date(2030, 1, 1)])
@pytest.mark.parametrize("status", [None, "", "   "])
def test_current_requires_positive_evidence_never_from_absence(vf, vt, status):
    """Sweep: `current` is returned ONLY when there is at least one piece of positive in-window evidence (a
    satisfied date bound). When ALL temporal inputs are absent/blank, the verdict is `unknown` — never `current`."""
    out = temporal_freshness(vf, vt, status, AS_OF)
    if vf is None and vt is None and not (status or "").strip():
        assert out["verdict"] == "unknown", "no evidence at all must be unknown"
    if out["verdict"] == "current":
        # something positive must have carried it
        opened = vf is not None and vf <= AS_OF
        not_closed = vt is not None and vt >= AS_OF
        assert opened or not_closed, "current asserted without a satisfied date bound"


# ── THE SIGNAL INVARIANT: never drops / hides a finding — only annotates ────────────────────────────────────
def test_verdict_is_pure_signal_never_drops():
    """INV-SIGNAL: temporal_freshness is an ANNOTATION, not a filter. For EVERY representative input — including
    the 'worst' temporal states (annulled, superseded) — it returns a fully-populated annotation dict and never
    signals removal. There is no key, and no value, that means 'drop this finding'. The cardinal rule: a stale or
    repealed citation is FLAGGED for the reader, never silently hidden."""
    representative = [
        (date(2020, 1, 1), date(2030, 1, 1), None),  # current
        (date(2010, 1, 1), date(2024, 1, 1), None),  # superseded
        (date(2027, 1, 1), None, None),  # future
        (None, None, "ANNULLED"),  # annulled  ← even a hard repeal is a flag, not a drop
        (None, None, None),  # unknown
    ]
    drop_signals = {"drop", "hide", "suppress", "remove", "delete", "exclude"}
    for vf, vt, st in representative:
        out = temporal_freshness(vf, vt, st, AS_OF)
        # always a complete annotation
        assert set(out) >= {"verdict", "reason", "stale_since", "effective_as_of", "analysis_as_of"}
        assert out["verdict"] in _VERDICTS
        assert isinstance(out["reason"], str) and out["reason"]
        # no drop-shaped signal anywhere in the output (neither as a verdict nor as a key)
        assert out["verdict"] not in drop_signals
        assert not (drop_signals & set(out))
        # the annotation echoes the as-of date so it is self-describing / reproducible
        assert out["analysis_as_of"] == AS_OF


def test_annulled_is_annotated_not_dropped():
    """A repealed provision is the strongest 'stale' case — it must still come back as a kept, flagged finding."""
    out = temporal_freshness(date(2010, 1, 1), date(2099, 1, 1), "ANNULLED", AS_OF)
    assert out["verdict"] == "annulled"
    assert out["reason"]  # has an explanation for the reader
    assert "analysis_as_of" in out  # self-describing annotation, not a deletion


# ── temporal_status dialect handling (app hyphenated + engine UPPER both work) ──────────────────────────────
@pytest.mark.parametrize(
    "status", ["annulled", "ANNULLED", "Annulled", "repealed", "REPEALED", "abrogated", "revoked"]
)
def test_annulled_dialects(status):
    assert temporal_freshness(None, None, status, AS_OF)["verdict"] == "annulled"


@pytest.mark.parametrize(
    "status", ["in-force", "IN_FORCE", "in force", "effective", "valid", "current"]
)
def test_in_force_dialects(status):
    assert temporal_freshness(None, None, status, AS_OF)["verdict"] == "current"


@pytest.mark.parametrize(
    "status",
    ["future-effective", "future_effective", "FUTURE_EFFECTIVE", "pending", "NOT_YET_EFFECTIVE"],
)
def test_future_status_dialects_without_dates(status):
    assert temporal_freshness(None, None, status, AS_OF)["verdict"] == "future_effective"


def test_annulled_overrides_dates():
    """A repeal is dispositive: even an otherwise-open validity window reads as `annulled`."""
    out = temporal_freshness(date(2010, 1, 1), date(2099, 1, 1), "repealed", AS_OF)
    assert out["verdict"] == "annulled"


def test_superseded_takes_precedence_over_stale_inforce_status():
    """If the dates say the window closed but a stale status still says 'in-force', the closed window wins (the
    date evidence is more specific than a possibly-stale status flag) — and it surfaces as `superseded`, not
    hidden."""
    out = temporal_freshness(date(2010, 1, 1), date(2024, 1, 1), "in-force", AS_OF)
    assert out["verdict"] == "superseded"


# ── determinism / purity ────────────────────────────────────────────────────────────────────────────────────
def test_deterministic_same_input_same_output():
    args = (date(2020, 1, 1), date(2030, 1, 1), "in-force", AS_OF)
    assert temporal_freshness(*args) == temporal_freshness(*args)


def test_as_of_is_explicit_argument_changes_verdict():
    """No clock reads: the verdict is driven purely by the passed-in as_of. The same provision is future, then
    current, then superseded as the as-of date advances across its window."""
    vf, vt = date(2026, 1, 1), date(2026, 12, 31)
    assert temporal_freshness(vf, vt, None, date(2025, 1, 1))["verdict"] == "future_effective"
    assert temporal_freshness(vf, vt, None, date(2026, 6, 1))["verdict"] == "current"
    assert temporal_freshness(vf, vt, None, date(2027, 1, 1))["verdict"] == "superseded"


def test_every_verdict_value_is_in_the_closed_set():
    """Property: across a grid of inputs, the verdict is ALWAYS a member of the closed Verdict set."""
    dates = [None, date(2020, 1, 1), AS_OF, date(2030, 1, 1)]
    statuses = [None, "", "in-force", "annulled", "future-effective", "PROVISIONAL"]
    for vf in dates:
        for vt in dates:
            for st in statuses:
                out = temporal_freshness(vf, vt, st, AS_OF)
                assert out["verdict"] in _VERDICTS
                assert out["analysis_as_of"] == AS_OF
                assert out["engine_version"] == 1


@pytest.mark.parametrize("status", ["superseded", "SUPERSEDED", "expired", "replaced", "lapsed"])
def test_superseded_status_without_dates_is_superseded_not_unknown(status):
    """A status that declares the provision closed is evidence even with no dates (mirrors the
    future-effective rule); stale_since stays None because no end date is known."""
    out = temporal_freshness(None, None, status, AS_OF)
    assert out["verdict"] == "superseded"
    assert out["stale_since"] is None


@pytest.mark.parametrize(
    ("vf", "expected"),
    [(date(2027, 1, 1), "future_effective"), (date(2020, 1, 1), "current")],
)
def test_engine_not_yet_effective_status_defers_to_dates(vf, expected):
    # With a date present the date still decides, as for every other status: a stale
    # NOT_YET_EFFECTIVE on a provision whose start has passed reads current.
    assert temporal_freshness(vf, None, "NOT_YET_EFFECTIVE", AS_OF)["verdict"] == expected
