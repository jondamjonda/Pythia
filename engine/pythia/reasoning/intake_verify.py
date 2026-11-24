"""Intake verifier registry: turns the raw LLM intake proposal into a safe IntakeRecord.

The intake counterpart of claim verification, applying the same cite-or-refuse discipline to query
decomposition: `validate` runs each proposed fact and sub-question through ordered, typed verifiers
and records the per-item verdicts on the record's `iv_verdicts`. Pure and idempotent (no LLM,
database, clock or config). Used only when `intake_v2_enabled` is on. Intake facts feed only the
answer path and the audit trace, never the lawyer-gated candidate and verdict machinery, and this
module never reads a metagate verdict: the metagate stays advisory, as it does for the answer-side
loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# The closed vocabulary, limits and record shapes are imported from intake.py so there is one source
# of truth; the parity assert below fails loudly at import if the vocabulary drifts from what was
# reviewed.
from pythia.reasoning.intake import (
    _FACT_FIELDS,
    _HYDE_MAX,
    _HYDE_MAXLEN,
    _QUOTE_MAXLEN,
    _SUBQ_MAX,
    _SUBQ_MAXLEN,
    _SUBQ_MIN,
    _SUFFICIENCY,
    _VALUE_MAXLEN,
    IntakeFact,
    IntakeRecord,
    _fold_ws,
)

# Closed fact-field vocabulary for IV-ENUM: intake.py's lawyer-reviewed frozenset of the SME scoping
# axes the answer path can safely read (legal_form, sector, the five compliance gates, jurisdiction,
# region, temporal), with no candidate or verdict fields. Mirrored here for readability, with an
# equality assert so drift in intake.py is caught at import instead of letting the two gates
# silently diverge.
_ENUM_VOCAB: frozenset[str] = frozenset(_FACT_FIELDS)
assert _ENUM_VOCAB == _FACT_FIELDS, "IV-ENUM vocab drifted from intake._FACT_FIELDS (parity guard)"

# Fact fields that carry a point-in-time signal; IV-TEMPORAL treats them as display-only.
_TEMPORAL_FIELDS: frozenset[str] = frozenset({"temporal"})
# Year sniff (19xx or 20xx, which also covers dd.mm.yyyy and "od 2024"), used only to surface a
# suggested_as_of in the trace; it never changes anything. A miss just means no suggestion.
_DATE_RE = re.compile(r"\b(?:19|20)\d{2}\b")

# Intake verdict statuses (shaped like the claim-ledger vocabulary, but specific to intake).
IV_STATUS: tuple[str, ...] = ("pass", "drop", "conflict", "advisory")
# Registered verifiers, each producing an IVVerdict:
#   IV-SPAN      a fact's evidence_quote must occur verbatim in the query (whitespace-folded,
#                case-insensitive). Offsets are recomputed, never taken from the model. No match
#                drops the fact: no ungrounded facts.
#   IV-ENUM      a fact's field must be in the closed vocabulary; otherwise the fact is dropped.
#   IV-CONFLICT  a query fact that conflicts with a profile fact on the same field is dropped and
#                the conflict is recorded; the profile wins.
#   IV-TEMPORAL  an extracted date is display-only: a suggested_as_of may appear in the trace but
#                never changes the request's as_of.
#   IV-LANG      a value or sub-question must be non-empty and length-bounded.
#   IV-COUNT     the sub-question list is capped at _SUBQ_MAX; fewer than _SUBQ_MIN valid ones
#                fails the whole proposal and the caller falls back to the legacy path.
IV_VERIFIERS: tuple[str, ...] = ("IV-SPAN", "IV-ENUM", "IV-CONFLICT", "IV-TEMPORAL", "IV-LANG", "IV-COUNT")


@dataclass(frozen=True)
class IVVerdict:
    """One intake verifier's outcome for one proposed item, the intake analogue of ClaimVerdict.

    `item_id` identifies the item ('fact:2', 'subq:0', 'subq_count', ...); `verifier` is one of
    IV_VERIFIERS and `status` one of IV_STATUS. `reason` is a stable code that later passes key off,
    not prose. `evidence` is a small JSON-safe dict (recomputed offsets, conflicting values,
    suggested_as_of, ...).
    """

    item_id: str
    verifier: str
    status: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {"item_id": self.item_id, "verifier": self.verifier, "status": self.status, "reason": self.reason}
        if self.evidence:
            d["evidence"] = dict(self.evidence)
        return d


# IV-SPAN: verbatim grounding in the query
def iv_span(item_id: str, quote: Any, folded_q: str) -> tuple[bool, IVVerdict]:
    """Check that a fact's evidence quote occurs verbatim in the query.

    Matching is whitespace-folded and case-insensitive. The offset is recomputed with str.find on
    the folded, lower-cased query and recorded in the verdict evidence; offsets from the model are
    never trusted. A non-string, empty or unmatched quote is not grounded, and the fact is dropped.

    Returns:
        (grounded, verdict).
    """
    if not isinstance(quote, str) or not quote.strip():
        return False, IVVerdict(item_id, "IV-SPAN", "drop", "empty_quote")
    folded = _fold_ws(quote)
    offset = folded_q.find(folded.lower())
    if offset < 0:
        return False, IVVerdict(item_id, "IV-SPAN", "drop", "quote_not_in_query", {"quote": folded[:_QUOTE_MAXLEN]})
    return True, IVVerdict(item_id, "IV-SPAN", "pass", "grounded", {"offset": offset, "length": len(folded)})


# IV-ENUM: closed-vocabulary field gate
def iv_enum(item_id: str, fld: Any) -> tuple[bool, IVVerdict]:
    """Check that a fact's field is in the closed vocabulary; otherwise the fact is dropped.

    Returns:
        (in_vocab, verdict).
    """
    if fld in _ENUM_VOCAB:
        return True, IVVerdict(item_id, "IV-ENUM", "pass", "in_vocab", {"field": fld})
    return False, IVVerdict(item_id, "IV-ENUM", "drop", "field_out_of_vocab", {"field": str(fld)[:_VALUE_MAXLEN]})


# IV-LANG: value sanity (non-empty, length-bounded)
def iv_lang_value(item_id: str, value: str) -> IVVerdict:
    """Return a soft sanity verdict on a fact value; never drops (the value is already truncated)."""
    if not value.strip():
        return IVVerdict(item_id, "IV-LANG", "advisory", "empty_value")
    return IVVerdict(item_id, "IV-LANG", "pass", "ok", {"length": len(value)})


# IV-TEMPORAL: extracted dates are display-only
def iv_temporal(item_id: str, fld: str, value: str, quote: str) -> IVVerdict | None:
    """Record a display-only `suggested_as_of` for a temporal fact or a value or quote with a year.

    Advisory only: it never changes the request's as_of, which the caller owns; the answer path may
    show the suggestion. Returns None when nothing temporal is found.
    """
    blob = f"{value} {quote}"
    m = _DATE_RE.search(blob)
    if fld not in _TEMPORAL_FIELDS and not m:
        return None
    ev: dict[str, Any] = {"display_only": True}
    if m:
        ev["suggested_as_of"] = m.group(0)
    return IVVerdict(item_id, "IV-TEMPORAL", "advisory", "date_display_only", ev)


def _clamp_subquestions(raw: dict) -> tuple[list[str], list[IVVerdict]]:
    """Apply IV-LANG and IV-COUNT to the proposed sub-questions.

    Keeps non-empty whitespace-folded strings, truncates each to _SUBQ_MAXLEN, removes
    case-insensitive duplicates (keeping order), caps the list at _SUBQ_MAX and records an IV-COUNT
    verdict on the final count. The caller decides the below-minimum failure, so it can fall back to
    the legacy path (the intake counterpart of a refusal).

    Returns:
        (sub_questions, verdicts).
    """
    verdicts: list[IVVerdict] = []
    subs: list[str] = []
    seen: set[str] = set()
    for i, s in enumerate(raw.get("sub_questions", []) or []):
        if not isinstance(s, str):
            verdicts.append(IVVerdict(f"subq:{i}", "IV-LANG", "drop", "not_a_string"))
            continue
        t = _fold_ws(s)[:_SUBQ_MAXLEN].strip()
        if not t:
            verdicts.append(IVVerdict(f"subq:{i}", "IV-LANG", "drop", "empty"))
            continue
        if t.lower() in seen:
            verdicts.append(IVVerdict(f"subq:{i}", "IV-LANG", "drop", "duplicate"))
            continue
        seen.add(t.lower())
        subs.append(t)
    dropped_over = subs[_SUBQ_MAX:]
    subs = subs[:_SUBQ_MAX]
    status = "pass" if len(subs) >= _SUBQ_MIN else "drop"
    reason = "count_ok" if status == "pass" else "below_min"
    verdicts.append(
        IVVerdict(
            "subq_count",
            "IV-COUNT",
            status,
            reason,
            {"kept": len(subs), "min": _SUBQ_MIN, "max": _SUBQ_MAX, "over_cap_dropped": len(dropped_over)},
        )
    )
    return subs, verdicts


def _validate_facts(
    raw: dict, folded_q: str, profile_by_field: dict[str, IntakeFact]
) -> tuple[list[IntakeFact], list[dict], list[IVVerdict]]:
    """Run each proposed fact through IV-ENUM, IV-SPAN, IV-LANG, IV-TEMPORAL and IV-CONFLICT.

    The first verifier that drops a fact decides. A kept fact passed IV-ENUM and IV-SPAN and is on a
    field the profile does not cover. If the profile has a different value for the field, the query
    fact is dropped and the conflict recorded (`{field, profile_value, query_value}`); with the same
    value it is dropped without a conflict. The caller appends the profile facts themselves, which
    are trusted.

    Returns:
        (kept_query_facts, conflicts, verdicts).
    """
    kept: list[IntakeFact] = []
    conflicts: list[dict] = []
    verdicts: list[IVVerdict] = []
    for i, f in enumerate(raw.get("facts", []) or []):
        item_id = f"fact:{i}"
        if not isinstance(f, dict):
            verdicts.append(IVVerdict(item_id, "IV-LANG", "drop", "not_a_dict"))
            continue
        fld = f.get("field")
        in_vocab, v_enum = iv_enum(item_id, fld)
        verdicts.append(v_enum)
        if not in_vocab:
            continue
        quote = f.get("evidence_quote")
        grounded, v_span = iv_span(item_id, quote, folded_q)
        verdicts.append(v_span)
        if not grounded:
            continue
        folded_quote = _fold_ws(quote)[:_QUOTE_MAXLEN]
        value = str(f.get("value", "")).strip()[:_VALUE_MAXLEN]
        verdicts.append(iv_lang_value(item_id, value))
        v_temporal = iv_temporal(item_id, fld, value, folded_quote)
        if v_temporal is not None:
            verdicts.append(v_temporal)
        # IV-CONFLICT: a profile fact on the same field wins and the query fact is dropped.
        pf = profile_by_field.get(fld)
        if pf is not None:
            if value.strip().lower() != pf.value.strip().lower():
                conflicts.append({"field": fld, "profile_value": pf.value, "query_value": value})
                verdicts.append(
                    IVVerdict(
                        item_id,
                        "IV-CONFLICT",
                        "conflict",
                        "profile_wins",
                        {"field": fld, "profile_value": pf.value, "query_value": value},
                    )
                )
            else:
                verdicts.append(IVVerdict(item_id, "IV-CONFLICT", "drop", "profile_same_value", {"field": fld}))
            continue  # the profile owns this field either way
        kept.append(IntakeFact(field=fld, value=value, evidence_quote=folded_quote))
    return kept, conflicts, verdicts


def _clamp_hyde(raw: dict) -> list[str]:
    """Return up to _HYDE_MAX non-empty, whitespace-folded HyDE drafts, cut to _HYDE_MAXLEN."""
    hyde: list[str] = []
    for h in raw.get("hyde_drafts", []) or []:
        if not isinstance(h, str):
            continue
        t = _fold_ws(h)[:_HYDE_MAXLEN].strip()
        if t:
            hyde.append(t)
        if len(hyde) >= _HYDE_MAX:
            break
    return hyde


def validate_det_facts(det_facts: list[IntakeFact]) -> tuple[list[IntakeFact], list[IVVerdict]]:
    """Route deterministic pre-pass facts (source='det') through the registry.

    A deterministic fact is a classification, not a copied query span, so it is exempt from IV-SPAN
    (there is no verbatim span to ground) but still subject to IV-ENUM: its field must be in the
    closed vocabulary. This is the one place that routing is decided, so deterministic facts clear
    the same enum gate as LLM facts. Pure; never raises.

    Returns:
        (kept, verdicts): in-vocabulary facts are kept and others dropped, each with an IV-ENUM
        verdict.
    """
    kept: list[IntakeFact] = []
    verdicts: list[IVVerdict] = []
    for i, f in enumerate(det_facts or []):
        item_id = f"det:{i}"
        in_vocab, v_enum = iv_enum(item_id, f.field)  # IV-ENUM only; no IV-SPAN for det facts
        verdicts.append(v_enum)
        if in_vocab:
            kept.append(f)
    return kept, verdicts


def validate(query: str, raw: dict, profile_context_facts: list[IntakeFact] | None = None) -> IntakeRecord:
    """Turn the raw LLM intake proposal into a safe IntakeRecord, recording every per-item verdict.

    The verdicts go on `iv_verdicts`, the intake counterpart of the claim ledger. Pure (no LLM) and
    idempotent: validating an already-validated proposal (in `record.as_trace()` shape) yields an
    equal record, because the verifiers only drop or truncate.

    Args:
        query: The user's question; fact quotes must be grounded in it.
        raw: The raw LLM proposal.
        profile_context_facts: Known profile facts (from intake._profile_facts, source='profile').
            They win over conflicting query facts on the same field (the conflict is recorded) and
            are appended to the record's facts as trusted givens, never subject to IV-SPAN.

    Raises:
        ValueError: if fewer than _SUBQ_MIN valid sub-questions remain, so the caller falls back to
            the legacy path (the intake counterpart of an answer-side refusal).
    """
    profile_facts = list(profile_context_facts or [])
    profile_by_field = {f.field: f for f in profile_facts}
    folded_q = _fold_ws(query).lower()

    subs, subq_verdicts = _clamp_subquestions(raw)
    if len(subs) < _SUBQ_MIN:
        raise ValueError(f"intake produced {len(subs)} valid sub_questions (need >= {_SUBQ_MIN})")

    juris = [j for j in (raw.get("jurisdiction", []) or []) if j in ("CZ", "EU")] or ["CZ", "EU"]
    matter = str(raw.get("matter_type", "unknown")).strip()[:80] or "unknown"

    suff = raw.get("sufficiency")
    sufficiency = suff if suff in _SUFFICIENCY else "sufficient"

    query_facts, conflicts, fact_verdicts = _validate_facts(raw, folded_q, profile_by_field)
    hyde = _clamp_hyde(raw)

    all_verdicts = [*subq_verdicts, *fact_verdicts]
    facts = query_facts + profile_facts  # profile wins: its fields were excluded from query_facts

    return IntakeRecord(
        sub_questions=subs,
        jurisdiction=juris,
        matter_type=matter,
        hyde_drafts=hyde,
        facts=facts,
        sufficiency=sufficiency,
        fell_back=False,
        profile_conflicts=conflicts,
        iv_verdicts=[v.as_dict() for v in all_verdicts],
    )
