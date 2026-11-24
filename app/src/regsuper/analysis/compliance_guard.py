"""The compliance guard: the single gate between legal information and reserved legal advice.

Czech law reserves some legal services to advocates, so the product may give legal information but
must never act on a user's behalf. This module is the one enforcement point for that rule:
``classify_answer_mode`` (the reserved-advice classifier), ``project_refusal`` (folds refusal
signals into one decision) and the ``ComplianceVerdict`` / ``RefusalDecision`` types.

The guard fails closed. Any exception, unparseable LLM output or confidence below 0.6 yields
``needs_review`` from the classifier and ``refuse=True`` from ``project_refusal``. It never fails
open to ``information``: defaulting to information on error would let reserved advice (unauthorised
legal practice) through.

``classify_answer_mode`` takes a ``where`` argument (``"input"`` or ``"output"``) so the audit row
records which gate judged the text. Only the input gate is currently wired: the project-analyze
intake (``api/projects.py::analyze``), before any LLM spend. The output gate is supported (the
parameter, classifier and audit chain exist) but no live caller uses it yet; only unit tests do.
This module classifies and logs; whether to block is the call site's decision, governed by
``compliance_guard_mode``. In 'shadow' (the default) it annotates and logs without blocking; in
'enforce', allowed only after a lawyer's sign-off is recorded as an audit event, it short-circuits.

The business-profile analyze path (``api/profiles.py``) deliberately does not pass through this
gate: it analyses a structured profile with ``declared_intent`` None, so there is no free-text
request to classify. If a free-text intent field is ever added there, route it through
``classify_answer_mode(where="input")`` as project-analyze does.

Tier-1 is a deterministic Czech and English reserved-advice regex (no model call). Tier-2 asks a
cheap LLM (``resolve_model(ModelRole.CHEAP)``) about the ambiguous middle only, wrapped fail-closed:
if the model or client is unavailable the result is ``needs_review``, never 'information'. The
exact Tier-1 phrasing is for the reviewing lawyer to finalise (verdicts record ``decided_by``
'rule:advice-guard-v1').
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final, Literal
from uuid import UUID

from regsuper.analysis.disclaimers import (
    DISCLAIMER_INFO,
    DISCLAIMER_INFO_LOW_CONFIDENCE,
    DISCLAIMER_RESERVED_ADVICE,
)
from regsuper.analysis.llm_clients import get_openai
from regsuper.analysis.model_resolver import ModelRole, resolve_model
from regsuper.config import get_settings
from regsuper.observability.api_tracker import tracked_chat_completion
from regsuper.storage.audit import hash_prompt, log_event
from regsuper.storage.database import session_scope

_IMPL_WP: Final[str] = "WP-033 / B2b-02"

# Confidence floor: a Tier-2 verdict below this (or any error or unparseable output) fails closed
# to needs_review.
_CONF_FLOOR: Final[float] = 0.6

AnswerModeLiteral = Literal["information", "reserved_advice", "needs_review"]


class AnswerMode(str, Enum):
    """The three answer modes. The ``str`` mixin makes members serialise as their value."""

    INFORMATION = "information"
    RESERVED_ADVICE = "reserved_advice"
    NEEDS_REVIEW = "needs_review"  # the fail-closed target


FAIL_CLOSED_ANSWER_MODE: Final[AnswerModeLiteral] = "needs_review"


# Tier-1: deterministic Czech and English reserved-advice signals, scoped to the advocate's
# determination (June 2026). The only forbidden class is the product acting on behalf of the user:
# representing them, or filing, submitting, lodging or suing for them. (That is also technically
# impossible: the product produces information and synthesis and has no agent that files or
# appears.) Everything else is allowed as information with the "this is not advice" disclaimer:
# summaries, step-by-step procedures, court preparation, drafting a document, and advice on the
# user's own matter. Those are deliberately not matched here. Only high-precision markers belong
# here; a true act-on-behalf request that slips past is caught by Tier-2 and the fail-closed path.
#
# The Czech object pronoun "me" appears as "mě", "me" and the formal or older "mne" ("Zastupte mne
# u soudu"); the object can be separated from the verb ("Podejte tu žalobu za mě"); and "mým
# jménem" ("in my name") can follow the verb ("Podejte žalobu mým jménem") as well as precede it.
# The patterns cover all three forms. Each extension only broadens detection: anything matched
# before still matches.
_RESERVED_ADVICE_RE = re.compile(
    r"(?:"
    # Czech: represent me, act or file for me, in my name. Stem-based, so imperative, question,
    # wish and conditional forms all match. Object pronoun: mě, me or mne.
    r"(?:zastup|zastoup|reprezent)\w*\s+m(?:ě|e|ne)\b"  # zastupte/zastupovat/zastoupit/... mě|mne
    # Object first: "mě (u soudu) zastupovat".
    r"|m(?:ě|e|ne)\s+(?:\w+\s+){0,3}(?:zastup|zastoup|reprezent)\w*"
    r"|právní\s+zastoupen"
    r"|zastoupen[íi]\s+(?:u\s+soud|před\s+soud|v\s+(?:soudním\s+)?řízení)"
    # File, submit, draft or fill in for me, allowing an object between the verb and "za mě".
    r"|(?:podej(?:te|íte)?|podat|pode[jš]le|sepi[šs](?:te)?|sepsat|vypl[ňn](?:te|it)?)\b[^.?!\n]{0,40}\bza\s+m(?:ě|e|ne)\b"
    # In my name, with the verb either after or before "mým jménem".
    r"|m(?:ým|ym)\s+jménem\s+(?:podej|podat|zastup|jednej|jednat|reprezent)"
    r"|(?:podej(?:te|íte)?|podat|zastup\w*|jednej(?:te)?|jednat|reprezent\w*|sepi[šs](?:te)?|sepsat)\b[^.?!\n]{0,40}\bm(?:ým|ym)\s+jménem\b"
    r"|jednej(?:te)?\s+za\s+m(?:ě|e|ne)\b"
    # English: represent, act, file, submit, sue or appear for me, or on my behalf.
    r"|represent\s+me\b"
    r"|legal\s+representation\b"
    r"|on\s+my\s+behalf\b"
    r"|act\s+for\s+me\b"
    r"|(?:file|lodge|submit|sue|appear)\b[^.?!\n]{0,40}\bfor\s+me\b"
    r")",
    re.IGNORECASE,
)


# Tier-2 prompt for the ambiguous middle; its hash is computed at import, like L2's PROMPT_HASH. It
# follows the same advocate determination: the only reserved class is the product acting on behalf
# of the user. Summaries, procedures, court preparation, drafting and advice on the user's own
# matter are allowed as information, covered by the "this is not advice" disclaimer.
ADVICE_GUARD_PROMPT = """You are a compliance gate for a Czech legal-INFORMATION product. Classify the user
message into exactly one of two classes.

RESERVED_ADVICE — and ONLY this: the user asks the product to ACT ON THEIR BEHALF in a legal matter — to
REPRESENT them, or to FILE / SUBMIT / LODGE / SERVE / SUE / APPEAR / sign on their behalf or in their name
(an agency action reserved to advocates, which this product cannot and must not perform).

INFORMATION — everything else, including: an abstract question about what the law says/requires; a SUMMARY or
synthesis; a STEP-BY-STEP procedure on how to do something; PREPARING the user for a court session; DRAFTING a
document for the user; and ADVICE on the user's OWN matter or dispute. These are ALL information (the product
adds a brief "this is not legal advice" notice) — they are NOT reserved. Do NOT mark advice-on-own-matter,
drafting, or court-prep as reserved; only an explicit "do it / file it / represent me" agency request is.

MESSAGE:
<<<
{text}
>>>

OUTPUT (JSON only): {{"answer_mode": "information" | "reserved_advice", "confidence": 0.0-1.0, "reason": "<short>"}}
When unsure, give a LOWER confidence — the caller fails closed to human review below 0.6."""

GUARD_PROMPT_HASH: Final[str] = hash_prompt(ADVICE_GUARD_PROMPT)


@dataclass(frozen=True)
class ComplianceVerdict:
    """The answer-mode decision for one piece of text."""

    answer_mode: AnswerModeLiteral
    reason: str
    decided_by: str  # 'rule:advice-guard-v1' | '<model>:advice-guard-v1'
    matched_signals: list[str] = field(default_factory=list)
    confidence: float = 0.0
    prompt_hash: str | None = None


@dataclass(frozen=True)
class RefusalDecision:
    """The combined refusal decision for one answer surface."""

    refuse: bool
    flagged: bool
    reason: str | None = None
    source: str | None = None
    threshold: float | None = None


def disclaimer_for(answer_mode: AnswerModeLiteral, *, confidence: float = 1.0) -> str:
    """Return the stable disclaimer id that must accompany an answer mode and confidence.

    Recording the id with each verdict lets the audit trail prove which disclaimer was shown.
    reserved_advice maps to the refusal block; needs_review and low-confidence information map
    to the heightened-caveat block; everything else gets the normal information block.
    """
    if answer_mode == "reserved_advice":
        return DISCLAIMER_RESERVED_ADVICE
    if answer_mode == "needs_review" or confidence < _CONF_FLOOR:
        return DISCLAIMER_INFO_LOW_CONFIDENCE
    return DISCLAIMER_INFO


async def classify_answer_mode(
    *,
    text: str,
    project_id: Any | None = None,
    org_id: Any | None = None,
    where: Literal["input", "output"],
) -> ComplianceVerdict:
    """Classify ``text`` into an answer mode at the ``input`` or ``output`` gate.

    A Tier-1 regex match means reserved_advice; otherwise the Tier-2 cheap LLM judges the
    ambiguous middle. Fails closed: any exception, unparseable output or confidence below 0.6
    gives needs_review. Writes exactly one append-only ``compliance.answer_mode.judged`` audit row
    carrying ``where`` and the disclaimer id. Never raises, because a crash that propagated could
    fail open.

    ``org_id`` (optional) stamps the audit row's tenant column so the row passes row-level
    security's WITH CHECK when RLS is enforced (an ``audit_event`` INSERT with a NULL org as
    ``regsuper_app`` is rejected). It defaults to None; the ``projects.py`` input gate should pass
    ``org_id=_proj.org_id``.
    """
    verdict = await _classify(text=text, project_id=project_id, org_id=org_id)
    try:  # one audit row per classification; the write must never break the gate
        async with session_scope() as session:
            await log_event(
                session,
                actor_type=("rule" if verdict.decided_by.startswith("rule") else "llm"),
                actor_id=verdict.decided_by,
                action="compliance.answer_mode.judged",
                target_type="answer_mode",
                target_id=str(where),
                org_id=org_id,
                project_id=project_id if isinstance(project_id, UUID) else None,
                prompt_hash=verdict.prompt_hash,
                payload={
                    "where": where,
                    "answer_mode": verdict.answer_mode,
                    "confidence": verdict.confidence,
                    "reason": verdict.reason,
                    "matched_signals": verdict.matched_signals,
                    "disclaimer_id": disclaimer_for(
                        verdict.answer_mode, confidence=verdict.confidence
                    ),
                    "mode": get_settings().compliance_guard_mode,
                    "text_hash": hash_prompt(text or ""),
                },
            )
    except Exception:
        pass
    return verdict


async def _classify(
    *, text: str, project_id: Any | None, org_id: Any | None = None
) -> ComplianceVerdict:
    raw = (text or "").strip()
    if not raw:
        # An empty message is not a reserved-advice request, but we cannot affirm 'information'
        # either, so send it to review.
        return ComplianceVerdict(
            answer_mode="needs_review",
            reason="empty input",
            decided_by="rule:advice-guard-v1",
            confidence=1.0,
        )

    # Tier 1: deterministic, no model call. A reserved-advice signal is decisive.
    m = _RESERVED_ADVICE_RE.search(raw)
    if m:
        return ComplianceVerdict(
            answer_mode="reserved_advice",
            reason="reserved-advice signal (Tier-1)",
            decided_by="rule:advice-guard-v1",
            matched_signals=[m.group(0).strip()[:80]],
            confidence=0.95,
        )

    # Tier 2: cheap LLM on the ambiguous middle. Any failure (model or client unavailable,
    # unparseable JSON, unknown mode, confidence below the floor) fails closed to needs_review,
    # never 'information'.
    try:
        model = resolve_model(ModelRole.CHEAP)
        client = get_openai()
        resp = await tracked_chat_completion(
            client,
            source="analysis.compliance_guard",
            project_id=project_id if isinstance(project_id, UUID) else None,
            org_id=org_id
            if isinstance(org_id, UUID)
            else None,  # stamp the cost row's tenant so it passes RLS
            model=model,
            temperature=0.0,
            max_tokens=200,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": ADVICE_GUARD_PROMPT.format(text=raw[:4000])}],
        )
        data = json.loads((resp.choices[0].message.content or "").strip())
        mode = data["answer_mode"]
        conf = float(data["confidence"])
        if mode not in ("information", "reserved_advice") or conf < _CONF_FLOOR:
            raise ValueError(f"low-confidence/out-of-vocab: {mode!r} @ {conf}")
        return ComplianceVerdict(
            answer_mode=mode,
            reason=str(data.get("reason", ""))[:200],
            decided_by=f"{model}:advice-guard-v1",
            confidence=conf,
            prompt_hash=GUARD_PROMPT_HASH,
        )
    except Exception as exc:
        return ComplianceVerdict(
            answer_mode=FAIL_CLOSED_ANSWER_MODE,
            reason=f"fail-closed ({type(exc).__name__})",
            decided_by="rule:advice-guard-v1:fail-closed",
            confidence=1.0,
            prompt_hash=GUARD_PROMPT_HASH,
        )


def project_refusal(retrieval_result: Any, cascade_findings: Any) -> RefusalDecision:
    """Combine the retrieval ``refused``/``flagged`` signals and cascade refusals into one decision.

    This only produces the decision; acting on it is the call site's job. Fails closed: if the
    inputs cannot be evaluated it refuses (``refuse=True``) rather than let an answer through.
    Pure and synchronous (no database or LLM).
    """
    try:
        refused = bool(getattr(retrieval_result, "refused", False))
        flagged = bool(getattr(retrieval_result, "flagged", False))
        cascade_refused = False
        for f in cascade_findings or []:
            ft = getattr(f, "finding_type", None)
            if ft is None and isinstance(f, dict):
                ft = f.get("finding_type")
            if ft == "refusal":
                cascade_refused = True
                break
        if refused or cascade_refused:
            return RefusalDecision(
                refuse=True,
                flagged=flagged,
                reason=("retrieval refused" if refused else "cascade refusal"),
                source=("retrieval" if refused else "cascade"),
                threshold=getattr(retrieval_result, "threshold", None),
            )
        return RefusalDecision(refuse=False, flagged=flagged, source="none")
    except Exception as exc:
        return RefusalDecision(
            refuse=True, flagged=True, reason=f"fail-closed ({type(exc).__name__})", source="error"
        )
