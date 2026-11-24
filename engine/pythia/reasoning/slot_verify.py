"""Per-slot grounding check for the structured seven-slot action plan, without any LLM.

Runs only when ``structured_answer_enabled`` is on, after ``sections.split_into_sections``. It is
the anti-fabrication net for the action plan when the claim-verify loop is off:

  * Every claim slot (applies, obligations, steps, where, risks) must contain at least one grounded
    citation. A slot without one is an unsupported assertion: it is demoted (recorded in
    ``demoted_slots``) and an honest draft gap line from gap_registry is emitted instead.
  * The "Vaše situace" restatement of the asker's situation gets a deterministic guard: if it
    slips in a legal characterisation (a §, an act number, or an obligation such as "musíte",
    "jste povinni", "plátce DPH"), it is demoted so an uncited legal claim cannot hide there.
  * "Co korpus nepokrývá" (what the corpus does not cover) is expected to be uncited and is not
    checked.

It also lifts verbatim practical facts (reasoning.practical_facts) from each grounded provision into
PracticalItem rows, tagged with the slot they best support. Everything is additive: the flat
``drafted_answer`` and ``sections`` remain the complete answer. Pure and reproducible.
"""

from __future__ import annotations

import re

from pythia.contract import PracticalBlock, PracticalItem, Section
from pythia.reasoning import gap_registry
from pythia.reasoning.practical_facts import extract_practical_facts

# Slots whose prose asserts something the asker is expected to act on; they must be grounded.
_CLAIM_SLOTS: frozenset[str] = frozenset({"applies", "obligations", "steps", "where", "risks"})

# The situation restatement must not carry an uncited legal characterisation. Deliberately
# over-inclusive: a false positive only turns the restatement into a cautious gap line, which is the
# safe direction.
_SITUATION_LEGAL_RE = re.compile(
    r"§|zákon[a-ěíéý]*\s*č\.|\bč\.\s*\d+\s*/\s*\d{4}|"
    r"\bmus[ií]te\b|\bnesm[ií]te\b|\bjste\s+povin[a-ý]+\b|\bje\s+povin[a-ý]+\b|\bplátc[ei]?\s+DPH\b",
    re.IGNORECASE,
)

# fact_type to the action-plan slot it best supports (used for PracticalItem.slot).
_FACT_SLOT: dict[str, str] = {
    "deadline": "obligations",
    "authority": "where",
    "penalty": "risks",
    "form": "steps",
    # The submission channel and procedural steps belong in "steps"; an administrative fee is a
    # cost, grouped with penalties under "risks".
    "channel": "steps",
    "fee": "risks",
    "procedure_step": "steps",
}
# Demoted claim slot to the gap_registry class that best explains the corpus limit.
_SLOT_GAP_CLASS: dict[str, str] = {
    "where": "authority_locator",
    "steps": "form_portal",
    "applies": "soft_law_guidance",
    "obligations": "soft_law_guidance",
    "risks": "soft_law_guidance",
}

# Gap class for a situation slot that carried an uncited legal characterisation.
_SITUATION_GAP_CLASS = "situation_uncited_legal_claim"


def _body(section_text: str) -> str:
    """Strip the leading '## Heading' line so the guard scans only the restatement prose.

    ``Section.text`` includes its own heading line.
    """
    parts = (section_text or "").split("\n", 1)
    if len(parts) == 2 and parts[0].lstrip().startswith("#"):
        return parts[1]
    return section_text or ""


def verify_slots(
    sections: list[Section],
    *,
    cited_texts: dict[str, str] | None = None,
    profile_used: bool = False,
) -> PracticalBlock:
    """Build the PracticalBlock for a structured draft. Never raises, including on empty input.

    Args:
        sections: ``split_into_sections`` output for a structured draft (may be empty).
        cited_texts: Grounded provision id to its (widened) text; the source of verbatim
            practical facts.
        profile_used: Whether the user's saved business profile seeded the situation slot.
    """
    items: list[PracticalItem] = []
    for pid, text in (cited_texts or {}).items():
        for fact in extract_practical_facts(text or ""):
            items.append(
                PracticalItem(
                    slot=_FACT_SLOT.get(fact.fact_type, "obligations"),
                    fact_type=fact.fact_type,
                    value_raw=fact.value_raw,
                    provision_id=pid,
                    grounded=True,
                )
            )

    demoted: list[str] = []
    gap_classes: list[str] = []
    situation_flagged = False
    for sec in sections or []:
        if sec.kind in _CLAIM_SLOTS:
            if not sec.citations_map:  # a claim slot with no grounded citation is unsupported
                demoted.append(sec.kind)
                gap_classes.append(_SLOT_GAP_CLASS.get(sec.kind, "soft_law_guidance"))
        elif sec.kind == "situation" and _SITUATION_LEGAL_RE.search(_body(sec.text)):
            demoted.append("situation")
            situation_flagged = True

    gaps: list[dict] = gap_registry.gap_items(gap_classes) if gap_classes else []
    if situation_flagged:
        gaps.append(
            {
                "gap_class": _SITUATION_GAP_CLASS,
                "text": gap_registry.demoted_line(),
                "status": gap_registry.DRAFT_MARKER,
            }
        )
    return PracticalBlock(items=items, gaps=gaps, demoted_slots=demoted, profile_used=profile_used)
