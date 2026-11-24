"""Claim-ledger schema: the data shapes and vocabularies of the claim-verification loop.

The atomizer (claims.py) builds `Claim`s, the orchestrator (claim_verify.py) builds `ClaimVerdict`s,
and `build_ledger` folds both into the `audit_trace["claim_ledger"]` payload read by the metrics
counters, the canonical-JSON export, lawyer review and the EU AI Act record-keeping export. This is
the only ledger schema: consumers derive their counters from the statuses defined here instead of
redeclaring them. Pure and deterministic: dataclasses and the standard library only; no LLM,
database, clock or config.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

# Controlled vocabularies. They are versioned: a rename must break loudly downstream.
#
# VERDICT_STATUS (ClaimVerdict.status):
#   pass      the check affirmatively verified the claim.
#   fail      the check found a defect; the repair step keys off the reason code.
#   skip      the check reached no verdict (not implemented, or the checker raised). Never a
#             silent pass; enforce mode treats skip conservatively.
#   advisory  a soft signal (e.g. support could not be entailment-verified); kept, not a hard fail.
# FINAL_STATUS (ClaimLedgerRow.final_status): in shadow mode every row is 'shadow' (nothing is
#   demoted or repaired, so the output is unchanged). The other five are enforce outcomes from the
#   repair loop.
# VERIFICATION_CLASS (ClaimLedgerRow.verification_class): whether a claim's surviving support is
#   deterministic or only advisory, which keeps the enforce honesty condition visible (with the
#   entailment lane off, prose support is only "advisory-plausible").
VERDICT_STATUS: tuple[str, ...] = ("pass", "fail", "skip", "advisory")
FINAL_STATUS: tuple[str, ...] = (
    "verified",
    "repaired_verified",
    "demoted_gap",
    "dropped_safety",
    "dropped_unverifiable",
    "shadow",
)
VERIFICATION_CLASS: tuple[str, ...] = ("deterministic", "advisory", "unverified")

# Claim types. 'gap' is an honest no-coverage line: never verified and never a fabrication risk. The
# atomizer stamps one of these on every claim.
CLAIM_TYPE: tuple[str, ...] = (
    "deadline",
    "amount",
    "authority",
    "sanction",
    "procedure_step",
    "legal_rule",
    "situation_application",
    "gap",
)

# Check names. The orchestrator routes each claim type to an ordered subset of these.
CHECK_NAME: tuple[str, ...] = ("v_cit", "v_fact", "v_calc", "v_safe", "v_prof")

# Ledger schema version, stamped by to_audit_dict() so downstream consumers can check it.
LEDGER_SCHEMA_VERSION: str = "claim_ledger_v1"


@dataclass
class Claim:
    """An atomized, verbatim span of the drafted answer.

    `text` is exactly `draft[span[0]:span[1]]`; the atomizer enforces this. `chips` are the
    citations_map entries whose span falls inside this claim (the sections.py span-containment
    rule). `normative` is False for headings, blank lines and disclaimer boilerplate, which tile the
    draft but assert nothing verifiable.
    """

    id: str
    span: tuple[int, int]
    text: str
    slot: str  # section kind or action-plan slot it came from
    type: str  # one of CLAIM_TYPE
    type_source: str  # how `type` was set: "slot_default", "regex" or "llm_assist"
    chips: list[dict] = field(default_factory=list)
    normative: bool = True


@dataclass
class ClaimVerdict:
    """One verifier's outcome for one claim.

    `reason` is a stable code that the repair step keys off, not free prose. `evidence` is a small,
    verifier-specific, JSON-safe dict (provision_id, quote_span, fact_id, pattern_id, expected,
    found, ...).
    """

    check: str  # one of CHECK_NAME
    status: str  # one of VERDICT_STATUS
    reason: str  # stable code (e.g. 'not_implemented', 'fact_token_not_in_source')
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClaimLedgerRow:
    """One claim with all its verdicts and its resolved final status ('shadow' in shadow mode)."""

    claim: Claim
    verdicts: list[ClaimVerdict] = field(default_factory=list)
    final_status: str = "shadow"  # one of FINAL_STATUS
    verification_class: str = "unverified"  # one of VERIFICATION_CLASS
    # Intake sub-questions and facts the claim's content overlaps (see _claim_intake_provenance).
    sq_ids: list[str] = field(default_factory=list)
    intake_fact_ids: list[str] = field(default_factory=list)


def _verification_class(verdicts: list[ClaimVerdict]) -> str:
    """Classify a row's support from its verdicts: 'deterministic', 'advisory' or 'unverified'.

    At least one pass makes it 'deterministic'; otherwise any advisory signal makes it 'advisory'. A
    row whose checks all skipped is, honestly, 'unverified'.
    """
    if any(v.status == "pass" for v in verdicts):
        return "deterministic"
    if any(v.status == "advisory" for v in verdicts):
        return "advisory"
    return "unverified"


# Intake schema version recorded in the ledger header (mirrors the version in intake.as_trace).
INTAKE_SCHEMA_VERSION: int = 1

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _content_tokens(text: str) -> set[str]:
    """Return the lower-cased words of four or more letters in `text`.

    Used only for the intake-provenance overlap heuristic, never as a verification signal.
    """
    return {w.lower() for w in _WORD_RE.findall(text or "") if len(w) >= 4}


def _claim_intake_provenance(claim: Claim, intake: dict | None) -> tuple[list[str], list[str]]:
    """Derive (sq_ids, intake_fact_ids) for a claim from the intake trace dict.

    `sq_ids` are the ids ``sq{i}`` of sub-questions sharing at least two content tokens with the
    claim; `intake_fact_ids` are the ``field`` names of intake facts whose value appears
    (case-folded) in the claim text. Returns ([], []) when there is no intake, leaving those ledger
    fields empty. This adds no verification signal; it only records where a claim's content came
    from, for lawyer review.
    """
    if not isinstance(intake, dict):
        return [], []
    claim_toks = _content_tokens(getattr(claim, "text", "") or "")
    sq_ids: list[str] = []
    for i, sq in enumerate(intake.get("sub_questions") or []):
        if len(_content_tokens(str(sq)) & claim_toks) >= 2:
            sq_ids.append(f"sq{i}")
    text_low = (getattr(claim, "text", "") or "").lower()
    fact_ids: list[str] = []
    for f in intake.get("facts") or []:
        val = str(f.get("value") or "").strip().lower()
        fid = str(f.get("field") or "")
        if val and fid and val in text_low and fid not in fact_ids:
            fact_ids.append(fid)
    return sq_ids, fact_ids


def build_ledger(
    claims: list[Claim],
    verdicts_by_claim: dict[str, list[ClaimVerdict]],
    mode: str,
    *,
    intake: dict | None = None,
    pre_assembly_draft: str | None = None,
) -> dict[str, Any]:
    """Fold atomized claims and their verdicts into the ledger payload.

    In shadow mode (`mode` other than "enforce") every row's final_status is 'shadow': nothing is
    demoted or repaired and the drafted answer is untouched. Enforce-mode rows also start as
    'shadow' here; the enforce path then stamps each row's real status from the repair loop and
    refreshes the histogram with `recompute_per_status`.

    Returns:
        `{"rows": [...], "summary": {...}}`. Rows are ClaimLedgerRow objects. The summary carries
        the counters /metrics and eval read: claims_total, normative_claims, mode, per_status (the
        final_status histogram) and per_check (`{check: {pass, fail, skip, advisory}}`), plus the
        SHA-256 of the pre-assembly draft and the schema version pins.
    """
    shadow = mode != "enforce"
    rows: list[ClaimLedgerRow] = []
    for c in claims:
        vs = list(verdicts_by_claim.get(c.id, []))
        sq_ids, fact_ids = _claim_intake_provenance(c, intake)
        row = ClaimLedgerRow(
            claim=c,
            verdicts=vs,
            final_status="shadow" if shadow else _enforce_placeholder_status(vs),
            verification_class=_verification_class(vs),
            sq_ids=sq_ids,
            intake_fact_ids=fact_ids,
        )
        rows.append(row)

    # Histogram of final_status, keyed by the FINAL_STATUS vocabulary.
    per_status: dict[str, int] = {s: 0 for s in FINAL_STATUS}
    for r in rows:
        per_status[r.final_status] = per_status.get(r.final_status, 0) + 1

    # Per-check tallies of pass / fail / skip / advisory across the whole loop.
    per_check: dict[str, dict[str, int]] = {ck: {st: 0 for st in VERDICT_STATUS} for ck in CHECK_NAME}
    for r in rows:
        for v in r.verdicts:
            bucket = per_check.setdefault(v.check, {st: 0 for st in VERDICT_STATUS})
            bucket[v.status] = bucket.get(v.status, 0) + 1

    return {
        "rows": rows,
        "summary": {
            "claims_total": len(rows),
            "normative_claims": sum(1 for r in rows if r.claim.normative),
            "mode": "shadow" if shadow else "enforce",
            "per_status": per_status,
            "per_check": per_check,
            # Provenance header for lawyer review: the SHA-256 of the pre-assembly draft (None when
            # the caller does not supply it) and the schema versions the reviewer checks the record
            # against.
            "pre_assembly_draft_sha256": (
                hashlib.sha256(pre_assembly_draft.encode("utf-8")).hexdigest()
                if isinstance(pre_assembly_draft, str)
                else None
            ),
            "version_pins": {
                "ledger_schema": LEDGER_SCHEMA_VERSION,
                "intake_schema": INTAKE_SCHEMA_VERSION,
            },
        },
    }


def recompute_per_status(rows: list[ClaimLedgerRow]) -> dict[str, int]:
    """Recompute the final_status histogram from the rows' current final_status.

    The enforce path overwrites each row's final_status after `build_ledger` has counted the
    placeholder statuses, so the summary's per_status must be refreshed before /metrics and eval
    read it.
    """
    per_status: dict[str, int] = {s: 0 for s in FINAL_STATUS}
    for r in rows:
        per_status[r.final_status] = per_status.get(r.final_status, 0) + 1
    return per_status


def _enforce_placeholder_status(verdicts: list[ClaimVerdict]) -> str:
    """Return the placeholder final_status for enforce-mode rows, which is always 'shadow'.

    The real enforce status comes from the repair loop and is stamped onto each row afterwards.
    Until then the row reads 'shadow', so the ledger never claims a 'verified' the loop has not
    earned.
    """
    return "shadow"


# Row provenance: where each claim, and the evidence of each of its checks, came from. It adds no
# new signal: every value is read from data the atomizer and verifiers already computed (the claim's
# slot and chips, and each ClaimVerdict.evidence). It never re-runs a verifier, calls an LLM or the
# database, or raises on missing evidence (it yields empty lists or None). Only the ledger rows gain
# this field; the served answer, sections and citations are unaffected.

# Evidence keys under which verifiers record the provision ids a check searched or used; any key
# present contributes. v_cit uses 'provision_ids' (and 'missing'), v_fact 'searched_pids', v_safe
# 'provision_id' (temporal failures). v_calc and v_prof record none, which honestly yields an empty
# list.
_EVIDENCE_PID_KEYS: tuple[str, ...] = ("provision_ids", "searched_pids", "provision_id", "missing")


def _evidence_provision_ids(evidence: dict[str, Any]) -> list[str]:
    """Return the provision ids a verdict's evidence says it searched or used.

    Ids are str-coerced and de-duplicated in first-seen order. Checks that ground on no provision
    (the v_calc token check, the v_prof profile axes) give []. Pure and total: a non-dict evidence
    or missing key gives [].
    """
    if not isinstance(evidence, dict):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for key in _EVIDENCE_PID_KEYS:
        val = evidence.get(key)
        if val is None:
            continue
        items = val if isinstance(val, (list, tuple, set)) else [val]
        for pid in items:
            if pid is None:
                continue
            p = str(pid)
            if p and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def _verdict_span(evidence: dict[str, Any]) -> str | None:
    """Return the verbatim quote a verdict used, if any (v_cit's entailment check records one)."""
    if not isinstance(evidence, dict):
        return None
    quote = evidence.get("quote")
    return quote if isinstance(quote, str) and quote else None


def _grounding_provision_ids(claim: "Claim") -> list[str]:
    """Return the distinct provision ids of the claim's chips, str-coerced, in first-seen order.

    A chip without a provision_id contributes nothing.
    """
    out: list[str] = []
    seen: set[str] = set()
    for ch in getattr(claim, "chips", None) or []:
        pid = ch.get("provision_id") if isinstance(ch, dict) else None
        if pid is None:
            continue
        p = str(pid)
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _row_provenance(row: "ClaimLedgerRow") -> dict[str, Any]:
    """Build the provenance annotation for one ledger row.

    Reads only the claim's slot and chips and each verdict's existing evidence; it never re-runs a
    verifier, calls an LLM or the database, or raises. Returns::

        {"source_slot": <the section or slot the claim came from>,
         "grounding_provision_ids": [<the chips' provision ids>],
         "checks": [{"check", "verifier", "reason", "evidence_provision_ids", "span"} per verdict]}

    A row without verdicts has checks=[]; a verdict without evidence ids has
    evidence_provision_ids=[].
    """
    c = row.claim
    checks_out: list[dict[str, Any]] = []
    for v in row.verdicts:
        ev = v.evidence if isinstance(v.evidence, dict) else {}
        checks_out.append(
            {
                "check": v.check,
                "verifier": v.check,  # verifier module that produced it (same as the check name)
                "reason": v.reason,  # stable reason code the check resolved to
                "evidence_provision_ids": _evidence_provision_ids(ev),
                "span": _verdict_span(ev),
            }
        )
    return {
        "source_slot": c.slot,
        "grounding_provision_ids": _grounding_provision_ids(c),
        "checks": checks_out,
    }


def to_audit_dict(ledger: dict[str, Any]) -> dict[str, Any]:
    """Serialize a `build_ledger` result into the JSON-safe `audit_trace["claim_ledger"]` dict.

    Dataclasses become plain dicts and the schema version is stamped so consumers can check it; no
    engine object leaks into the trace. This is the canonical shape the export and the metrics
    derive from. Each row also carries a `provenance` field (see `_row_provenance`): the source
    slot, the grounding provision ids and, per verdict, the verifier, reason, evidence provision ids
    and span.
    """
    rows_out: list[dict[str, Any]] = []
    for r in ledger.get("rows", []):
        c = r.claim
        rows_out.append(
            {
                "claim_id": c.id,
                "span": [c.span[0], c.span[1]],
                "text": c.text,
                "slot": c.slot,
                "type": c.type,
                "type_source": c.type_source,
                "normative": c.normative,
                "citations": [str(ch.get("provision_id")) for ch in c.chips if ch.get("provision_id")],
                "checks": [
                    {"check": v.check, "status": v.status, "reason": v.reason, "evidence": dict(v.evidence)}
                    for v in r.verdicts
                ],
                "final": r.final_status,
                "verification_class": r.verification_class,
                "sq_ids": list(r.sq_ids),
                "intake_fact_ids": list(r.intake_fact_ids),
                "provenance": _row_provenance(r),
            }
        )
    return {
        "schema": LEDGER_SCHEMA_VERSION,
        "summary": ledger.get("summary", {}),
        "rows": rows_out,
    }
