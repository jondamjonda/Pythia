"""Honest gap lines that replace claims which failed verification and could not be repaired.

Maps ``(slot, claim_type, check)`` to a gap line. When a claim fails verification and cannot be
repaired, reassemble.py cuts the claim's sentence and inserts an honest gap line instead: it never
invents a substitute value and never repeats the withheld assertion. This module is the single
registry of that copy and of the ``gap_source`` field.

A GapLine carries two independent fields:
  * ``gap_kind`` (gap_kind.py): what material is missing; feeds the connector backlog.
  * ``gap_source`` (here): why the line was rendered, from the ``vf_*`` verification-failure
    family, so verification failures do not pollute backlog telemetry.
reassemble.py de-duplicates on ``(slot, gap_source)``, so a line is never emitted twice.

Gap copy never looks like a citation: it has no bare ``§`` or act number, so a gap line can never
count toward verify.py's 0.34 grounding floor when reassemble reruns ``verify_answer`` on the final
text. ``provision_hint`` is the human-readable label from the failed claim's own citation (never
invented); citation-like tokens are stripped from it and it is rendered as free prose, never as
``[act § n]``.

All Czech copy is a draft pending lawyer review: descriptive and cautious, stating a corpus limit
and where to look in general, never a specific obligation, § or authority decision, and never acting
on the reader's behalf. It is read only by the claim-enforcement path, which has its own feature
flag.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pythia.reasoning.gap_kind import GapKind

# Citation-like tokens to strip from a provision hint before it enters gap copy. A bare "§ 297" or a
# "240/2013" act number in a gap line would be picked up by verify.py's grounding scan and could
# count toward the 0.34 floor. Only the human title fragment remains, e.g. "zákona o ..." with no §
# or act number.
_CIT_TOKEN_RE = re.compile(
    r"(?:§\s*\d+[a-z]?|\bčl\.?\s*\d+|\bart(?:icle)?\.?\s*\d+|\b\d{1,4}/\d{4}\b|"
    r"\b[0-9]{5}[A-Z][0-9]{4}\b)",
    re.I,
)


def _safe_hint(hint: str) -> str:
    """Remove citation-like tokens from a provision hint and tidy what is left."""
    out = _CIT_TOKEN_RE.sub("", hint or "")
    out = re.sub(r"[\s,;.]+", " ", out).strip(" -–—,;.")
    return out


# gap_source values: one per failure class reassembly can emit. Stable wire strings; add new ones,
# never rename.
GAP_SOURCES: tuple[str, ...] = (
    "data_absent",  # the corpus never had the data (not a verification failure)
    "vf_deadline",  # a deadline claim failed V-FACT or V-CIT (not verifiable verbatim)
    "vf_amount",  # an amount claim failed V-FACT or V-CIT
    "vf_authority",  # an authority claim failed V-FACT or V-CIT
    "vf_procedure_step",  # a procedure_step claim failed V-CIT
    "vf_legal_rule",  # a legal_rule claim failed V-CIT
    "vf_sanction",  # a sanction claim failed V-CIT
    "vf_situation",  # a situation_application claim failed V-PROF
    "vf_calculation",  # a computed value failed V-CALC (verbatim-only rule)
    "vf_citation",  # uncited, or cited but unsupported (generic V-CIT failure)
    "vf_safety",  # a V-SAFE failure (reserved advice, temporal); dropped silently, no visible line
    "vf_unchecked",  # every check skipped (checker crash or bad return); never treated as verified
)

# Claim type to gap_source, used when a claim is demoted for a verification failure. The failing
# check can override it (see _CHECK_TO_SOURCE).
_TYPE_TO_SOURCE: dict[str, str] = {
    "deadline": "vf_deadline",
    "amount": "vf_amount",
    "authority": "vf_authority",
    "procedure_step": "vf_procedure_step",
    "legal_rule": "vf_legal_rule",
    "sanction": "vf_sanction",
    "situation_application": "vf_situation",
}

# Failing check to gap_source, consulted before _TYPE_TO_SOURCE. The check is the stronger signal: a
# V-CALC failure is a computation failure whatever the claim type, and a V-SAFE failure is always a
# safety drop.
_CHECK_TO_SOURCE: dict[str, str] = {
    "v_calc": "vf_calculation",
    "v_safe": "vf_safety",
    "v_prof": "vf_situation",
}

# Draft Czech gap copy pending lawyer review, keyed by gap_source. {hint} is the cited
# human-readable label (never invented); when empty, the hint clause is omitted. No copy contains a
# `§ n` or `n/YYYY` pattern.
_COPY: dict[str, str] = {
    "vf_deadline": (
        "Přesnou lhůtu se z korpusu nepodařilo ověřit; ověřte ji prosím přímo v uvedeném předpisu{hint} "
        "nebo u příslušného úřadu."
    ),
    "vf_amount": (
        "Přesnou částku se z korpusu nepodařilo ověřit; ověřte ji prosím přímo v uvedeném předpisu{hint} "
        "nebo u příslušného úřadu."
    ),
    "vf_authority": (
        "Konkrétní příslušný úřad se z korpusu nepodařilo spolehlivě určit; ověřte jej prosím na portálu "
        "veřejné správy nebo u dotčeného orgánu."
    ),
    "vf_procedure_step": (
        "Jeden z kroků postupu se nepodařilo dostatečně podložit korpusem a byl vynechán; úplný postup "
        "prosím ověřte u příslušného úřadu."
    ),
    "vf_legal_rule": (
        "K tomuto bodu se nepodařilo dohledat dostatečně podložené ustanovení; informaci prosím ověřte "
        "u příslušného úřadu nebo právníka."
    ),
    "vf_sanction": (
        "Konkrétní výši či druh sankce se z korpusu nepodařilo ověřit; ověřte ji prosím u příslušného úřadu."
    ),
    "vf_situation": (
        "Uplatnění pravidla na vaši situaci se nepodařilo spolehlivě potvrdit z uvedených údajů; posouzení "
        "prosím ověřte u právníka."
    ),
    "vf_calculation": (
        "Uvedená hodnota vychází z výpočtu, který korpus přímo nepotvrzuje; konkrétní hodnotu prosím ověřte "
        "v uvedeném předpisu{hint} nebo u příslušného úřadu."
    ),
    "vf_citation": (
        "K tomuto tvrzení se nepodařilo dohledat podložené ustanovení; informaci prosím ověřte u příslušného "
        "úřadu nebo právníka."
    ),
    "vf_unchecked": (
        "Toto tvrzení se nepodařilo automaticky ověřit; informaci prosím ověřte v uvedeném předpisu{hint} "
        "nebo u příslušného úřadu."
    ),
}

# Machine-readable marker on every emitted gap line (same value as gap_registry.DRAFT_MARKER).
DRAFT_MARKER = "DRAFT/advocate-review-pending"


@dataclass
class GapLine:
    """One honest gap line that reassembly inserts in place of a demoted claim.

    ``gap_key`` is ``(slot, gap_source)``, so a slot that loses several claims of the same kind gets
    one line. ``copy`` is the rendered draft Czech line (never citation-like). ``provision_hint`` is
    the cited human-readable label, or "" (never invented). ``gap_kind`` is the independent
    missing-material code (default UNKNOWN).
    """

    gap_key: tuple[str, str]
    gap_source: str
    slot: str
    copy: str
    provision_hint: str = ""
    gap_kind: str = field(default=GapKind.UNKNOWN.value)
    status: str = DRAFT_MARKER


def gap_source_for(claim_type: str, check: str | None) -> str:
    """Return the gap_source for a demoted claim.

    The failing check takes precedence over the claim type (a V-CALC, V-SAFE or V-PROF failure
    defines the source); otherwise the claim type maps to its ``vf_*`` value, and an unknown type
    falls back to ``vf_citation``.
    """
    if check and check in _CHECK_TO_SOURCE:
        return _CHECK_TO_SOURCE[check]
    return _TYPE_TO_SOURCE.get(claim_type, "vf_citation")


def is_safety_drop(gap_source: str) -> bool:
    """Return True for the silent-drop class (V-SAFE failures), which never gets a visible line.

    A reserved-advice or temporally inconsistent claim is cut and never repeated or explained in a
    way that would leak its content.
    """
    return gap_source == "vf_safety"


def demote(
    slot: str, claim_type: str, check: str | None, *, gap_kind: str = GapKind.UNKNOWN.value, provision_hint: str = ""
) -> GapLine | None:
    """Build the gap line for a demoted claim, or None for a silent safety drop.

    For a V-SAFE failure the caller cuts the claim and emits nothing. An unknown slot or claim type
    never raises; it falls back to the generic ``vf_citation`` line.

    ``provision_hint`` is the failed claim's cited label (e.g. '§ 297 zákona 240/2013').
    Citation-like tokens are stripped and the rest is rendered as free prose in parentheses, never
    as an ``[act § n]`` span, so the line cannot count toward the 0.34 grounding floor.
    """
    src = gap_source_for(claim_type, check)
    if is_safety_drop(src):
        return None  # V-SAFE failure: silent drop, no line
    template = _COPY.get(src, _COPY["vf_citation"])
    # Render the hint as free prose with citation-like tokens stripped, so the gap line cannot count
    # toward verify.py's 0.34 floor.
    hint = _safe_hint(str(provision_hint or ""))
    hint_clause = f" ({hint})" if hint else ""
    copy = template.format(hint=hint_clause)
    return GapLine(
        gap_key=(slot, src),
        gap_source=src,
        slot=slot,
        copy=copy,
        provision_hint=hint,
        gap_kind=str(gap_kind or GapKind.UNKNOWN.value),
    )
