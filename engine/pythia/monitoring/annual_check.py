"""Staleness checker for annual legal instruments (the alerting half).

Czech law has annual or periodic instruments, decrees reissued every year: travel allowances
(cestovní náhrady, currently decree 475/2024), minimum-wage coefficients set by government
regulation, limits derived from meal allowances (stravné), and so on. When a family's latest
instrument reaches its `valid_to` and no successor has been ingested, the corpus silently goes
stale: retrieval keeps serving last year's figures with no sign that a newer instrument should
exist. This module raises that signal. It only produces alerts; scheduling the check and routing
the alerts is the caller's job.

VL-F-10 AUDIT: valid_to semantics
---------------------------------
The corpus's canonical status rule, `taxonomy._temporal_status(valid_from, valid_to, as_of)`,
returns "REPEALED" when `valid_to is not None and valid_to <= as_of`.

Several core acts that are in force carry valid_to = 2026-12-31, among them 455/1991 (Trade
Licensing Act, živnostenský zákon), 435/2004 (Employment Act, zákon o zaměstnanosti) and 582/1991
(organisation of social security). A naive daily job that fed those rows through `_temporal_status`
would wrongly mark all three REPEALED on 2027-01-01, the day after their valid_to, although they
remain fully in force: their valid_to is a reissue or consolidation horizon, not a repeal.

Decision: for an annual instrument, `valid_to` is an EXPECTED-REISSUE horizon, not a repeal date.
The checker never marks anything REPEALED from `valid_to` alone. It only raises an alert, and only
on a positive no-successor finding (the latest instrument is past its horizon and the supplied
corpus rows contain no successor for that family). It fails open: if the horizon is unknown, a
successor is present, or the answer cannot be determined, no alert is raised, so an unknown never
becomes a false REPEALED or a false alert.

`run_annual_check` therefore returns human-review alerts, never a REPEALED verdict, and a scheduler
that consumes it must route them to a human review queue rather than write temporal_status. Code
that derives REPEALED from valid_to for an act listed in data/annual_instruments.json is a
regression.

Pure and offline: `run_annual_check(registry, rows_or_asof)` uses no database and reads no clock.
The caller passes the current date and, optionally, the corpus rows to scan for successors, so the
same inputs always give the same alerts. `load_registry()` reads and validates
data/annual_instruments.json.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

# data/annual_instruments.json, resolved relative to the engine root (this file is
# <root>/pythia/monitoring/annual_check.py).
_REGISTRY_PATH = Path(__file__).resolve().parents[2] / "data" / "annual_instruments.json"

# The family fields run_annual_check relies on. A registry entry missing any of them is skipped
# (fail open).
_REQUIRED_FAMILY_FIELDS = ("family", "domain", "latest_external_id", "valid_to", "cadence")


@dataclass(frozen=True)
class AnnualAlert:
    """One structured staleness alert: a human-review signal, never a REPEALED verdict.

    See the valid_to semantics in the module docstring.
    """

    family: str
    latest_ext: str
    valid_to: str  # ISO date string, as stored in the registry (the expected-reissue horizon)
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "family": self.family,
            "latest_ext": self.latest_ext,
            "valid_to": self.valid_to,
            "reason": self.reason,
        }


def _parse_date(value: Any) -> date | None:
    """Parse an ISO 'YYYY-MM-DD' string (or a date/datetime) into a date, or return None.

    An unparseable horizon is unknown and must fail open (no alert) rather than crash the daily job.
    """
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _row_external_id(row: Any) -> str | None:
    """Return the external_id of a supplied corpus row (a mapping or an object), best effort."""
    if isinstance(row, Mapping):
        val = row.get("external_id")
    else:
        val = getattr(row, "external_id", None)
    return str(val) if val else None


def _has_successor(
    family: Mapping[str, Any],
    rows: Iterable[Any],
    horizon: date,
) -> bool:
    """Return True if the supplied corpus `rows` contain a successor to the family's latest instrument.

    A successor is a corpus row for the family whose external_id differs from the latest known one.
    Family membership comes from an explicit `successor_external_ids` allow-list on the family, when
    the registry knows the successor id space; otherwise any other row carrying the family's own
    `domain` counts. This is deliberately conservative: when a row cannot be shown to belong to the
    family it is not treated as a successor (that would suppress a real alert), and rows that cannot
    be read are skipped rather than raising. `horizon` is accepted for future date-scoped matching;
    membership is currently decided by id and domain only, and neither path can produce a false
    REPEALED.
    """
    latest = str(family.get("latest_external_id"))
    allow = {str(x) for x in family.get("successor_external_ids", []) if x}
    domain = family.get("domain")

    for row in rows:
        ext = _row_external_id(row)
        if not ext or ext == latest:
            continue
        if allow:
            if ext in allow:
                return True
            continue
        # No explicit allow-list: fall back to domain membership when the row exposes a matching domain.
        row_domain = row.get("domain") if isinstance(row, Mapping) else getattr(row, "domain", None)
        if domain is not None and row_domain == domain:
            return True
    return False


def run_annual_check(
    registry: Mapping[str, Any],
    rows_or_asof: Any,
    *,
    as_of: date | None = None,
    rows: Iterable[Any] | None = None,
) -> list[dict[str, str]]:
    """Check the annual-instrument registry for stale families and return human-review alerts.

    Pure: no database and no clock. The caller supplies the current date and, optionally, the corpus
    rows to scan for successors. The second positional argument is overloaded for convenience,
    hence `rows_or_asof`:

    - `run_annual_check(registry, as_of_date)`: date only, no corpus rows to scan.
    - `run_annual_check(registry, corpus_rows, as_of=today)`: rows positionally, date by keyword.
    - `run_annual_check(registry, None, as_of=today, rows=corpus_rows)`: both by keyword.

    A family raises an alert when its latest instrument's `valid_to` is on or before `as_of` and the
    supplied rows contain no successor. The check fails open throughout: a missing or invalid
    valid_to, a missing as_of, a malformed family, or a present successor means no alert, so an
    unknown never becomes a false alert. In line with the valid_to semantics in the module
    docstring, it never emits a REPEALED verdict, only a human-review alert.

    Returns:
        A list of alert dicts with keys family, latest_ext, valid_to and reason.
    """
    # Disambiguate the overloaded positional arg.
    supplied_rows: Iterable[Any]
    effective_asof = as_of
    if isinstance(rows_or_asof, date):
        effective_asof = rows_or_asof if effective_asof is None else effective_asof
        supplied_rows = list(rows) if rows is not None else []
    elif rows_or_asof is None:
        supplied_rows = list(rows) if rows is not None else []
    else:
        supplied_rows = list(rows_or_asof) if rows is None else list(rows)

    # Without as_of we cannot tell what is in the past, so fail open (no alerts).
    if effective_asof is None:
        return []

    families = registry.get("families") if isinstance(registry, Mapping) else None
    if not isinstance(families, list):
        return []

    alerts: list[AnnualAlert] = []
    for fam in families:
        if not isinstance(fam, Mapping):
            continue
        if any(fam.get(f) in (None, "") for f in _REQUIRED_FAMILY_FIELDS):
            continue  # malformed family: fail open (skip, no alert)

        horizon = _parse_date(fam.get("valid_to"))
        if horizon is None:
            continue  # unknown horizon: fail open (no false alert)
        if horizon > effective_asof:
            continue  # still within its expected-reissue horizon: fresh, no alert

        if _has_successor(fam, supplied_rows, horizon):
            continue  # a successor was ingested: not stale

        alerts.append(
            AnnualAlert(
                family=str(fam.get("family")),
                latest_ext=str(fam.get("latest_external_id")),
                valid_to=str(fam.get("valid_to")),
                reason=(
                    f"latest annual instrument {fam.get('latest_external_id')} for family "
                    f"'{fam.get('family')}' passed its expected-reissue horizon {fam.get('valid_to')} "
                    f"(as_of {effective_asof.isoformat()}) and NO successor instrument was found in the "
                    f"supplied corpus rows — likely silently stale. Human review (do NOT auto-mark REPEALED)."
                ),
            )
        )

    return [a.as_dict() for a in alerts]


def load_registry(path: Path | str | None = None) -> dict[str, Any]:
    """Read and validate data/annual_instruments.json.

    Raises:
        ValueError: The registry is structurally invalid (no 'families' list, or a family missing a
            required field), so a broken registry fails loudly at load time instead of silently
            producing no alerts at run time.
    """
    p = Path(path) if path is not None else _REGISTRY_PATH
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("families"), list):
        raise ValueError(f"annual_instruments registry {p} must have a 'families' list")
    for i, fam in enumerate(data["families"]):
        if not isinstance(fam, dict):
            raise ValueError(f"annual_instruments registry {p}: family[{i}] is not an object")
        missing = [f for f in _REQUIRED_FAMILY_FIELDS if not fam.get(f)]
        if missing:
            raise ValueError(
                f"annual_instruments registry {p}: family[{i}] "
                f"({fam.get('family', '?')}) missing required field(s): {missing}"
            )
    return data
