"""L4, gap analysis: obligations the client's own documents show no evidence of meeting.

A deliberately simple keyword heuristic; no LLM is involved. A grounded LLM check ("does this
client document satisfy this obligation?") would replace it.

1. Scan every L3 obligation finding for evidence keywords (licence, permit, registration,
   notification, depositary, prospectus, ...).
2. For each keyword found, search the project's uploaded documents
   (``project_document.extracted_text``) for the same term (case-insensitive substring).
3. If a keyword appears in the obligation but in no client document, the obligation gets a gap
   finding.

The vocabulary intentionally over-flags rather than under-flags: in compliance review a missed gap
costs far more than a false-positive one.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import desc, select, tuple_
from sqlalchemy import text as sql_text

from regsuper.analysis import require_cascade_law_source
from regsuper.analysis.l0_intake import get_project
from regsuper.config import get_settings
from regsuper.observability.events import emit
from regsuper.storage.audit import EmptyCitationError, log_event
from regsuper.storage.database import session_scope
from regsuper.storage.models import Finding, ProjectDocument

L4_GENERATOR = "rule:l4-keyword-stub-v1"

# How many L3 obligation findings to hold in memory per database round trip. A
# project can have thousands of obligations, each carrying its statement and
# supporting text, so streaming in bounded pages caps peak memory without changing
# which gaps are produced (same rows, same order, same predicate). The default is
# generous, so small projects fit in one page.
L4_OBLIGATION_PAGE_SIZE = 500

# How many client documents to fetch per page when scanning for evidence keywords.
# Rather than concatenating every document's text into one large blob, documents
# are streamed in pages and reduced to the small fixed set of evidence terms they
# contain, so peak memory is one page of documents plus a set of at most 15 terms.
# The result is the same (see the _present_search_terms docstring).
L4_DOCUMENT_PAGE_SIZE = 200

# Each row is (regex matching an evidence keyword in obligation text,
# evidence_type label, canonical term to search for in client documents). The
# regex captures, so the exact phrase that triggered the gap can be shown.
EVIDENCE_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"\b(licen[cs]e[ds]?|licen[zc][ie])\b", "licence", "licence"),
    (r"\b(authoris(?:ation|ed)|authoriz(?:ation|ed))\b", "authorisation", "authorisation"),
    (r"\b(permit[s]?)\b", "permit", "permit"),
    (r"\b(register|registr(?:ation|ed))\b", "registration", "registration"),
    (r"\b(notif(?:y|ication|ied)|notification)\b", "notification", "notification"),
    (r"\b(prospect(?:us|i|uses))\b", "prospectus", "prospectus"),
    (r"\b(depositar(?:y|ies))\b", "depositary", "depositary"),
    (r"\b(custod(?:y|ian))\b", "custodian", "custodian"),
    (r"\b(audit(?:or|ed)?|auditing)\b", "auditor", "auditor"),
    (r"\b(consent|approv(?:al|ed))\b", "approval", "approval"),
    (r"\b(report(?:ing)?|filing)\b", "reporting", "reporting"),
    (r"\b(policy|policies|procedure[s]?)\b", "policy", "policy"),
    (r"\b(record[s]?|documenta(?:tion|ry))\b", "records", "records"),
    (r"\b(insurance|professional indemnity|guarantee)\b", "insurance", "insurance"),
    (
        r"\b(senior management|fit and proper|qualification[s]?)\b",
        "fit_and_proper",
        "fit and proper",
    ),
)

# The complete, fixed vocabulary of client-document search terms (lower-cased).
# Every Evidence the scan can produce has a ``search_term`` from this set, so
# precomputing which of them appear in any client document is enough to decide
# coverage for the whole run.
_ALL_SEARCH_TERMS: tuple[str, ...] = tuple(
    dict.fromkeys(term.lower() for _, _, term in EVIDENCE_PATTERNS)
)


@dataclass(frozen=True)
class Evidence:
    """One evidence keyword extracted from an obligation.

    Stored on the gap finding's payload so a lawyer can see what triggered each gap.
    """

    pattern_match: str  # the actual phrase found in the obligation text
    evidence_type: str  # canonical bucket (licence, authorisation, ...)
    search_term: str  # what to look for in client-document text


@dataclass
class L4RunReport:
    project_id: UUID
    n_obligations_scanned: int
    n_obligations_with_evidence: int
    n_gaps_persisted: int
    n_evidence_covered: int  # found in at least one client doc
    n_client_documents: int
    sample_errors: list[str]
    total_latency_ms: int


def _detect_evidence(text: str) -> list[Evidence]:
    """Run every evidence pattern and return the de-duplicated Evidence list."""
    if not text:
        return []
    found: dict[str, Evidence] = {}
    for pat, etype, term in EVIDENCE_PATTERNS:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            key = f"{etype}:{m.group(0).lower()}"
            if key not in found:
                found[key] = Evidence(
                    pattern_match=m.group(0),
                    evidence_type=etype,
                    search_term=term,
                )
    return list(found.values())


def _finding_to_obligation(f: Finding) -> dict[str, Any]:
    """Convert one L3 obligation Finding row into the plain dict the scan loop uses.

    The single definition of the field set, so the streaming path and the list path (used by
    tests) produce identical dicts.
    """
    p = f.payload or {}
    return {
        "id": f.id,
        "cited_provisions": list(f.cited_provisions),
        "cited_documents": list(f.cited_documents),
        "action": p.get("action", ""),
        "supporting_text": p.get("supporting_text", ""),
        "statement": f.statement,
        # Inputs for gap demotion (applicability verdict, duplicate detection); None if absent.
        "applicability": p.get("applicability"),
        "actor": p.get("actor"),
        "modal": p.get("modal"),
    }


async def _iter_obligation_findings(
    project_id: UUID,
    *,
    page_size: int = L4_OBLIGATION_PAGE_SIZE,
) -> AsyncIterator[dict[str, Any]]:
    """Stream L3 obligation findings for the project in bounded keyset pages.

    Yields dicts ordered by created_at, with id as a stable tie-break, holding only one page
    in memory at a time. The keyset cursor on (created_at, id) keeps paging deterministic even
    when many findings share a created_at, so the gap set and the duplicate detection's "first
    wins" rule do not depend on page size. Read-only; no state is carried between pages except
    the cursor.
    """
    page_size = max(1, int(page_size))
    last_created = None
    last_id: UUID | None = None
    while True:
        async with session_scope() as session:
            stmt = select(Finding).where(
                Finding.project_id == project_id,
                Finding.analysis_layer == "L3",
                Finding.finding_type == "obligation",
            )
            if last_created is not None:
                # Keyset: rows strictly after the last one yielded, in the same
                # (created_at, id) total order.
                stmt = stmt.where(
                    tuple_(Finding.created_at, Finding.id) > tuple_(last_created, last_id)
                )
            stmt = stmt.order_by(Finding.created_at, Finding.id).limit(page_size)
            rows = (await session.execute(stmt)).scalars().all()
        if not rows:
            return
        for f in rows:
            last_created = f.created_at
            last_id = f.id
            yield _finding_to_obligation(f)
        if len(rows) < page_size:
            return


async def _load_obligation_findings(project_id: UUID) -> list[dict[str, Any]]:
    """Load every L3 obligation finding for the project (id and key fields) as a list.

    A thin eager wrapper over the streaming iterator for callers and tests that want the full
    list; the live run streams via ``_iter_obligation_findings`` to bound peak memory. Same
    (created_at, id) order.
    """
    return [ob async for ob in _iter_obligation_findings(project_id)]


def _gap_suppressed_by(obligation: dict[str, Any], *, dup_of: str | None) -> list[str]:
    """Return the reasons a gap should be demoted to the visible context section.

    Gaps are tagged, never dropped. Returns [] unless a flag is on and its condition holds:
    ``entity_scoping_gap_suppression_enabled`` demotes gaps from obligations that do not bind
    the client, and ``multi_limb_gap_dedup_enabled`` demotes duplicates of an earlier gap. The
    caller always persists the gap; the reasons only change where render shows it. A
    'stale_law' reason is added later, at render, where the provision's date columns are
    available.
    """
    s = get_settings()
    reasons: list[str] = []
    if getattr(s, "entity_scoping_gap_suppression_enabled", False):
        appl = obligation.get("applicability")
        if isinstance(appl, dict) and appl.get("binds") == "out_of_scope":
            reasons.append("applicability_out_of_scope")
    if getattr(s, "multi_limb_gap_dedup_enabled", False) and dup_of:
        reasons.append("duplicate")
    return reasons


async def _iter_client_doc_texts(
    project_id: UUID,
    *,
    page_size: int = L4_DOCUMENT_PAGE_SIZE,
) -> AsyncIterator[str]:
    """Stream each client document's non-empty extracted_text in bounded pages.

    Yields the raw (not lower-cased) text of each document, skipping NULL and
    empty text. A keyset cursor on the row id keeps paging deterministic and
    complete.
    """
    page_size = max(1, int(page_size))
    last_id: UUID | None = None
    while True:
        async with session_scope() as session:
            stmt = select(ProjectDocument.id, ProjectDocument.extracted_text).where(
                ProjectDocument.project_id == project_id,
                ProjectDocument.extracted_text.is_not(None),
            )
            if last_id is not None:
                stmt = stmt.where(ProjectDocument.id > last_id)
            stmt = stmt.order_by(ProjectDocument.id).limit(page_size)
            rows = (await session.execute(stmt)).all()
        if not rows:
            return
        for rid, txt in rows:
            last_id = rid
            if txt:
                yield txt
        if len(rows) < page_size:
            return


async def _present_search_terms(
    project_id: UUID,
    search_terms: Iterable[str],
    *,
    page_size: int = L4_DOCUMENT_PAGE_SIZE,
) -> tuple[set[str], int]:
    """Stream client documents and return ``(present_terms, n_documents)``.

    ``present_terms`` is the set of lower-cased ``search_term`` values that appear as a
    case-insensitive substring in at least one client document, which is exactly what the scan
    loop needs.

    This is equivalent to searching the concatenation ``"\\n\\n".join(texts).lower()`` (what
    ``_load_client_doc_text`` builds). The only extra matches a concatenation could produce are
    substrings straddling the ``"\\n\\n"`` separator between two documents, and every search
    term is a short phrase without newlines, so none can. Nor can concatenation hide a match
    within a document. So the gaps produced are the same; only peak memory differs (one page of
    documents plus a small set, never the full concatenation).
    """
    terms = [t.lower() for t in search_terms]
    present: set[str] = set()
    n_docs = 0
    async for txt in _iter_client_doc_texts(project_id, page_size=page_size):
        n_docs += 1
        low = txt.lower()
        # Only test terms not yet found: once present, a term stays present, so
        # the work shrinks as the scan proceeds.
        remaining = [t for t in terms if t not in present]
        for t in remaining:
            if t in low:
                present.add(t)
    return present, n_docs


async def _load_client_doc_text(project_id: UUID) -> tuple[str, int]:
    """Return all client documents' text concatenated and lower-cased, plus the document count.

    Kept for tests and backward compatibility. The live run streams via
    ``_present_search_terms`` instead, to bound peak memory.
    """
    texts = [txt async for txt in _iter_client_doc_texts(project_id)]
    return ("\n\n".join(texts).lower(), len(texts))


async def _count_obligation_findings(project_id: UUID) -> int:
    """Count the project's L3 obligation findings without loading them.

    Provides the up-front total for the started event and the report's
    ``n_obligations_scanned``.
    """
    async with session_scope() as session:
        n = (
            await session.execute(
                sql_text(
                    """
                    SELECT count(*) FROM finding
                     WHERE project_id = :pid
                       AND analysis_layer = 'L3'
                       AND finding_type = 'obligation'
                    """
                ),
                {"pid": project_id},
            )
        ).scalar()
    return int(n or 0)


async def _dedup_first_map(project_id: UUID) -> dict[str, str]:
    """Map each duplicate obligation id to the first obligation sharing its substance key.

    "First" follows the scan's (created_at, id) order. Obligations are streamed and only two
    small string-to-string maps are kept (``seen`` keyed by limb_key, and the result), never
    the full obligation dicts.
    """
    from regsuper.analysis.multi_limb import limb_key

    seen: dict[str, str] = {}
    dedup_of: dict[str, str] = {}
    async for ob in _iter_obligation_findings(project_id):
        k = limb_key(ob.get("actor"), ob.get("statement"), ob.get("modal"), None)
        ob_id = str(ob["id"])
        first = seen.setdefault(k, ob_id)
        if first != ob_id:
            dedup_of[ob_id] = first
    return dedup_of


async def _existing_gap_obligations(project_id: UUID) -> set[UUID]:
    """Return obligation finding ids already covered by an L4 gap row, for idempotency."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                sql_text(
                    """
                SELECT DISTINCT (payload->>'obligation_finding_id')::uuid
                  FROM finding
                 WHERE project_id = :pid
                   AND analysis_layer = 'L4'
                   AND finding_type = 'gap'
                   AND payload ? 'obligation_finding_id'
                """
                ),
                {"pid": project_id},
            )
        ).all()
    return {r[0] for r in rows if r[0]}


async def _persist_gap(
    *,
    project_id: UUID,
    org_id: UUID,
    obligation: dict[str, Any],
    missing_evidence: list[Evidence],
    suppressed_by: list[str] | None = None,
) -> UUID:
    cited_provisions = obligation["cited_provisions"]
    cited_documents = obligation["cited_documents"]
    statement_bits = [e.search_term for e in missing_evidence]
    statement = (
        f"No client document mentions: {', '.join(statement_bits)}. "
        f"Obligation: {obligation['statement'][:200]}"
    )
    payload = {
        "obligation_finding_id": str(obligation["id"]),
        "missing_evidence": [
            {
                "evidence_type": e.evidence_type,
                "pattern_match": e.pattern_match,
                "search_term": e.search_term,
            }
            for e in missing_evidence
        ],
        "obligation_statement": obligation["statement"],
        "obligation_action": obligation["action"],
        # The parent obligation's deontic modal, so render._gap_severity_label can order gaps
        # critical first. If absent, render falls back to the provision column and shows the
        # default label "důležité" (important).
        "modal": obligation.get("modal"),
    }
    # Record any demotion reasons. The gap is still persisted in full below; this key only
    # tells render to show it in the visible context section.
    if suppressed_by:
        payload["suppressed_by"] = list(suppressed_by)
    if get_settings().law_source == "nexus":
        # A gap inherits its parent obligation's cited_provisions (nexus ids on this path), so it
        # must carry the same corpus marker, or the citation resolver would look them up in the
        # app provision table, where they do not exist.
        payload["law_source"] = "nexus"
    async with session_scope() as session:
        finding = Finding(
            project_id=project_id,
            org_id=org_id,  # inherited from the owning project (NOT NULL since migration 007)
            finding_type="gap",
            statement=statement[:4000],
            severity="gap",
            cited_provisions=list(cited_provisions),
            cited_documents=list(cited_documents),
            confidence_overall=0.6,  # rule-based; calibrated later
            confidence_interpretation=0.6,
            analysis_layer="L4",
            generated_by=L4_GENERATOR,
            prompt_hash=None,
            review_state="unreviewed",
            payload=payload,
        )
        session.add(finding)
        await session.flush()
        return finding.id


async def _audit_l4_call(
    *,
    project_id: UUID,
    obligation_finding_id: UUID,
    n_missing: int,
    n_present: int,
    gap_finding_id: UUID | None,
    org_id: UUID | None = None,
) -> None:
    async with session_scope() as session:
        await log_event(
            session,
            actor_type="rule",
            actor_id=L4_GENERATOR,
            action="l4.gap.scanned",
            target_type="finding",
            target_id=str(obligation_finding_id),
            project_id=project_id,
            org_id=org_id,  # stamp the tenant: enforced RLS rejects a NULL-org audit INSERT
            model_version=L4_GENERATOR,
            payload={
                "n_missing_evidence": n_missing,
                "n_present_evidence": n_present,
                "gap_finding_id": str(gap_finding_id) if gap_finding_id else None,
            },
        )


async def run_l4_gaps(
    project_id: UUID,
    *,
    force: bool = False,
    analysis_run_id: int | None = None,
) -> L4RunReport:
    """Find evidence keywords in each L3 obligation and record a gap for any the client lacks.

    ``analysis_run_id`` is accepted so the metered analyze path can pass one id to L2, L3 and
    L4 alike. L4 makes no external API call, so there is nothing to tag and the argument is
    unused; the per-run cost roll-up covers only L2 and L3 LLM spend.
    """
    _ = analysis_run_id  # accepted for call-site parity; L4 makes no api_call to tag
    require_cascade_law_source("L4")  # fail loudly unless the law source is approved
    t0 = time.perf_counter()
    project = await get_project(project_id)
    if project is None:
        raise LookupError(f"project {project_id} not found")
    org_id = project.org_id  # gaps inherit the owning project's tenant (finding.org_id NOT NULL)

    # To bound memory, obligations are streamed in pages below, and client documents are
    # reduced up front to the small set of evidence terms that appear in any of them (see the
    # _present_search_terms docstring for why this gives the same gaps).
    n_obligations = await _count_obligation_findings(project_id)
    present_terms, n_docs = await _present_search_terms(project_id, _ALL_SEARCH_TERMS)

    # Duplicate detection, only with multi_limb_gap_dedup_enabled on. Maps an obligation id to
    # the earlier obligation with the same substance key. Used only to add a 'duplicate'
    # demotion reason to the later gap, which is still persisted in full.
    dedup_of: dict[str, str] = {}
    if getattr(get_settings(), "multi_limb_gap_dedup_enabled", False):
        dedup_of = await _dedup_first_map(project_id)

    await emit(
        "analysis.gaps.started",
        project_id=str(project_id),
        total=n_obligations,
        client_documents=n_docs,
    )

    already_scanned = set() if force else await _existing_gap_obligations(project_id)

    n_with_ev = 0
    n_persisted = 0
    n_covered = 0
    sample_errors: list[str] = []

    async for ob in _iter_obligation_findings(project_id):
        if ob["id"] in already_scanned:
            continue
        evidence_text = f"{ob['action']} {ob['supporting_text']}".strip()
        evidence = _detect_evidence(evidence_text)
        if not evidence:
            # Stamped with org_id and guarded like the audit call below: an audit-write
            # failure (for example a NULL-org INSERT rejected under enforced RLS) is
            # recorded, not allowed to abort the whole run with an HTTP 500.
            try:
                await _audit_l4_call(
                    project_id=project_id,
                    obligation_finding_id=ob["id"],
                    n_missing=0,
                    n_present=0,
                    gap_finding_id=None,
                    org_id=org_id,
                )
            except Exception as exc:
                if len(sample_errors) < 5:
                    sample_errors.append(f"{ob['id']}: audit: {exc!r}")
            continue
        n_with_ev += 1

        missing: list[Evidence] = []
        for ev in evidence:
            # present_terms is exactly the set of search terms occurring in any
            # client document (see _present_search_terms).
            if ev.search_term.lower() in present_terms:
                n_covered += 1
            else:
                missing.append(ev)

        gap_id: UUID | None = None
        if missing:
            # Compute any demotion reasons (none unless a flag is on). The gap is persisted
            # regardless; the reasons only move it to the visible context section at render.
            suppressed_by = _gap_suppressed_by(ob, dup_of=dedup_of.get(str(ob["id"])))
            try:
                gap_id = await _persist_gap(
                    project_id=project_id,
                    org_id=org_id,
                    suppressed_by=suppressed_by,
                    obligation=ob,
                    missing_evidence=missing,
                )
                n_persisted += 1
            except EmptyCitationError as exc:
                sample_errors.append(f"{ob['id']}: citation: {exc}")
            except Exception as exc:
                sample_errors.append(f"{ob['id']}: persist: {exc!r}")

        try:
            await _audit_l4_call(
                project_id=project_id,
                obligation_finding_id=ob["id"],
                n_missing=len(missing),
                n_present=len(evidence) - len(missing),
                gap_finding_id=gap_id,
                org_id=org_id,  # the owning project's tenant, required under enforced RLS
            )
        except Exception as exc:
            if len(sample_errors) < 5:
                sample_errors.append(f"{ob['id']}: audit: {exc!r}")

    total_ms = int((time.perf_counter() - t0) * 1000)

    await emit(
        "analysis.gaps.complete",
        project_id=str(project_id),
        n=n_persisted,
    )

    return L4RunReport(
        project_id=project_id,
        n_obligations_scanned=n_obligations,
        n_obligations_with_evidence=n_with_ev,
        n_gaps_persisted=n_persisted,
        n_evidence_covered=n_covered,
        n_client_documents=n_docs,
        sample_errors=sample_errors[:5],
        total_latency_ms=total_ms,
    )


# Keep the `desc` import referenced so linters do not flag it as unused.
_ = desc
