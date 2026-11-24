"""L2, applicability classification: does each L1 candidate provision apply to the project?

For each L1 candidate an LLM judges whether the provision applies. Responses are validated with
Pydantic. ``applicable=true`` and ``'ambiguous'`` verdicts are persisted to the ``finding`` table
with ``finding_type='applicable'`` and a single-element ``cited_provisions`` array; a verdict whose
confidence falls below the calibrated floor becomes a cited refusal for human review instead.

The default model is OpenAI ``gpt-4o-mini``. If calibration shows insufficient precision, set the
``MODEL_APPLICABILITY`` override (or change the ``ModelRole.APPLICABILITY`` default in
``model_resolver``) and re-run.

Concurrency is bounded by an asyncio.Semaphore to stay within the OpenAI rate limit. Cost is about
$0.0001 per call at gpt-4o-mini list price, so 200 candidates cost about $0.02.

Re-runs skip provisions already evaluated for the project (every evaluation is recorded in the audit
log); ``force=True`` overrides this.

The prompt includes a transposition note telling the LLM whether a provision is directly applicable
EU law, binding national transposition, or interpretive EU directive text.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal
from uuid import UUID

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy import text as sql_text

from regsuper.analysis import require_cascade_law_source
from regsuper.analysis.citation_check import review_state_for, verify_citation
from regsuper.analysis.l0_intake import get_project
from regsuper.analysis.l1_candidates import L1Candidate, l1_candidates
from regsuper.analysis.llm_clients import get_openai
from regsuper.analysis.model_resolver import ModelRole, resolve_model
from regsuper.analysis.transposition_map import transposition_note_for_provision
from regsuper.calibration.runtime import get_cascade_refusal_floor
from regsuper.config import get_settings
from regsuper.corpus import NEXUS_CORPUS
from regsuper.observability.api_tracker import tracked_chat_completion
from regsuper.observability.events import emit
from regsuper.observability.killswitch import KillSwitchEngaged
from regsuper.observability.limits import SpendLimitExceeded
from regsuper.pythia import client as pythia_client
from regsuper.storage.audit import EmptyCitationError, hash_prompt, log_event
from regsuper.storage.database import session_scope
from regsuper.storage.models import Finding, Provision, ProvisionIdMap

# The crosswalk (provision_id_map) is unique on the (nexus_id, nexus_corpus) pair, so lookups must
# pin the corpus the populator wrote. NEXUS_CORPUS (regsuper.corpus) is shared by the populator,
# l3_obligations and ProvisionIdMap.nexus_corpus's default, so writes and reads cannot drift apart.

L2_MAX_TOKENS = 500
L2_TEMPERATURE = 0.1
DEFAULT_CONCURRENCY = 8


# ---------------------------------------------------------------------------
# Prompt. The transposition note tells the LLM whether this provision is a
# directly applicable EU regulation, a binding Czech transposition, or
# interpretive EU directive text; without it the LLM cannot reason cleanly about
# "binding for whom".
# ---------------------------------------------------------------------------
APPLICABILITY_PROMPT = """You are analysing whether a regulatory provision applies
to a specific project.

PROJECT
sector: {sector}
jurisdiction: {jurisdictions}
entity_type: {entity_type}
declared intent: {declared_intent}
declared risks: {declared_risks}

PROVISION
source: {publisher} {external_id}, {legal_hierarchy}
hierarchy: {hierarchy_path}
label: {label}
{transposition_note}text:
<<<
{provision_text}
>>>

QUESTION
Is this provision applicable to this project?

OUTPUT (JSON only)
{{
  "applicable": true | false | "ambiguous",
  "confidence": 0.0-1.0,
  "applicable_text_span": "<exact substring from provision text>",
  "reasoning": "<1-2 sentence explanation>",
  "open_questions": ["<question that would need resolution to firm up the answer>"]
}}

If you cannot determine applicability from the provision text alone, set
"applicable": "ambiguous" and explain in open_questions. Do not guess.
"""

PROMPT_HASH = hash_prompt(APPLICABILITY_PROMPT)


class ApplicabilityResult(BaseModel):
    """Validated LLM response.

    ``applicable_text_span`` is deliberately lax: no exact substring match is required
    (whitespace and quoting drift), and an empty span is not rejected. An empty span on a
    true or ambiguous verdict still validates and persists; it is flagged for review
    downstream, where ``verify_citation`` and ``review_state_for`` (see ``_persist_finding``)
    set a review state (``needs_quote`` for an empty span) that the review UI surfaces.
    """

    applicable: Literal[True, False, "ambiguous"]
    confidence: float = Field(ge=0.0, le=1.0)
    applicable_text_span: str = ""
    reasoning: str = ""
    open_questions: list[str] = Field(default_factory=list)

    @field_validator("applicable_text_span", "reasoning", mode="before")
    @classmethod
    def _coerce_none(cls, v: Any) -> str:
        return "" if v is None else str(v)


@dataclass
class ApplicabilityCall:
    """One L2 unit of work and its result, recorded in the audit trail."""

    provision_id: UUID
    label: str | None
    source_external_id: str | None
    result: ApplicabilityResult | None
    error: str | None
    latency_ms: int
    # Retained for the citation-substring check at persist time. Loaded
    # once in evaluate_provision; carrying it avoids a second DB round-trip.
    provision_text: str = ""


# ---------------------------------------------------------------------------
# Per-provision evaluation
# ---------------------------------------------------------------------------
def _format_prompt(*, project: Any, provision_row: dict[str, Any], note: str | None) -> str:
    return APPLICABILITY_PROMPT.format(
        sector=project.sector,
        jurisdictions=", ".join(project.jurisdiction_scope or []),
        entity_type=project.entity_type or "(unspecified)",
        declared_intent=(project.declared_intent or "(none)").strip(),
        declared_risks=", ".join(project.declared_risks or []) or "(none)",
        publisher=provision_row.get("publisher", ""),
        external_id=provision_row.get("external_id", ""),
        legal_hierarchy=provision_row.get("legal_hierarchy", ""),
        hierarchy_path=provision_row.get("hierarchy_path", ""),
        label=provision_row.get("label") or "(unlabelled)",
        transposition_note=(f"transposition note: {note}\n" if note else ""),
        provision_text=(provision_row.get("text") or "").strip()[:8000],
    )


async def _nexus_doc_lookup(nexus_ids: list[UUID]) -> dict[UUID, UUID]:
    """Map nexus provision ids to app-side ``source_document_id`` through the crosswalk.

    The L2 counterpart of L3's ``_nexus_source_document_id``. Under law_source='nexus' the L1/L2
    work ids are nexus ids absent from the app Provision table, so the normal app-table lookup
    would find nothing and every finding would be dropped for a missing source_document_id. This
    resolves each nexus id to its regsuper id and then to the app source document, so the same
    act's app document can be cited. The lookup pins nexus_corpus, since the table is unique on
    (nexus_id, nexus_corpus). A nexus id without a crosswalk row (for example an act not in the
    app corpus) is absent from the result, and the caller drops that finding: honest
    under-coverage, never a guessed document.
    """
    if not nexus_ids:
        return {}
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ProvisionIdMap.nexus_id, Provision.source_document_id)
                .join(Provision, Provision.id == ProvisionIdMap.regsuper_id)
                .where(
                    ProvisionIdMap.nexus_id.in_(nexus_ids),
                    ProvisionIdMap.nexus_corpus == NEXUS_CORPUS,
                )
            )
        ).all()
    return {nid: sdid for nid, sdid in rows}


async def _load_provision_for_l2_via_nexus(provision_id: UUID) -> dict[str, Any] | None:
    """Fetch an L2 provision from the engine corpus via the sidecar's ``/v1/provisions``.

    Used when law_source='nexus', since the provision id is then a nexus id absent from the app's
    own tables. Returns the same dict shape as the app-table loader; fields the engine does not
    expose (hierarchy_path, source_document_id, publisher) are None, which the L2 prompt
    tolerates.
    """
    rows = await pythia_client.provisions([str(provision_id)])
    if not rows:
        return None
    r = rows[0]
    echoed = r.get("id")
    if echoed is not None and str(echoed) != str(provision_id):
        return None  # engine returned a different provision: refuse rather than mis-cite (as in L3)
    return {
        "id": provision_id,
        "label": r.get("label"),
        "text": r.get("text"),
        "hierarchy_path": None,
        "legal_hierarchy": r.get("legal_hierarchy"),
        "jurisdiction": r.get("jurisdiction"),
        "source_document_id": None,
        "external_id": r.get("external_id"),
        "title": r.get("source_title"),
        "publisher": None,
    }


async def _load_provision_for_l2(provision_id: UUID) -> dict[str, Any] | None:
    """Load only the fields the L2 prompt needs, in one round trip.

    Routes by law source: 'nexus' reads the engine corpus through the sidecar; 'regsuper' (the
    default) reads the app's tables.
    """
    if get_settings().law_source == "nexus":
        return await _load_provision_for_l2_via_nexus(provision_id)
    async with session_scope() as session:
        row = (
            await session.execute(
                sql_text(
                    """
                    SELECT p.id, p.label, p.text, p.hierarchy_path::text,
                           p.legal_hierarchy, p.jurisdiction, p.source_document_id,
                           sd.external_id, sd.title, sd.publisher
                      FROM provision p
                      JOIN source_document sd ON sd.id = p.source_document_id
                     WHERE p.id = :pid
                    """
                ),
                {"pid": provision_id},
            )
        ).first()
    if row is None:
        return None
    return {
        "id": row[0],
        "label": row[1],
        "text": row[2],
        "hierarchy_path": row[3],
        "legal_hierarchy": row[4],
        "jurisdiction": row[5],
        "source_document_id": row[6],
        "external_id": row[7],
        "title": row[8],
        "publisher": row[9],
    }


async def evaluate_provision(
    *,
    client: AsyncOpenAI,
    project: Any,
    provision_id: UUID,
    model: str,
    analysis_run_id: int | None = None,
) -> ApplicabilityCall:
    """Call the LLM for one provision and return the result with its timing.

    ``analysis_run_id`` is written onto the api_call row so the per-run cost roll-up
    (``analysis_run.cost_usd = SUM(api_call.cost_usd WHERE analysis_run_id=:id)``) can include
    this provision's spend. It is set only on the metered path (``entitlements_enabled=True``)
    and is None otherwise.
    """
    provision = await _load_provision_for_l2(provision_id)
    if provision is None:
        return ApplicabilityCall(
            provision_id=provision_id,
            label=None,
            source_external_id=None,
            result=None,
            error=f"provision {provision_id} not found",
            latency_ms=0,
        )
    # Citation-identity guard: the text later passed to verify_citation must be the text of the
    # cited provision. The regsuper load keys on `p.id = :pid`, so this holds by construction; the
    # assertion makes the id-to-text binding explicit and would catch a mismatch on the nexus path,
    # where id and text come back separately. A quote can only be verified against the right text.
    assert provision["id"] == provision_id, (
        f"citation identity violated: loaded provision {provision['id']} != cited {provision_id}"
    )
    note = await transposition_note_for_provision(provision_id)
    prompt = _format_prompt(project=project, provision_row=provision, note=note)

    t0 = time.perf_counter()
    try:
        response = await tracked_chat_completion(
            client,
            source="analysis.l2",
            project_id=project.id,
            # Stamp the project's tenant on the api_call row. Under enforced RLS an
            # api_call INSERT with a NULL org as regsuper_app is rejected, and the cost
            # and token row would be silently lost (persist_call swallows the error).
            org_id=getattr(project, "org_id", None),
            # Tag the api_call with its analysis run for the per-run cost roll-up
            # (None outside the metered path).
            analysis_run_id=analysis_run_id,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=L2_MAX_TOKENS,
            temperature=L2_TEMPERATURE,
            response_format={"type": "json_object"},
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        body = (response.choices[0].message.content or "").strip()
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            return ApplicabilityCall(
                provision_id=provision_id,
                label=provision["label"],
                source_external_id=provision["external_id"],
                result=None,
                error=f"json decode: {exc}",
                latency_ms=latency_ms,
                provision_text=provision.get("text") or "",
            )
        try:
            result = ApplicabilityResult.model_validate(data)
        except ValidationError as exc:
            return ApplicabilityCall(
                provision_id=provision_id,
                label=provision["label"],
                source_external_id=provision["external_id"],
                result=None,
                error=f"schema: {exc.errors()[:2]}",
                latency_ms=latency_ms,
                provision_text=provision.get("text") or "",
            )
        return ApplicabilityCall(
            provision_id=provision_id,
            label=provision["label"],
            source_external_id=provision["external_id"],
            result=result,
            error=None,
            latency_ms=latency_ms,
            provision_text=provision.get("text") or "",
        )
    except (KillSwitchEngaged, SpendLimitExceeded):
        # Abort signals: the kill switch or a breached spend cap. These are not per-provision
        # failures; they mean "stop the whole cascade now". Re-raise so they propagate through
        # _one and asyncio.gather and halt run_l2, instead of being recorded as a per-item error
        # while the loop keeps spending budget on every remaining provision.
        raise
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return ApplicabilityCall(
            provision_id=provision_id,
            label=provision["label"],
            source_external_id=provision["external_id"],
            result=None,
            error=repr(exc)[:300],
            latency_ms=latency_ms,
            provision_text=provision.get("text") or "",
        )


# ---------------------------------------------------------------------------
# Audit: write one audit_event for every L2 evaluation, whatever the verdict.
# `_existing_l2_provisions` reads these to skip repeated work, and the audit
# trail must record every model decision, not just those that produced a finding.
# ---------------------------------------------------------------------------
async def _audit_l2_call(
    *,
    project_id: UUID,
    provision_id: UUID,
    call: ApplicabilityCall,
    finding_id: UUID | None,
    model: str,
    org_id: UUID | None = None,
) -> None:
    verdict: str
    confidence: float | None
    if call.result is not None:
        verdict = str(call.result.applicable)
        confidence = call.result.confidence
    else:
        verdict = "error"
        confidence = None
    payload: dict[str, Any] = {
        "verdict": verdict,
        "confidence": confidence,
        "latency_ms": call.latency_ms,
        "model": model,
        "finding_id": str(finding_id) if finding_id else None,
        "error": call.error,
    }
    async with session_scope() as session:
        await log_event(
            session,
            actor_type="llm",
            actor_id=model,
            action="l2.applicability.judged",
            target_type="provision",
            target_id=str(provision_id),
            project_id=project_id,
            org_id=org_id,  # stamp the tenant: enforced RLS rejects a NULL-org audit INSERT
            model_version=model,
            prompt_hash=PROMPT_HASH,
            payload=payload,
        )


# ---------------------------------------------------------------------------
# Persistence: findings for applicable=true or ambiguous verdicts
# ---------------------------------------------------------------------------
async def _persist_finding(
    *,
    project_id: UUID,
    org_id: UUID,
    provision_id: UUID,
    source_document_id: UUID,
    call: ApplicabilityCall,
    provision_text: str,
    model: str,
) -> UUID:
    """Create one finding row.

    The matching audit_event is written separately by ``_audit_l2_call``, so the audit trail
    records every evaluation, not just the persisted ones.

    Citation-hallucination guard: before persisting, check that the LLM's
    ``applicable_text_span`` actually appears (exactly or nearly) in the provision text. A
    ``not_found`` verdict means the LLM invented the quote; the finding is still persisted so a
    lawyer can see and correct it, but with ``review_state='unverified_citation'`` so the review
    UI surfaces it.
    """
    result = call.result
    assert result is not None  # caller filters
    statement = (
        result.reasoning.strip()
        or f"Provision marked {result.applicable!r} with confidence {result.confidence:.2f}"
    )
    severity = "info" if result.applicable == "ambiguous" else "applicable"
    verdict = verify_citation(span=result.applicable_text_span, provision_text=provision_text)
    review_state = review_state_for(verdict)
    payload = {
        "applicable": result.applicable,
        "confidence": result.confidence,
        "applicable_text_span": result.applicable_text_span,
        "reasoning": result.reasoning,
        "open_questions": result.open_questions,
        "model": model,
        "latency_ms": call.latency_ms,
        "citation_verdict": verdict,
    }
    if get_settings().law_source == "nexus":
        payload["law_source"] = (
            "nexus"  # record the corpus so cited-provision resolvers read the engine
        )
    async with session_scope() as session:
        finding = Finding(
            project_id=project_id,
            org_id=org_id,  # inherited from the owning project (NOT NULL since migration 007)
            finding_type="applicable",
            statement=statement[:4000],
            severity=severity,
            cited_provisions=[provision_id],
            cited_documents=[source_document_id],
            confidence_overall=float(result.confidence),
            confidence_retrieval=None,
            confidence_interpretation=float(result.confidence),
            confidence_temporal=None,
            confidence_source_coverage=None,
            analysis_layer="L2",
            generated_by=model,
            prompt_hash=PROMPT_HASH,
            review_state=review_state,
            payload=payload,
        )
        session.add(finding)
        await session.flush()  # finding.id now populated
        return finding.id


# ---------------------------------------------------------------------------
# Refusal persistence: the below-floor branch.
#
# When the LLM judged a provision applicable (True or 'ambiguous') but its
# confidence fell below the calibrated cascade applicability floor, we must not
# publish a confident `finding_type='applicable'` row. Instead we record a
# durable, cited refusal, so the answer is honest ("the law may apply here, but
# our confidence is below the floor; a human must review") rather than either a
# falsely confident finding or a silent drop.
#
# Invariants (all checked in test_l2_refusal_in_cascade):
#   * finding_type='refusal' is a distinct lane. Migration 012 made finding_type
#     plain TEXT (no CHECK), so 'refusal' is insertable.
#   * cited_provisions=[provision_id]: a refusal still cites the provision it
#     refused on, so the `cited_provisions_nonempty` CHECK and the Python-side
#     EmptyCitationError both hold.
#   * answer_mode='needs_review' is the honest output mode.
#   * severity='needs_review', deliberately not 'applicable', so L3's
#     `_l2_applicable_provisions` filter (finding_type='applicable' AND
#     severity='applicable') excludes it and L3 never processes a refusal.
#   * analysis_layer='L2' and generated_by=model give the same provenance as an
#     above-floor finding, so the audit chain stays intact.
# ---------------------------------------------------------------------------
async def _persist_refusal(
    *,
    project_id: UUID,
    org_id: UUID,
    provision_id: UUID,
    source_document_id: UUID,
    call: ApplicabilityCall,
    floor: float,
    model: str,
) -> UUID:
    """Create one finding_type='refusal' row for a below-floor applicable verdict.

    The refusal cites the same provision (so the cited_provisions_nonempty CHECK and
    EmptyCitationError hold) and carries answer_mode='needs_review' with a severity other than
    'applicable', so L3's applicable-only filter excludes it.
    """
    result = call.result
    assert result is not None  # caller filters
    statement = (
        f"Applicability uncertain — confidence {result.confidence:.2f} is below the "
        f"applicability floor {floor:.2f}; recorded for human review."
    )
    payload = {
        "applicable": result.applicable,
        "confidence": result.confidence,
        "cascade_refusal_floor": floor,
        "applicable_text_span": result.applicable_text_span,
        "reasoning": result.reasoning,
        "open_questions": result.open_questions,
        "model": model,
        "latency_ms": call.latency_ms,
    }
    if get_settings().law_source == "nexus":
        payload["law_source"] = (
            "nexus"  # record the corpus so cited-provision resolvers read the engine
        )
    async with session_scope() as session:
        finding = Finding(
            project_id=project_id,
            org_id=org_id,  # inherited from the owning project (NOT NULL since migration 007)
            finding_type="refusal",
            statement=statement[:4000],
            # Not 'applicable', so L3's `_l2_applicable_provisions` filter
            # (finding_type='applicable' AND severity='applicable') never picks
            # this row up. A refusal must never enter the obligation lane.
            severity="needs_review",
            cited_provisions=[provision_id],
            cited_documents=[source_document_id],
            confidence_overall=float(result.confidence),
            confidence_retrieval=None,
            confidence_interpretation=float(result.confidence),
            confidence_temporal=None,
            confidence_source_coverage=None,
            analysis_layer="L2",
            generated_by=model,
            prompt_hash=PROMPT_HASH,
            answer_mode="needs_review",
            review_state="needs_review",
            payload=payload,
        )
        session.add(finding)
        await session.flush()  # finding.id now populated
        return finding.id


async def _existing_l2_provisions(project_id: UUID) -> set[UUID]:
    """Return the provision ids already analysed for this project, for idempotent re-runs.

    Checks ``audit_event`` (action='l2.applicability.judged') rather than ``finding``, because
    findings are only written for applicable and ambiguous verdicts; checking ``finding`` alone
    would re-run every previously not-applicable provision and spend budget on each run. The
    audit log records every L2 evaluation regardless of verdict.
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                sql_text(
                    """
                    SELECT DISTINCT target_id::uuid AS pid
                      FROM audit_event
                     WHERE project_id = :pid
                       AND action = 'l2.applicability.judged'
                       AND target_type = 'provision'
                    """
                ),
                {"pid": project_id},
            )
        ).all()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
@dataclass
class L2RunReport:
    project_id: UUID
    n_candidates: int
    n_evaluated: int
    n_skipped_cached: int
    n_applicable: int
    n_ambiguous: int
    n_not_applicable: int
    n_refused: int
    n_errors: int
    findings: list[UUID]
    sample_errors: list[str]
    total_latency_ms: int


async def run_l2(
    project_id: UUID,
    *,
    candidates: list[L1Candidate] | None = None,
    max_concurrency: int = DEFAULT_CONCURRENCY,
    limit: int | None = None,
    force: bool = False,
    as_of: date | None = None,
    analysis_run_id: int | None = None,
) -> L2RunReport:
    """Evaluate the project's L1 candidates with the L2 applicability prompt.

    Args:
        candidates: A precomputed L1 result (for example so a UI can show progress on the exact
            list being analysed). If None, l1_candidates is called internally.
        limit: Stop after this many candidates; useful for smoke tests.
        force: Re-evaluate provisions already evaluated for this project.
        as_of: Evaluate the law as it stood on this date (None means current law).
        analysis_run_id: On the metered path, every api_call this run makes is tagged with it
            for the per-run cost roll-up.
    """
    require_cascade_law_source("L2")  # fail loudly unless the law source is approved
    # Resolve the L2 model once per run, so actor_id, model_version and generated_by stay stable
    # across every provision (ModelRole.APPLICABILITY defaults to "gpt-4o-mini").
    model = resolve_model(ModelRole.APPLICABILITY)
    # Resolve the cascade applicability floor once per run, so the threshold is stable. A provision
    # judged applicable with confidence below it becomes a cited refusal instead of a confident
    # finding. The floor reads its own calibration key, `cascade_applicability_floor`, separate
    # from get_refusal_threshold's retrieval gate, and falls back to settings.cascade_refusal_floor
    # (0.5, the default until a lawyer tunes it).
    refusal_floor = get_cascade_refusal_floor().value
    project = await get_project(project_id)
    if project is None:
        raise LookupError(f"project {project_id} not found")
    # Findings inherit the owning project's tenant (finding.org_id is NOT NULL).
    org_id = project.org_id

    if candidates is None:
        candidates = await l1_candidates(project_id, top_k=200, as_of=as_of)
    if limit is not None:
        candidates = candidates[: int(limit)]

    already: set[UUID] = set() if force else await _existing_l2_provisions(project_id)
    work = [c for c in candidates if c.provision_id not in already]
    n_skipped = len(candidates) - len(work)

    await emit(
        "analysis.applicability.started",
        project_id=str(project_id),
        total=len(work),
        skipped_cached=n_skipped,
    )

    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    client = get_openai()

    sem = asyncio.Semaphore(max_concurrency)
    # Shared abort flag. asyncio.gather schedules every _one up front and does not cancel siblings
    # when one raises, so a bare re-raise alone would let the queued provisions keep making LLM
    # calls as the semaphore drains. Each _one checks this flag before its LLM call and returns at
    # once after a hard stop (kill switch or spend cap), so the run actually stops spending.
    aborted = asyncio.Event()
    results: list[ApplicabilityCall] = []
    findings: list[UUID] = []
    sample_errors: list[str] = []
    t0 = time.perf_counter()
    done = 0
    applicable_running = 0
    refused_running = 0  # below-floor applicable verdicts routed to a cited refusal

    # Source-document lookup for `cited_documents`: one round trip up front is
    # cheaper than one per finding.
    doc_lookup: dict[UUID, UUID] = {}
    if settings.law_source == "nexus":
        # The work ids are nexus ids absent from the app Provision table, so resolve their app-side
        # source_document_id (the same act's app document) through the crosswalk. A nexus id without
        # a crosswalk row is absent, and its finding is dropped below for a missing
        # source_document_id. require_cascade_law_source('L2') above still refuses nexus unless the
        # cutover is approved or an offline evaluation is running.
        doc_lookup = await _nexus_doc_lookup([c.provision_id for c in work])
    else:
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(Provision.id, Provision.source_document_id).where(
                        Provision.id.in_([c.provision_id for c in work])
                    )
                )
            ).all()
        for pid, sdid in rows:
            doc_lookup[pid] = sdid

    async def _one(cand: L1Candidate) -> None:
        nonlocal done, applicable_running, refused_running
        async with sem:
            if aborted.is_set():
                return  # a hard stop already fired; don't start another LLM call
            try:
                call = await evaluate_provision(
                    client=client,
                    project=project,
                    provision_id=cand.provision_id,
                    model=model,
                    analysis_run_id=analysis_run_id,  # for the per-run cost roll-up
                )
            except (KillSwitchEngaged, SpendLimitExceeded):
                aborted.set()  # queued siblings return before making their LLM call
                raise
        results.append(call)
        done += 1
        finding_id: UUID | None = None

        if call.error:
            if len(sample_errors) < 5:
                sample_errors.append(f"{call.provision_id}: {call.error}")
        elif call.result and call.result.applicable in (True, "ambiguous"):
            sdid = doc_lookup.get(call.provision_id)
            if sdid is None:
                sample_errors.append(f"{call.provision_id}: missing source_document_id")
            # Below-floor refusal: the LLM judged the provision applicable (True or 'ambiguous')
            # but with confidence under the cascade applicability floor. Rather than publish a
            # confident finding (a false positive) or drop it silently (a silent miss), record a
            # durable cited refusal (finding_type='refusal', answer_mode='needs_review') citing the
            # same provision, with a severity L3 excludes, and emit
            # `analysis.applicability.refused` with the confidence and floor.
            elif call.result.confidence < refusal_floor:
                try:
                    finding_id = await _persist_refusal(
                        project_id=project_id,
                        org_id=org_id,
                        provision_id=call.provision_id,
                        source_document_id=sdid,
                        call=call,
                        floor=refusal_floor,
                        model=model,
                    )
                    findings.append(finding_id)
                    refused_running += 1
                    await emit(
                        "analysis.applicability.refused",
                        project_id=str(project_id),
                        confidence=call.result.confidence,
                        floor=refusal_floor,
                    )
                except EmptyCitationError as exc:
                    sample_errors.append(f"{call.provision_id}: citation: {exc}")
                except Exception as exc:
                    sample_errors.append(f"{call.provision_id}: refusal: {exc!r}")
            else:
                try:
                    finding_id = await _persist_finding(
                        project_id=project_id,
                        org_id=org_id,
                        provision_id=call.provision_id,
                        source_document_id=sdid,
                        call=call,
                        provision_text=call.provision_text,
                        model=model,
                    )
                    findings.append(finding_id)
                    if call.result.applicable is True:
                        applicable_running += 1
                except EmptyCitationError as exc:
                    sample_errors.append(f"{call.provision_id}: citation: {exc}")
                except Exception as exc:
                    sample_errors.append(f"{call.provision_id}: persist: {exc!r}")

        # Always write an audit row, including for not-applicable and errored
        # evaluations. `_existing_l2_provisions` reads these to make re-runs
        # idempotent; otherwise every not-applicable provision would be sent to
        # the LLM again on every run.
        try:
            await _audit_l2_call(
                project_id=project_id,
                provision_id=call.provision_id,
                call=call,
                finding_id=finding_id,
                model=model,
                org_id=org_id,  # the owning project's tenant, required under enforced RLS
            )
        except Exception as exc:
            if len(sample_errors) < 5:
                sample_errors.append(f"{call.provision_id}: audit: {exc!r}")
        if done % 10 == 0 or done == len(work):
            await emit(
                "analysis.applicability.progress",
                project_id=str(project_id),
                done=done,
                total=len(work),
                applicable=applicable_running,
            )

    try:
        await asyncio.gather(*(_one(c) for c in work))
    except (KillSwitchEngaged, SpendLimitExceeded) as exc:
        # A hard stop fired mid-cascade (kill switch engaged or spend cap breached). Stop cleanly:
        # emit the abort so the control room shows why L2 halted, then re-raise so the caller
        # (project_analyze) records the run as failed rather than reporting a partial success.
        await emit(
            "analysis.applicability.aborted",
            project_id=str(project_id),
            reason=type(exc).__name__,
            detail=str(exc)[:200],
            done=done,
            total=len(work),
        )
        raise

    total_ms = int((time.perf_counter() - t0) * 1000)
    n_app = sum(1 for r in results if r.result and r.result.applicable is True)
    n_amb = sum(1 for r in results if r.result and r.result.applicable == "ambiguous")
    n_no = sum(1 for r in results if r.result and r.result.applicable is False)
    n_err = sum(1 for r in results if r.result is None)

    await emit(
        "analysis.applicability.complete",
        project_id=str(project_id),
        n=n_app,
        ambiguous=n_amb,
        not_applicable=n_no,
        errors=n_err,
        latency_ms=total_ms,
    )

    return L2RunReport(
        project_id=project_id,
        n_candidates=len(candidates),
        n_evaluated=len(results),
        n_skipped_cached=n_skipped,
        n_applicable=n_app,
        n_ambiguous=n_amb,
        n_not_applicable=n_no,
        # n_refused counts rows actually routed to the below-floor refusal branch (the applicable or
        # ambiguous verdicts with confidence under the floor). n_app and n_amb remain raw verdict
        # tallies; n_refused says how many became cited refusals.
        n_refused=refused_running,
        n_errors=n_err,
        findings=findings,
        sample_errors=sample_errors[:5],
        total_latency_ms=total_ms,
    )
