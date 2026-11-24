"""Conduit output envelope and `finalize()`, the single citation chokepoint.

Every tool and resource response is its `data` wrapped in an `Envelope`, and `finalize()` is the
one place that enforces the citation rule: a response that carries data must carry at least one
citation, otherwise it is refused. This mirrors the database CHECK on
`finding.cited_provisions` and `EmptyCitationError`; Conduit does not write findings, so the
guarantee is enforced at the response boundary instead of on a row. The answer mode is always
"information", and the disclaimer is stamped on every response, refused or not.

`finalize()` is pure (no database, network or settings access), so it can be unit-tested with
the Conduit service off. It reuses the engine's citation verifier,
`regsuper.analysis.citation_check.verify_citation`, to re-check supporting spans.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from regsuper.analysis.citation_check import verify_citation

_WP = "WP-062 (Stage-2 Conduit build)"

# The answer mode is fixed: Conduit provides information, never advice.
LANE_INFORMATION = "information"

# The disclaimer stamped on every envelope; {as_of} is filled in by finalize().
DISCLAIMER_TEMPLATE = (
    "General legal information, not legal advice (advokátní služba). "
    "Not a substitute for an advokát. In force as of {as_of}."
)


class RefusalReason(StrEnum):
    """The closed set of values for the envelope's `refusal_reason`.

    External integrators may switch on these values, so the set is append-only: existing
    values never change.
    """

    RESERVED_ADVICE = "reserved_advice"
    LOW_CONFIDENCE = "low_confidence"
    NO_COVERAGE = "no_coverage"
    OUT_OF_SCOPE = "out_of_scope"
    AS_OF_OUT_OF_RANGE = "as_of_out_of_range"
    TIMEOUT = "timeout"
    GUARD_UNAVAILABLE = "guard_unavailable"
    # The question gate found a false premise; the response carries a grounded correction.
    FALSE_PREMISE = "false_premise"
    # The question needs clarifying. This is not a refusal (refused stays False and the
    # clarifying questions are included), but exposing a reason lets integrators render a
    # form that collects the missing details instead of an answer.
    NEEDS_CLARIFICATION = "needs_clarification"


@dataclass(slots=True)
class Citation:
    """One citation; an envelope with `data` must carry at least one.

    `provision_id` is the stable internal id. The other fields are the human-facing act,
    section label and effective dates, plus a deep link that lets a reader verify the
    citation.
    """

    provision_id: str
    external_id: str | None = None  # e.g. '240/2013'
    label: str | None = None  # e.g. '§ 15 odst. 2'
    act_title: str | None = None
    legal_hierarchy: str | None = None  # eu-regulation|eu-directive|cz-act|cnb-vyhlaska|...
    jurisdiction: str | None = None
    effective_from: str | None = None  # provision.valid_from
    effective_to: str | None = None  # provision.valid_to (None = in force)
    binding_note: str | None = None  # from transposition_map
    url: str | None = None  # deep link


@dataclass(slots=True)
class Trust:
    """The envelope's `trust` block: grounding and calibrated confidence."""

    in_force_as_of: str | None = None
    confidence: float | None = None  # calibrated rerank top score (0..1) or None
    confidence_source: str | None = None  # 'calibrated' | 'fallback'
    grounding: str | None = None  # 'verified' | 'fuzzy' | 'unverified'


@dataclass(slots=True)
class Envelope:
    """The output envelope that wraps every tool's `data`.

    `lane` is always `LANE_INFORMATION` and `disclaimer` is always set. When `refused` is True,
    `data` is None and `refusal_reason` is set. `citations` must be non-empty whenever `data`
    is present; `finalize` enforces this.
    """

    lane: str = LANE_INFORMATION
    refused: bool = False
    needs_lawyer: bool = False
    refusal_reason: RefusalReason | None = None
    as_of: str | None = None
    jurisdiction: list[str] = field(default_factory=list)
    disclaimer: str = ""
    citations: list[Citation] = field(default_factory=list)
    trust: Trust = field(default_factory=Trust)
    data: dict[str, object] | None = None
    meta: dict[str, object] = field(default_factory=dict)


# Verdicts that count a span as citation-backed (`verify_citation` returns one of verified,
# fuzzy, not_found or empty).
_SPAN_KEPT_VERDICTS = frozenset({"verified", "fuzzy"})


def _disclaimer_for(as_of: str | None) -> str:
    """Render the disclaimer with the resolved `as_of` date.

    `as_of` may be absent (a refusal that never resolved a date); the slot is then filled with
    'today', so a literal '{as_of}' placeholder never reaches a host model.
    """
    return DISCLAIMER_TEMPLATE.format(as_of=as_of or "today")


def finalize(
    data: dict[str, object] | None,
    citations: list[Citation],
    *,
    refused: bool = False,
    refusal_reason: RefusalReason | None = None,
    needs_lawyer: bool = False,
    as_of: str | None = None,
    jurisdiction: list[str] | None = None,
    trust: Trust | None = None,
    meta: dict[str, object] | None = None,
    provision_texts: dict[str, str] | None = None,
) -> Envelope:
    """Wrap a tool result in an envelope, refusing any data that lacks citations.

    Every Conduit tool response passes through here. Pure: no database, network or settings
    access. In order:

    1. If an upstream guard already refused (`refused=True`), return a refusal envelope:
       `data` is forced to None, `citations` may be empty, and `refusal_reason` is kept
       (defaulting to RESERVED_ADVICE only if unset, so a refusal always has a reason).
    2. If `data` is present but `citations` is empty, refuse with NO_COVERAGE. An uncited
       claim is never emitted.
    3. For `answer_legal_question` only (recognised by a `supporting_spans` list in `data`),
       re-run `verify_citation` on each span against its cited provision text from
       `provision_texts`. Spans whose verdict is not verified or fuzzy are dropped, as are
       spans with no provision text to check against (they cannot be verified). If every span
       fails, refuse with NO_COVERAGE.
    4. Stamp `lane='information'` and the disclaimer on every path, refused or not.

    Args:
        data: The tool-specific payload, or None when the caller already refused.
        citations: The citations; must be non-empty for `data` to survive.
        refused: True when an upstream guard already refused.
        refusal_reason: The reason; defaults to RESERVED_ADVICE for an upstream refusal
            without one, so every refusal carries a machine-readable reason.
        needs_lawyer: True when the advice boundary was hit (hand-off to a lawyer).
        as_of: The date the answer is valid for; also shown in the disclaimer.
        jurisdiction: The jurisdictions actually searched.
        trust: The grounding and confidence block (defaults to an empty `Trust`).
        meta: Per-request metadata (request_id, tool, latency_ms, ...).
        provision_texts: Maps provision_id to verbatim provision text. Used only for the
            span re-check in step 3; other tools carry no `supporting_spans`.
    """
    jurisdiction = list(jurisdiction) if jurisdiction else []
    trust = trust if trust is not None else Trust()
    meta = dict(meta) if meta is not None else {}
    disclaimer = _disclaimer_for(as_of)

    def _refuse(reason: RefusalReason, *, lawyer: bool) -> Envelope:
        return Envelope(
            lane=LANE_INFORMATION,
            refused=True,
            needs_lawyer=lawyer,
            refusal_reason=reason,
            as_of=as_of,
            jurisdiction=jurisdiction,
            disclaimer=disclaimer,
            citations=[],  # a refusal emits no claim, so no citation is asserted
            trust=trust,
            data=None,
            meta=meta,
        )

    # Step 1: an upstream guard already refused.
    if refused:
        reason = refusal_reason if refusal_reason is not None else RefusalReason.RESERVED_ADVICE
        return _refuse(reason, lawyer=needs_lawyer)

    # Step 2: data requires at least one citation (never an uncited claim).
    if data is not None and not citations:
        return _refuse(RefusalReason.NO_COVERAGE, lawyer=needs_lawyer)

    # Step 3: per-span citation re-check for answer_legal_question, recognised by a
    # `supporting_spans` list in the payload.
    if data is not None and isinstance(data.get("supporting_spans"), list):
        texts = provision_texts or {}
        raw_spans = data["supporting_spans"]
        kept: list[object] = []
        for span in raw_spans:
            if not isinstance(span, dict):
                continue  # malformed span: drop it (fail closed)
            pid = span.get("provision_id")
            span_text = span.get("text")
            provision_text = texts.get(pid) if isinstance(pid, str) else None
            if not provision_text:
                continue  # no provision text to verify against: drop it (fail closed)
            verdict = verify_citation(span=span_text, provision_text=provision_text)
            if verdict in _SPAN_KEPT_VERDICTS:
                # Record the verdict as the span's grounding, so hosts can see it.
                span = {**span, "grounding": verdict}
                kept.append(span)
        if raw_spans and not kept:
            # Every span failed verification: refuse the whole answer.
            return _refuse(RefusalReason.NO_COVERAGE, lawyer=needs_lawyer)
        # Keep only the verified or fuzzy spans; unsupported spans are dropped.
        data = {**data, "supporting_spans": kept}

    # Step 4: stamp the lane and disclaimer and return a passing envelope.
    return Envelope(
        lane=LANE_INFORMATION,
        refused=False,
        needs_lawyer=needs_lawyer,
        refusal_reason=None,
        as_of=as_of,
        jurisdiction=jurisdiction,
        disclaimer=disclaimer,
        citations=list(citations),
        trust=trust,
        data=data,
        meta=meta,
    )


__all__ = [
    "DISCLAIMER_TEMPLATE",
    "LANE_INFORMATION",
    "Citation",
    "Envelope",
    "RefusalReason",
    "Trust",
    "finalize",
]
