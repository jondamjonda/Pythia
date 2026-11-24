"""L3, obligation extraction: the duties each applicable provision imposes.

For each provision L2 marked ``applicable=true``, an LLM extracts the discrete compliance
obligations the provision creates. Each obligation is persisted as its own ``finding`` row with
``finding_type='obligation'``, cited back to the provision, so the audit chain stays unbroken
from L1 candidate through L2 verdict and L3 obligation to L4 gap.

Structured output is validated with Pydantic. Idempotency mirrors L2: every L3 call writes an
``audit_event`` row, and re-runs skip provisions that already have one for the project.

Cost is low: a 200-candidate project typically has about 10 applicable verdicts, so ten L3 calls
at roughly $0.002 each on the default gpt-4o. L3 therefore always runs after L2 in the full
pipeline.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy import text as sql_text

from regsuper.analysis import require_cascade_law_source
from regsuper.analysis.citation_check import review_state_for, verify_citation
from regsuper.analysis.l0_intake import get_project
from regsuper.analysis.llm_clients import get_openai
from regsuper.analysis.model_resolver import ModelRole, resolve_model
from regsuper.config import get_settings
from regsuper.corpus import NEXUS_CORPUS
from regsuper.observability.api_tracker import tracked_chat_completion
from regsuper.observability.events import emit
from regsuper.observability.killswitch import KillSwitchEngaged
from regsuper.observability.limits import SpendLimitExceeded
from regsuper.pythia import client as pythia_client
from regsuper.storage.audit import EmptyCitationError, hash_prompt, log_event
from regsuper.storage.database import session_scope
from regsuper.storage.models import Finding, Provision

# Obligation extraction with deadline parsing is the precision-critical step of
# the cascade, so it uses the full gpt-4o rather than the mini model. The mini
# model misses about 95% of deadlines phrased indirectly ("no later than twenty
# working days after receipt of a complete notification"). Cost: about $0.20 per
# project with 100 applicable provisions, against about $0.015 on mini. The
# model is resolved once per run via `resolve_model(ModelRole.OBLIGATIONS)`; set
# the `MODEL_OBLIGATION` override (or change the default in `model_resolver`) to
# swap it.
L3_MAX_TOKENS = 2000
L3_TEMPERATURE = 0.1
DEFAULT_CONCURRENCY = 5  # gpt-4o has stricter rate limits than -mini

# The output is a JSON array wrapped in an explicit `obligations` key, because
# OpenAI JSON mode returns an object at the top level, not an array.
OBLIGATION_PROMPT = """You are extracting compliance obligations from a regulatory provision.

PROVISION
source: {publisher} {external_id}, {legal_hierarchy}
hierarchy: {hierarchy_path}
label: {label}
text:
<<<
{provision_text}
>>>

OUTPUT (JSON object with one key "obligations" holding a JSON array)
{{
  "obligations": [
    {{
      "actor": "<who must comply, e.g. 'fund manager', 'investment company'>",
      "modal": "shall" | "may" | "must_not" | "should",
      "action": "<what they must do>",
      "deadline": "<specific deadline if any (ISO 8601 or descriptive); empty string if none>",
      "conditions": ["<precondition>"],
      "exceptions": ["<exception>"],
      "supporting_text": "<exact substring from provision establishing this obligation>",
      "confidence": 0.0-1.0
    }}
  ]
}}

If the provision is purely a definition or contains no obligations, return
{{"obligations": []}}.
"""
PROMPT_HASH = hash_prompt(OBLIGATION_PROMPT)


class Obligation(BaseModel):
    actor: str
    modal: Literal["shall", "may", "must_not", "should"]
    action: str
    deadline: str = ""
    conditions: list[str] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    supporting_text: str = ""
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("deadline", "supporting_text", "actor", "action", mode="before")
    @classmethod
    def _coerce_none(cls, v: Any) -> str:
        return "" if v is None else str(v)


class ObligationListResult(BaseModel):
    obligations: list[Obligation] = Field(default_factory=list)


@dataclass
class L3Call:
    """One L3 unit of work and the obligations it returned."""

    provision_id: UUID
    source_document_id: UUID | None
    obligations: list[Obligation]
    error: str | None
    latency_ms: int
    # Retained for the citation-substring check at persist time.
    provision_text: str = ""


# ---------------------------------------------------------------------------
# Per-provision extraction
# ---------------------------------------------------------------------------
# The crosswalk (provision_id_map) is unique on the (nexus_id, nexus_corpus) pair, so lookups must
# pin the corpus the populator wrote to stay single-row. NEXUS_CORPUS (regsuper.corpus) is shared by
# the populator, l2_applicability and ProvisionIdMap.nexus_corpus's default.


async def _nexus_source_document_id(provision_id: UUID) -> UUID | None:
    """Return the app-side ``source_document_id`` for a nexus provision, via the crosswalk.

    The crosswalk (``provision_id_map``) is populated by scripts/sync_regsuper_from_nexus.py. A
    nexus provision has no app ``source_document`` of its own; this finds the document of the same
    act in the app corpus (nexus_id to regsuper_id to provision.source_document_id), so an
    obligation extracted from current engine text still cites a real, clickable app document. The
    query pins nexus_corpus, so it returns at most one row. Returns None when the act is not in the
    app corpus or the map is unpopulated, never a guessed citation; run_l3 then logs the miss and
    skips persistence, as it does for a regsuper provision without a resolvable source document.
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                sql_text(
                    """
                    SELECT p.source_document_id
                      FROM provision_id_map m
                      JOIN provision p ON p.id = m.regsuper_id
                     WHERE m.nexus_id = :nid AND m.nexus_corpus = :corpus
                    """
                ),
                {"nid": provision_id, "corpus": NEXUS_CORPUS},
            )
        ).first()
    return row[0] if row else None


async def _load_provision_via_nexus(provision_id: UUID) -> dict[str, Any] | None:
    """Fetch an L3 provision from the engine corpus via the sidecar's ``/v1/provisions``.

    Returns the same shape as L2's ``_load_provision_for_l2_via_nexus``, so the L3 prompt sees the
    same fields. The text is the engine's current provision text (current law rather than the
    app's frozen copy); ``source_document_id`` is resolved through the crosswalk so the finding's
    ``cited_documents`` points at the app's copy of the act. Fields the engine does not expose
    (hierarchy_path, publisher) are None, which the prompt tolerates.

    A single-id query should return the requested row, but if the engine echoes an id it is
    checked and a mismatch is refused (None, then logged and skipped by run_l3) rather than ever
    extracting an obligation from one provision's text and citing it to another. The assertion in
    extract_obligations only re-checks the id set below.

    run_l3 starts with ``require_cascade_law_source('L3')``, so this path is reachable only inside
    an offline evaluation (``cutover_eval_mode``) or after the operator has approved the cutover
    (``cutover_approved``). Otherwise a nexus run is refused before any provision is loaded.
    """
    rows = await pythia_client.provisions([str(provision_id)])
    if not rows:
        return None
    r = rows[0]
    echoed = r.get("id")
    if echoed is not None and str(echoed) != str(provision_id):
        return None  # engine returned a different provision: refuse rather than mis-cite
    return {
        "id": provision_id,
        "label": r.get("label"),
        "text": r.get("text"),
        "hierarchy_path": None,
        "legal_hierarchy": r.get("legal_hierarchy"),
        "source_document_id": await _nexus_source_document_id(provision_id),
        "external_id": r.get("external_id"),
        "title": r.get("source_title"),
        "publisher": None,
    }


async def _load_provision(provision_id: UUID) -> dict[str, Any] | None:
    """Load only the fields the L3 prompt needs.

    Routes by law source: 'nexus' reads the engine corpus through the sidecar (see
    ``_load_provision_via_nexus``); 'regsuper' (the default) reads the app's own tables.
    """
    if get_settings().law_source == "nexus":
        return await _load_provision_via_nexus(provision_id)
    async with session_scope() as session:
        row = (
            await session.execute(
                sql_text(
                    """
                    SELECT p.id, p.label, p.text, p.hierarchy_path::text,
                           p.legal_hierarchy, p.source_document_id,
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
        "source_document_id": row[5],
        "external_id": row[6],
        "title": row[7],
        "publisher": row[8],
    }


async def extract_obligations(
    *,
    client: AsyncOpenAI,
    project_id: UUID,
    provision_id: UUID,
    model: str,
    org_id: UUID | None = None,
    analysis_run_id: int | None = None,
) -> L3Call:
    """Call the LLM for one provision and return the validated obligations or an error.

    ``org_id`` (the project's tenant) is written onto the api_call row so the cost and token row
    survives enforced row-level security; an INSERT with a NULL org as regsuper_app is rejected,
    and the error would be silently swallowed. It defaults to None for single-tenant use.

    ``analysis_run_id`` is written onto the api_call row so the per-run cost roll-up
    (``analysis_run.cost_usd = SUM(api_call.cost_usd WHERE analysis_run_id=:id)``) includes this
    provision's spend. It is set only on the metered path.
    """
    provision = await _load_provision(provision_id)
    if provision is None:
        return L3Call(
            provision_id=provision_id,
            source_document_id=None,
            obligations=[],
            error="provision not found",
            latency_ms=0,
        )
    # Citation-identity guard: the text later passed to verify_citation must be the cited
    # provision's. The regsuper load keys on `p.id = :pid`, so this holds by construction; the
    # assertion makes the id-to-text binding explicit and would catch a mismatch on the nexus path.
    assert provision["id"] == provision_id, (
        f"citation identity violated: loaded provision {provision['id']} != cited {provision_id}"
    )

    prompt = OBLIGATION_PROMPT.format(
        publisher=provision["publisher"],
        external_id=provision["external_id"],
        legal_hierarchy=provision["legal_hierarchy"],
        hierarchy_path=provision["hierarchy_path"],
        label=provision["label"] or "(unlabelled)",
        provision_text=(provision["text"] or "").strip()[:8000],
    )
    t0 = time.perf_counter()
    try:
        response = await tracked_chat_completion(
            client,
            source="analysis.l3",
            project_id=project_id,
            org_id=org_id,  # tenant for the api_call row (required under enforced RLS)
            analysis_run_id=analysis_run_id,  # for the per-run cost roll-up
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=L3_MAX_TOKENS,
            temperature=L3_TEMPERATURE,
            response_format={"type": "json_object"},
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        body = (response.choices[0].message.content or "").strip()
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            return L3Call(
                provision_id=provision_id,
                source_document_id=provision["source_document_id"],
                obligations=[],
                error=f"json decode: {exc}",
                latency_ms=latency_ms,
                provision_text=provision.get("text") or "",
            )
        try:
            parsed = ObligationListResult.model_validate(data)
        except ValidationError as exc:
            return L3Call(
                provision_id=provision_id,
                source_document_id=provision["source_document_id"],
                obligations=[],
                error=f"schema: {exc.errors()[:2]}",
                latency_ms=latency_ms,
                provision_text=provision.get("text") or "",
            )
        return L3Call(
            provision_id=provision_id,
            source_document_id=provision["source_document_id"],
            obligations=parsed.obligations,
            error=None,
            latency_ms=latency_ms,
            provision_text=provision.get("text") or "",
        )
    except (KillSwitchEngaged, SpendLimitExceeded):
        # Abort signals: the kill switch or a breached spend cap. Re-raise so they propagate through
        # _one and asyncio.gather and halt run_l3, instead of being recorded as a per-provision
        # error while the loop keeps spending.
        raise
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return L3Call(
            provision_id=provision_id,
            source_document_id=provision["source_document_id"],
            obligations=[],
            error=repr(exc)[:300],
            latency_ms=latency_ms,
            provision_text=provision.get("text") or "",
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
async def _persist_obligation(
    *,
    project_id: UUID,
    org_id: UUID,
    provision_id: UUID,
    source_document_id: UUID,
    obligation: Obligation,
    extraction_latency_ms: int,
    provision_text: str,
    model: str,
) -> UUID:
    statement = (
        obligation.action.strip()
        or obligation.supporting_text[:200]
        or f"{obligation.modal} obligation"
    )
    severity = "obligation"
    verdict = verify_citation(span=obligation.supporting_text, provision_text=provision_text)
    review_state = review_state_for(verdict)
    payload = {
        "actor": obligation.actor,
        "modal": obligation.modal,
        "action": obligation.action,
        "deadline": obligation.deadline,
        "conditions": obligation.conditions,
        "exceptions": obligation.exceptions,
        "supporting_text": obligation.supporting_text,
        "confidence": obligation.confidence,
        "model": model,
        "extraction_latency_ms": extraction_latency_ms,
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
            finding_type="obligation",
            statement=statement[:4000],
            severity=severity,
            # Under law_source='nexus' provision_id is a nexus id that does not exist in the app
            # provision table; resolvers use payload['law_source'] to look it up in the engine.
            # cited_documents is always app-side (resolved through the crosswalk).
            cited_provisions=[provision_id],
            cited_documents=[source_document_id],
            confidence_overall=float(obligation.confidence),
            confidence_interpretation=float(obligation.confidence),
            analysis_layer="L3",
            generated_by=model,
            prompt_hash=PROMPT_HASH,
            review_state=review_state,
            payload=payload,
        )
        session.add(finding)
        await session.flush()
        return finding.id


async def _audit_l3_call(
    *,
    project_id: UUID,
    call: L3Call,
    n_persisted: int,
    model: str,
    org_id: UUID | None = None,
) -> None:
    """Write one audit_event row per L3 call (not per obligation).

    Recorded whether or not obligations were extracted, so re-runs can skip the provision.
    """
    async with session_scope() as session:
        await log_event(
            session,
            actor_type="llm",
            actor_id=model,
            action="l3.obligations.extracted",
            target_type="provision",
            target_id=str(call.provision_id),
            project_id=project_id,
            org_id=org_id,  # stamp the tenant: enforced RLS rejects a NULL-org audit INSERT
            model_version=model,
            prompt_hash=PROMPT_HASH,
            payload={
                "n_obligations": len(call.obligations),
                "n_persisted": n_persisted,
                "latency_ms": call.latency_ms,
                "error": call.error,
            },
        )


async def _existing_l3_provisions(project_id: UUID) -> set[UUID]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                sql_text(
                    """
                    SELECT DISTINCT target_id::uuid AS pid
                      FROM audit_event
                     WHERE project_id = :pid
                       AND action = 'l3.obligations.extracted'
                       AND target_type = 'provision'
                    """
                ),
                {"pid": project_id},
            )
        ).all()
    return {r[0] for r in rows}


async def _l2_applicable_provisions(project_id: UUID) -> list[UUID]:
    """Return the provision ids L2 marked applicable=True for this project.

    Only definite verdicts feed L3, not ambiguous ones: extracting obligations from a provision
    the LLM was not sure applies only adds noise.
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                sql_text(
                    """
                    SELECT unnest(cited_provisions) AS pid
                      FROM finding
                     WHERE project_id = :pid
                       AND analysis_layer = 'L2'
                       AND finding_type = 'applicable'
                       AND severity = 'applicable'   -- excludes ambiguous (severity='info')
                    """
                ),
                {"pid": project_id},
            )
        ).all()
    # Preserve insertion order but unique.
    seen: set[UUID] = set()
    out: list[UUID] = []
    for r in rows:
        if r[0] not in seen:
            seen.add(r[0])
            out.append(r[0])
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
@dataclass
class L3RunReport:
    project_id: UUID
    n_provisions: int
    n_evaluated: int
    n_skipped_cached: int
    n_obligations_extracted: int
    n_obligations_persisted: int
    n_errors: int
    findings: list[UUID]
    sample_errors: list[str]
    total_latency_ms: int


async def run_l3(
    project_id: UUID,
    *,
    max_concurrency: int = DEFAULT_CONCURRENCY,
    limit: int | None = None,
    force: bool = False,
    analysis_run_id: int | None = None,
) -> L3RunReport:
    require_cascade_law_source("L3")  # fail loudly unless the law source is approved
    # Resolve the L3 model once per run, so actor_id, model_version and generated_by stay stable
    # across every provision (ModelRole.OBLIGATIONS defaults to "gpt-4o").
    model = resolve_model(ModelRole.OBLIGATIONS)
    project = await get_project(project_id)
    if project is None:
        raise LookupError(f"project {project_id} not found")
    # Findings inherit the owning project's tenant (finding.org_id is NOT NULL).
    org_id = project.org_id

    applicable = await _l2_applicable_provisions(project_id)
    if limit is not None:
        applicable = applicable[: int(limit)]

    already: set[UUID] = set() if force else await _existing_l3_provisions(project_id)
    work = [pid for pid in applicable if pid not in already]
    n_skipped = len(applicable) - len(work)

    await emit(
        "analysis.obligations.started",
        project_id=str(project_id),
        total=len(work),
        skipped_cached=n_skipped,
    )

    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    client = get_openai()

    # Source-document lookup by app provision id. On the nexus path it finds nothing and the
    # per-call source_document_id (resolved through the crosswalk) is used instead.
    doc_lookup: dict[UUID, UUID] = {}
    if work:
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(Provision.id, Provision.source_document_id).where(Provision.id.in_(work))
                )
            ).all()
        for pid, sdid in rows:
            doc_lookup[pid] = sdid

    sem = asyncio.Semaphore(max_concurrency)
    # Shared abort flag, as in L2: asyncio.gather does not cancel siblings on a raise, so each _one
    # checks this before its LLM call and returns once a hard stop has fired.
    aborted = asyncio.Event()
    results: list[L3Call] = []
    findings: list[UUID] = []
    sample_errors: list[str] = []
    n_extracted = 0
    n_persisted = 0
    t0 = time.perf_counter()
    done = 0

    async def _one(provision_id: UUID) -> None:
        nonlocal done, n_extracted, n_persisted
        async with sem:
            if aborted.is_set():
                return  # a hard stop already fired; don't start another LLM call
            try:
                call = await extract_obligations(
                    client=client,
                    project_id=project_id,
                    provision_id=provision_id,
                    model=model,
                    org_id=org_id,  # tenant for the api_call cost row
                    analysis_run_id=analysis_run_id,  # for the per-run cost roll-up
                )
            except (KillSwitchEngaged, SpendLimitExceeded):
                aborted.set()
                raise
        results.append(call)
        done += 1
        n_extracted += len(call.obligations)
        persisted_for_this_call = 0

        if call.error:
            if len(sample_errors) < 5:
                sample_errors.append(f"{call.provision_id}: {call.error}")
        else:
            sdid = doc_lookup.get(call.provision_id) or call.source_document_id
            if sdid is None and call.obligations:
                sample_errors.append(f"{call.provision_id}: missing source_document_id")
            else:
                for ob in call.obligations:
                    try:
                        fid = await _persist_obligation(
                            project_id=project_id,
                            org_id=org_id,
                            provision_id=call.provision_id,
                            source_document_id=sdid,
                            obligation=ob,
                            extraction_latency_ms=call.latency_ms,
                            provision_text=call.provision_text,
                            model=model,
                        )
                        findings.append(fid)
                        persisted_for_this_call += 1
                    except EmptyCitationError as exc:
                        sample_errors.append(f"{call.provision_id}: citation: {exc}")
                    except Exception as exc:
                        sample_errors.append(f"{call.provision_id}: persist: {exc!r}")
        n_persisted += persisted_for_this_call

        try:
            await _audit_l3_call(
                project_id=project_id,
                call=call,
                n_persisted=persisted_for_this_call,
                model=model,
                org_id=org_id,  # the owning project's tenant, required under enforced RLS
            )
        except Exception as exc:
            if len(sample_errors) < 5:
                sample_errors.append(f"{call.provision_id}: audit: {exc!r}")

        if done % 5 == 0 or done == len(work):
            await emit(
                "analysis.obligations.progress",
                project_id=str(project_id),
                done=done,
                total=len(work),
                obligations=n_persisted,
            )

    try:
        await asyncio.gather(*(_one(pid) for pid in work))
    except (KillSwitchEngaged, SpendLimitExceeded) as exc:
        # A hard stop fired mid-cascade. Emit the abort for the control room, then re-raise so the
        # run is recorded as failed rather than a partial run being reported as a clean completion.
        await emit(
            "analysis.obligations.aborted",
            project_id=str(project_id),
            reason=type(exc).__name__,
            detail=str(exc)[:200],
            done=done,
            total=len(work),
        )
        raise

    total_ms = int((time.perf_counter() - t0) * 1000)
    n_err = sum(1 for r in results if r.error)

    await emit(
        "analysis.obligations.complete",
        project_id=str(project_id),
        n=n_persisted,
        errors=n_err,
        latency_ms=total_ms,
    )

    return L3RunReport(
        project_id=project_id,
        n_provisions=len(applicable),
        n_evaluated=len(results),
        n_skipped_cached=n_skipped,
        n_obligations_extracted=n_extracted,
        n_obligations_persisted=n_persisted,
        n_errors=n_err,
        findings=findings,
        sample_errors=sample_errors[:5],
        total_latency_ms=total_ms,
    )
