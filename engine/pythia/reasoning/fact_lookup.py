"""Single source of a provision's practical facts for the fact verifier (V-FACT).

``lookup(provision_id[, fact_type])`` returns typed, verbatim facts anchored to character spans:

  1. Stored rows first: if the ``practical_fact`` table has rows for the provision, return them
     (optionally filtered to one fact type). These were extracted offline and verified on insert.
  2. Live regex fallback: if the provision has no rows (the table is not populated yet, or the
     extraction run skipped it), run ``practical_facts.extract_practical_facts`` over the
     provision's raw text. V-FACT therefore works before the table is filled, with the same
     verbatim-span semantics.

In both paths every ``FactRow`` satisfies ``value_raw == provision.text[char_start:char_end]``, and
V-FACT treats it as ground truth without re-deriving the substring. The fallback applies no
fact-type allow-list of its own; it returns whatever the extractor finds (all seven types:
deadline, authority, penalty, form, channel, fee, procedure_step), filtered only by ``fact_type``.

Only reached from the V-FACT path, which is off by default. Reads the database and never writes.

With ``with_temporal=True`` each row also carries its provision's temporal status and a fail-open
``temporal_superseded`` flag: only a known superseded or not-yet-in-force status is flagged, and a
missing or unknown status is never a false temporal failure. V-FACT records a superseded row in its
verdict evidence in shadow mode only; no output-changing decision uses it yet. The default
(``with_temporal=False``) adds no temporal keys and makes no extra database read.
"""

from __future__ import annotations

from dataclasses import dataclass

from pythia.db import get_pool
from pythia.ports import NexusPort
from pythia.reasoning.practical_facts import extract_practical_facts

# Not-in-force vocabulary, the same as v_safe.py's _PIT_/_DOC_ sets, so "not current law" means one
# thing across the codebase. Only a known not-in-force value counts; anything else (NULL, '',
# 'IN_FORCE', unknown strings) is treated as in force. This fails open: the absence of a
# not-in-force marker is not staleness.
_PIT_NOT_IN_FORCE: frozenset[str] = frozenset({"SUPERSEDED", "NOT_YET_IN_FORCE"})
_DOC_NOT_IN_FORCE: frozenset[str] = frozenset(
    {
        "repealed",
        "superseded",
        "annulled",
        "abolished",
        "annulled_by_cc",
        "future_effective",
        "not_yet_in_force",
        "not_yet_effective",
    }
)


def _CURRENT_ONLY_PRED(pit_status, temporal_status) -> bool:  # noqa: N802 - mirrors seed._CURRENT_ONLY_PRED
    """Return True only if the status is a known not-in-force value.

    Known values include SUPERSEDED, NOT_YET_IN_FORCE, repealed and annulled. A missing, blank,
    unknown or 'IN_FORCE' status returns False, so the row is never wrongly flagged. Insensitive to
    case and hyphens.
    """
    pit = str(pit_status or "").strip().upper().replace("-", "_")
    doc = str(temporal_status or "").strip().lower().replace("-", "_")
    return pit in _PIT_NOT_IN_FORCE or doc in _DOC_NOT_IN_FORCE


@dataclass(frozen=True)
class FactRow:
    """One practical fact.

    ``value_raw == provision.text[char_start:char_end]`` holds for both stored rows and the regex
    fallback; ``source`` records which path produced it ('table' or 'regex') for telemetry. The
    temporal fields default to None and are filled only by ``lookup(..., with_temporal=True)``;
    ``as_dict()`` omits them otherwise.
    """

    fact_type: str
    value_raw: str
    char_start: int
    char_end: int
    value_norm: dict | None
    extraction_method: str  # 'regex' or 'llm' (the stored method; 'regex' for the fallback)
    source: str  # 'table' (practical_fact row) or 'regex' (live fallback)
    # Temporal fields, set only by a with_temporal=True lookup (None means not requested).
    pit_status: str | None = None
    temporal_status: str | None = None
    temporal_superseded: bool | None = None  # True: known not in force; False: current or unknown
    _with_temporal: bool = False  # whether the temporal fields were requested

    def as_dict(self) -> dict:
        d = {
            "fact_type": self.fact_type,
            "value_raw": self.value_raw,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "value_norm": self.value_norm,
            "extraction_method": self.extraction_method,
            "source": self.source,
        }
        # Temporal keys appear only when they were requested.
        if self._with_temporal:
            d["pit_status"] = self.pit_status
            d["temporal_status"] = self.temporal_status
            d["temporal_superseded"] = self.temporal_superseded
        return d


_SELECT_ROWS = """
    SELECT fact_type, value_raw, char_start, char_end, value_norm, extraction_method
      FROM practical_fact
     WHERE provision_id = $1::uuid
       AND ($2::text IS NULL OR fact_type = $2)
     ORDER BY char_start, char_end, fact_type
"""


def _norm_as_dict(value_norm) -> dict | None:
    """Coerce a jsonb value to a dict or None (asyncpg returns a str without a codec, else a dict)."""
    if value_norm is None:
        return None
    if isinstance(value_norm, dict):
        return value_norm
    if isinstance(value_norm, str):
        import json as _json

        try:
            v = _json.loads(value_norm)
            return v if isinstance(v, dict) else None
        except Exception:  # noqa: BLE001
            return None
    return None


async def _temporal_facets(provision_id: str, prov=None) -> tuple[str | None, str | None]:
    """Return the provision's ``(pit_status, temporal_status)``, read through NexusPort.

    An already-fetched ProvisionRow (``prov``) is reused to avoid a second read. A missing provision
    or missing fields give ``(None, None)``, which leaves the row unflagged.
    """
    if prov is None:
        provs = await NexusPort().fetch_provisions([provision_id])
        prov = provs[0] if provs else None
    if prov is None:
        return None, None
    return getattr(prov, "pit_status", None), getattr(prov, "temporal_status", None)


def _with_temporal_row(row: FactRow, pit_status: str | None, temporal_status: str | None) -> FactRow:
    """Return a copy of ``row`` with the temporal fields and the ``temporal_superseded`` flag set.

    The flag is True only for a known not-in-force status (see ``_CURRENT_ONLY_PRED``).
    """
    from dataclasses import replace

    return replace(
        row,
        pit_status=pit_status,
        temporal_status=temporal_status,
        temporal_superseded=_CURRENT_ONLY_PRED(pit_status, temporal_status),
        _with_temporal=True,
    )


async def lookup(provision_id: str, fact_type: str | None = None, *, with_temporal: bool = False) -> list[FactRow]:
    """Return the provision's practical facts: stored rows first, otherwise a live regex pass.

    Args:
        provision_id: The provision id (a UUID string).
        fact_type: Optional filter to one fact type; None returns all types.
        with_temporal: Also attach the provision's temporal status and the fail-open
            ``temporal_superseded`` flag to every row (one extra read, or none if the fallback
            already fetched the provision).

    Returns:
        Facts anchored verbatim to the provision's text. Empty only when the provision has no stored
        rows and its text yields no regex matches, or the id resolves to nothing.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SELECT_ROWS, provision_id, fact_type)
    if rows:
        out = [
            FactRow(
                fact_type=r["fact_type"],
                value_raw=r["value_raw"],
                char_start=r["char_start"],
                char_end=r["char_end"],
                value_norm=_norm_as_dict(r["value_norm"]),
                extraction_method=r["extraction_method"],
                source="table",
            )
            for r in rows
        ]
        if with_temporal:
            # One temporal fetch for the provision, applied to every row.
            pit, doc = await _temporal_facets(provision_id)
            out = [_with_temporal_row(r, pit, doc) for r in out]
        return out
    # Fallback: no stored rows. Read the text through NexusPort (never raw SQL on the provision
    # table; the port-boundary test enforces this) and extract facts live.
    provs = await NexusPort().fetch_provisions([provision_id])
    prov = provs[0] if provs else None
    text = getattr(prov, "text", None) if prov is not None else None

    if not text:
        return []
    facts = extract_practical_facts(text)
    out = [
        FactRow(
            fact_type=f.fact_type,
            value_raw=f.value_raw,
            char_start=f.char_start,
            char_end=f.char_end,
            value_norm=None,
            extraction_method="regex",
            source="regex",
        )
        for f in facts
        if fact_type is None or f.fact_type == fact_type
    ]
    if with_temporal:
        # Reuse the ProvisionRow the fallback already fetched; no second read.
        pit, doc = await _temporal_facets(provision_id, prov=prov)
        out = [_with_temporal_row(r, pit, doc) for r in out]
    return out


async def classify_no_fact(provision_id: str, *, prov=None) -> str:
    """Classify why a provision yielded no practical fact (``lookup(...) == []``).

      * ``extraction_not_run``: the provision's act is absent from the extraction_coverage manifest,
        so the extractor never processed it. This is a silent fallback and the caller must not emit
        a gap event: the honest state is "this act has not been extracted yet", not "there is no
        fact here".
      * ``no_fact_row``: the act was extracted but this provision has no fact of the asked type. A
        genuine fact gap the caller may report.

    Without this distinction an act that was never extracted would report a false ``no_fact_row``.

    If the extraction_coverage table does not exist yet, ``covered_acts()`` is empty, every act
    counts as not covered and the result is ``extraction_not_run``; a database that never ran the
    backfill never reports a false fact gap. ``prov`` reuses an already-fetched ProvisionRow;
    otherwise it is read through NexusPort to get the act's external_id. A provision that does not
    resolve or has no external_id also gives ``extraction_not_run``.

    Returns:
        The GapKind wire string (``GapKind.EXTRACTION_NOT_RUN.value`` or
        ``GapKind.NO_FACT_ROW.value``).
    """
    from pythia.reasoning.gap_kind import GapKind

    if prov is None:
        provs = await NexusPort().fetch_provisions([provision_id])
        prov = provs[0] if provs else None
    ext = getattr(prov, "external_id", None) if prov is not None else None
    if not ext:
        return GapKind.EXTRACTION_NOT_RUN.value  # unknown act: silent, never a false no_fact_row
    from pythia.ingest.extraction_coverage import covered_acts

    pool = await get_pool()
    async with pool.acquire() as conn:
        covered = await covered_acts(conn, [ext])
    return GapKind.NO_FACT_ROW.value if ext in covered else GapKind.EXTRACTION_NOT_RUN.value
