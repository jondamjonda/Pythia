"""Typed plan of the structured draft, passed from drafting to claim verification.

The drafting layer exposes its structure ("what to say, in which slots") only as the rendered
``PythiaAnswer.sections`` list, and the claim atomiser re-derives its partition from that list
(``claims._tiles_from_sections``). This module makes the boundary explicit: ``plan_from_sections``
builds a typed ``DraftPlan`` (``contract.DraftPlan`` / ``DraftSlot``) that the verifiers can read
directly, and ``sections_from_plan`` turns it back into sections.

The plan is a lossless view of the draft, for any draft::

    sections_from_plan(plan_from_sections(secs), draft) == secs

A DraftSlot copies each Section's ``kind``, ``heading``, ``range`` and ``citations_map`` verbatim,
and ``split_into_sections`` already guarantees ``Section.text == draft[range[0]:range[1]]``, so
re-slicing the draft reproduces the text exactly. The one extra field, ``seed_provision_ids``, is
derived from the slot's citations and is never read back, so it cannot affect the round trip.

Pure and total: no LLM, database, clock or randomness, and neither function raises. Used only when
``config.draft_planner_enabled`` is on (off by default).
"""

from __future__ import annotations

from pythia.contract import DraftPlan, DraftSlot, Section
from pythia.reasoning.sections import _HEADINGS

# Version of the draft grammar (the slot kinds and their canonical headings). Downstream consumers
# (claim verification, the claim ledger, evaluation artifacts) can check it. Bump it whenever the
# grammar changes (a heading renamed, a slot kind added, removed or reordered);
# tests/test_draft_plan.py fails if the grammar changes without a bump.
DRAFT_FORMAT_VERSION = "atoms-v1"

# {kind: canonical heading}, derived from ``sections._HEADINGS`` (the single source of slot
# headings) rather than copied, so a heading edit in sections.py shows up here and the version test
# catches a missing bump. A duplicated kind would keep its last heading (none exists); order follows
# _HEADINGS.
SLOT_HEADINGS: dict[str, str] = {kind: canonical for _alt, kind, canonical in _HEADINGS}


def _seed_ids(citation_map: list[dict]) -> list[str]:
    """Return the distinct provision ids cited by a slot, in first-seen order.

    First-seen order is the retrieval rank of the citations. A citation without a string
    ``provision_id`` is skipped.
    """
    seen: set[str] = set()
    out: list[str] = []
    for c in citation_map or []:
        pid = c.get("provision_id") if isinstance(c, dict) else None
        if isinstance(pid, str) and pid not in seen:
            seen.add(pid)
            out.append(pid)
    return out


def plan_from_sections(sections: list[Section] | None) -> DraftPlan:
    """Build a ``DraftPlan`` from the structured draft sections.

    One DraftSlot per Section, in order, copying ``kind``, ``heading``, ``range`` and
    ``citations_map`` verbatim and adding the derived ``seed_provision_ids``. No sections give an
    empty plan.
    """
    slots: list[DraftSlot] = []
    for s in sections or []:
        slots.append(
            DraftSlot(
                kind=s.kind,
                heading=s.heading,
                range=list(s.range),  # copy; never alias the Section's list
                citation_map=list(s.citations_map),  # shallow copy (same citation dicts)
                seed_provision_ids=_seed_ids(s.citations_map),
            )
        )
    return DraftPlan(slots=slots)


def sections_from_plan(plan: DraftPlan | None, draft: str | None) -> list[Section]:
    """Rebuild the ``Section`` list from a ``DraftPlan`` and the draft prose.

    Each section's ``text`` is re-sliced from ``draft[range[0]:range[1]]``; ``kind``, ``heading``,
    ``range`` and the citations are copied from the slot. An empty or None plan gives ``[]``; a slot
    with a malformed range gets empty text.
    """
    text = draft or ""
    out: list[Section] = []
    for slot in plan.slots if plan is not None else []:
        r = slot.range
        if len(r) == 2 and isinstance(r[0], int) and isinstance(r[1], int) and 0 <= r[0] <= r[1] <= len(text):
            body = text[r[0] : r[1]]
        else:
            body = ""
        out.append(
            Section(
                kind=slot.kind,
                heading=slot.heading,
                text=body,
                range=list(r),
                citations_map=list(slot.citation_map),
            )
        )
    return out
