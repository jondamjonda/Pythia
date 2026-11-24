"""L1, candidate retrieval: the cheap first filter of the analysis cascade.

Given a project, return up to about 200 provisions that might apply, using only metadata filters:
jurisdiction, sector_tags, activity_tags, legal_hierarchy and validity. No LLM, embedding or
reranking. False positives are accepted by design; L2's applicability prompt does the precision
work on whatever survives this stage.

Where the corpus lacks sector_tags or activity_tags, the tag filter would return nothing, so
retrieval degrades gracefully:

1. Try the full filter (jurisdiction, sector_tags overlap, active only).
2. If too few hits, drop sector_tags (jurisdiction, active only).
3. If still too few, drop the active-only condition.

Ranking inside the cap is deterministic: regulations, then directives, statutes and methodologies,
with ties broken by publisher and then provision ordinal. Each L1 row feeds one L2 applicability
call, so a stable order means re-runs hit the same prompt cache.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy import text as sql_text

from regsuper.analysis.l0_intake import get_project
from regsuper.config import get_settings
from regsuper.corpus import NEXUS_CORPUS
from regsuper.observability.events import emit
from regsuper.pythia import client as pythia_client
from regsuper.retrieval.hybrid import RetrievalFilters, metadata_filter_sql
from regsuper.storage.database import session_scope
from regsuper.storage.models import ProvisionIdMap

# Weight of each source type in the hierarchy of legal sources; higher is more important.
LEGAL_HIERARCHY_WEIGHT: dict[str, int] = {
    "eu-regulation": 100,
    "eu-directive": 90,
    "cz-act": 80,
    "cz-zakon": 80,  # alt spelling some ingesters emit
    "cz-decree": 70,
    "cz-vyhlaska": 70,
    "cz-decision": 65,
    "cnb-methodology": 60,
    "cnb-uredni-sdeleni": 55,
    "other": 0,
}

MIN_CANDIDATES_BEFORE_DEGRADE = 30

# Map the app's sector tags onto the engine corpus's `legal_domain` taxonomy, so a
# law_source='nexus' L1 scopes the broad engine corpus (about 255 documents) to the same focus the
# app's own tag filter targets. Unmapped tags add no domain and fall back to jurisdiction scope (see
# _l1_candidates_via_nexus).
#
# Invariant, do not break: every value here must be a legal_domain branch the engine corpus actually
# populates, that is a key of pythia.graph.taxonomy.DOMAIN_CUES (mirrored in _NEXUS_DOMAIN_VOCAB
# below). A value no document carries would not degrade gracefully: the engine's
# `sd.legal_domain && $2::text[]` overlap filter would return zero rows and silently empty L1.
# Sectors whose branch the engine cannot yet scope (FOIA and transparency, social security and
# pensions, IP and copyright, the cadastre) are therefore deliberately unmapped and fall back to
# jurisdiction scope. Map them only after matching DOMAIN_CUES rows exist and the corpus has been
# re-classified.
#
# Naming: the engine's branch for company and commercial law is 'corporate', and competition law
# currently sits under 'procurement' (hospodářská soutěž is a procurement cue). The values follow
# the corpus, not the colloquial sector name.
_SECTOR_TO_NEXUS_DOMAIN: dict[str, str] = {
    # Finance.
    "fin_services": "financial-market",
    "financial_services": "financial-market",
    "investment_funds": "financial-market",
    "capital_markets": "financial-market",
    "banking": "financial-market",
    "payments": "financial-market",
    "consumer_credit": "financial-market",
    "insurance": "financial-market",
    "securities": "financial-market",
    "aml": "aml",
    # Non-finance sectors: only branches the corpus actually carries (see the corpus_inventory
    # domain census).
    "civil": "civil",
    "civil_obligations": "civil",
    "civil_procedure": "civil-procedure",
    "enforcement": "civil-procedure",
    "administrative": "administrative",
    "public_administration": "administrative",
    "labour": "labour",
    "employment": "labour",
    "tax": "tax",
    "accounting": "financial-market",  # CZ accounting/audit acts classify under financial-market
    "criminal": "criminal",
    "criminal_procedure": "criminal",
    "commercial": "corporate",
    "corporate": "corporate",
    "competition": "procurement",  # competition law currently sits in the procurement branch
    "procurement": "procurement",
    "public_procurement": "procurement",
    "data_protection": "data-protection",
    "real_property": "civil",  # substantive real-property law is in the Civil Code
    "construction": "civil",  # construction contracts are civil; the Building Act is unclassified
}

# The legal_domain branches the engine corpus is known to populate (mirroring the keys of
# pythia.graph.taxonomy.DOMAIN_CUES). A self-check so a future map edit cannot silently route L1
# into a domain no document carries, which would empty the candidate pool instead of degrading to
# jurisdiction scope.
_NEXUS_DOMAIN_VOCAB: frozenset[str] = frozenset(
    {
        "financial-market",
        "aml",
        "data-protection",
        "tax",
        "criminal",
        "civil-procedure",
        "procurement",
        "administrative",
        "labour",
        "civil",
        "corporate",
    }
)
assert set(_SECTOR_TO_NEXUS_DOMAIN.values()) <= _NEXUS_DOMAIN_VOCAB, (
    "every _SECTOR_TO_NEXUS_DOMAIN value must be a branch Nexus actually populates; "
    f"unknown: {set(_SECTOR_TO_NEXUS_DOMAIN.values()) - _NEXUS_DOMAIN_VOCAB}"
)

# Cap on provisions taken from any single source document in the nexus L1 (mirroring the regsuper
# L1's per_document_cap), so one high-centrality act cannot dominate the candidate pool.
_NEXUS_PER_DOC_CAP = 30


@dataclass(frozen=True)
class L1Candidate:
    """One row of L1 output.

    L2 reads ``provision_id`` and joins back to ``provision`` for the full text; the other fields
    are for debugging and the UI.
    """

    provision_id: UUID
    label: str | None
    legal_hierarchy: str
    jurisdiction: str
    source_external_id: str | None
    source_title: str | None
    priority_weight: int


def _priority_case_sql() -> str:
    """Build a SQL CASE expression mapping legal_hierarchy to its numeric weight.

    Inlining the dict keeps the query parameter-free; the values are trusted constants defined in
    this module.
    """
    cases = " ".join(
        f"WHEN p.legal_hierarchy = '{k}' THEN {v}"
        for k, v in LEGAL_HIERARCHY_WEIGHT.items()
        if k != "other"
    )
    default = LEGAL_HIERARCHY_WEIGHT["other"]
    return f"CASE {cases} ELSE {default} END"


async def _run_filtered(
    filters: RetrievalFilters,
    *,
    limit: int,
    per_document_cap: int = 30,
) -> list[L1Candidate]:
    """Return a stratified top N of provisions matching ``filters``.

    Takes the first ``per_document_cap`` provisions of each source document (by provision
    ordinal, since early articles carry scope and definitions), then the global top ``limit``
    from that pool. Without stratification the legal_hierarchy sort fills the cap entirely with
    EU regulations (1,638 rows) before reaching directives or Czech statutes; AIFMD would never
    enter L1, and L2 cannot classify what L1 did not return.
    """
    where, params = metadata_filter_sql(filters)
    priority = _priority_case_sql()
    sql = f"""
        WITH ranked AS (
            SELECT p.id,
                   p.label,
                   p.legal_hierarchy,
                   p.jurisdiction,
                   sd.external_id,
                   sd.title,
                   sd.publisher,
                   p.ordinal,
                   ({priority})::int AS priority_weight,
                   ROW_NUMBER() OVER (
                       PARTITION BY p.source_document_id
                       ORDER BY p.ordinal
                   ) AS within_doc_rank
              FROM provision p
              JOIN source_document sd ON sd.id = p.source_document_id
             WHERE p.is_anchor_only IS NOT TRUE {where}
        )
        SELECT id, label, legal_hierarchy, jurisdiction,
               external_id, title, priority_weight
          FROM ranked
         WHERE within_doc_rank <= :_per_doc
         ORDER BY priority_weight DESC,
                  publisher,
                  within_doc_rank,
                  ordinal
         LIMIT :_limit
    """
    params["_limit"] = int(limit)
    params["_per_doc"] = int(per_document_cap)
    async with session_scope() as session:
        rows = (await session.execute(sql_text(sql), params)).all()
    return [
        L1Candidate(
            provision_id=r[0],
            label=r[1],
            legal_hierarchy=r[2],
            jurisdiction=r[3],
            source_external_id=r[4],
            source_title=r[5],
            priority_weight=int(r[6] or 0),
        )
        for r in rows
    ]


async def candidates_for_tags(
    *,
    jurisdictions: list[str],
    sector_tags: list[str],
    top_k: int = 200,
    as_of: date | None = None,
    project_id: UUID | None = None,
    activity_tags: list[str] | None = None,
) -> list[L1Candidate]:
    """Run the three-tier degrading retrieval over the app's own provision tables, given tags.

    The stateless core of the regsuper L1: no project is loaded. Callers such as the digest and
    advisor path supply jurisdictions and sector_tags themselves. ``_l1_candidates_regsuper``
    delegates here after reading tags from project metadata, so this is the single source of
    truth for the degrade thresholds and emitted events.

    ``as_of`` asks for the law as it stood on that date. With None (the default) the active-only
    condition applies. When set, it is passed to every tier's RetrievalFilters and replaces
    active_only in the SQL (see metadata_filter_sql), so the two never stack. The date window
    holds across all three tiers: degrading widens jurisdiction and sector, never the date.

    ``project_id`` is only for event correlation; without a project the events carry the marker
    '-'. ``activity_tags`` is likewise only emitted (only sector_tags enter a filter) and defaults
    to an empty list. The regsuper path passes the project's real activity_tags for its start
    event.
    """
    pid = str(project_id) if project_id is not None else "-"

    await emit(
        "analysis.l1.start",
        project_id=pid,
        sector_tags=sector_tags,
        activity_tags=list(activity_tags or []),
        jurisdiction=list(jurisdictions),
    )

    # Tier 1, strict: jurisdiction, sector_tags overlap, active only.
    filters = RetrievalFilters(
        jurisdictions=list(jurisdictions),
        sector_tags=sector_tags,
        active_only=True,
        as_of=as_of,
    )
    rows = await _run_filtered(filters, limit=top_k)
    strategy = "strict"

    # Tier 2: drop sector_tags if the corpus is not tagged well enough.
    if len(rows) < MIN_CANDIDATES_BEFORE_DEGRADE:
        filters = RetrievalFilters(
            jurisdictions=list(jurisdictions),
            sector_tags=[],
            active_only=True,
            as_of=as_of,
        )
        rows = await _run_filtered(filters, limit=top_k)
        strategy = "jurisdiction+active"

    # Tier 3: drop active_only if even jurisdiction alone yields too few.
    if len(rows) < MIN_CANDIDATES_BEFORE_DEGRADE:
        filters = RetrievalFilters(
            jurisdictions=list(jurisdictions),
            sector_tags=[],
            active_only=False,
            as_of=as_of,
        )
        rows = await _run_filtered(filters, limit=top_k)
        strategy = "jurisdiction_only"

    await emit(
        "analysis.l1.complete",
        project_id=pid,
        n_candidates=len(rows),
        strategy=strategy,
    )
    return rows


async def _l1_candidates_regsuper(
    project_id: UUID,
    *,
    top_k: int = 200,
    as_of: date | None = None,
) -> list[L1Candidate]:
    """Retrieve candidates from the app's own provision tables (law_source='regsuper').

    Loads the project, reads filters from its metadata and delegates the three-tier strategy
    (and its progress events) to ``candidates_for_tags``. ``as_of`` (the law as it stood on that
    date; None means current law) is passed through.
    """
    project = await get_project(project_id)
    if project is None:
        raise LookupError(f"project {project_id} not found")

    meta = project.metadata_ or {}
    sector_tags: list[str] = list(meta.get("sector_tags") or [])
    activity_tags: list[str] = list(meta.get("activity_tags") or [])

    return await candidates_for_tags(
        jurisdictions=list(project.jurisdiction_scope or []),
        sector_tags=sector_tags,
        top_k=top_k,
        as_of=as_of,
        project_id=project_id,
        activity_tags=activity_tags,
    )


async def l1_candidates(
    project_id: UUID,
    *,
    top_k: int = 200,
    as_of: date | None = None,
) -> list[L1Candidate]:
    """Return L1 candidates for a project from the configured law source.

    ``law_source='regsuper'`` (the default) retrieves from the app's own provision tables;
    ``law_source='nexus'`` uses the engine's canonical corpus through the Pythia sidecar. Switching
    back is just a config change.

    ``as_of`` (None means current law) is honoured end to end on the regsuper path. The nexus path
    accepts it but does not yet pass it to the engine.
    """
    if get_settings().law_source == "nexus":
        return await _l1_candidates_via_nexus(project_id, top_k=top_k, as_of=as_of)
    return await _l1_candidates_regsuper(project_id, top_k=top_k, as_of=as_of)


async def _l1_candidates_via_nexus(
    project_id: UUID,
    *,
    top_k: int = 200,
    as_of: date | None = None,  # accepted but not yet passed to pythia_client.candidates()
) -> list[L1Candidate]:
    """Retrieve candidates from the engine's canonical corpus (law_source='nexus').

    Calls the sidecar's ``/v1/candidates``, filtered by jurisdiction and ranked by graph
    centrality. The returned provision ids are nexus ids, so downstream steps (L2 text fetch,
    findings) must resolve them through the sidecar's ``/v1/provisions``. Requires pythia_enabled
    and pythia_url; otherwise PythiaNotConfigured propagates.

    Scopes by jurisdiction and the project's legal domains (its sector tags mapped onto the
    engine's ``legal_domain`` axis). As with the regsuper L1, false positives are accepted and L2
    does the precision pass. ``shadow_compare_l1`` measures agreement with the app's own L1 before
    a cutover.
    """
    project = await get_project(project_id)
    if project is None:
        raise LookupError(f"project {project_id} not found")
    jurisdictions = list(project.jurisdiction_scope or [])
    sector_tags = list((project.metadata_ or {}).get("sector_tags") or [])
    domains = sorted(
        {_SECTOR_TO_NEXUS_DOMAIN[t] for t in sector_tags if t in _SECTOR_TO_NEXUS_DOMAIN}
    )

    await emit(
        "analysis.l1.start",
        project_id=str(project_id),
        law_source="nexus",
        jurisdiction=jurisdictions,
        legal_domain=domains,
    )
    # Fetch a larger centrality-ranked pool, then cap provisions per source document so one
    # high-centrality act cannot fill the pool. This mirrors the regsuper L1's per_document_cap and
    # keeps the two L1s' candidate spreads comparable for shadow_compare_l1.
    pool = await pythia_client.candidates(
        jurisdiction=jurisdictions or None, legal_domain=domains or None, limit=max(top_k * 4, 800)
    )
    per_doc: dict[str, int] = {}
    rows: list[dict] = []
    for r in pool:  # pool arrives centrality-ranked
        ext = r.get("external_id") or ""
        if per_doc.get(ext, 0) >= _NEXUS_PER_DOC_CAP:
            continue
        per_doc[ext] = per_doc.get(ext, 0) + 1
        rows.append(r)
        if len(rows) >= top_k:
            break
    out = [
        L1Candidate(
            provision_id=UUID(r["id"]),
            label=r.get("label"),
            legal_hierarchy=r.get("legal_hierarchy") or "other",
            jurisdiction=r.get("jurisdiction") or "",
            source_external_id=r.get("external_id"),
            source_title=r.get("source_title"),
            priority_weight=max(top_k - i, 0),  # preserve the engine's centrality order
        )
        for i, r in enumerate(rows)
    ]
    await emit(
        "analysis.l1.complete",
        project_id=str(project_id),
        law_source="nexus",
        n_candidates=len(out),
        strategy="nexus_domain" if domains else "nexus_jurisdiction",
    )
    return out


async def crosswalk_scoped_nexus_candidates(
    project_id: UUID,
    *,
    top_k: int = 200,
    as_of: date | None = None,
) -> list[L1Candidate]:
    """Return nexus L1 candidates restricted to provisions the app corpus already holds.

    Used only by the offline evaluation that generates the lawyer-review sample. A provision is
    included only if it has a regsuper-to-nexus crosswalk row.

    The broad nexus L1 (``_l1_candidates_via_nexus``) draws from the whole engine corpus, hundreds
    of acts. A nexus finding's ``cited_documents`` resolve app-side through the crosswalk
    (``provision_id_map``), which covers only the handful of acts the app has ingested. A provision
    from any other act has no app ``source_document``, so L2 and L3 drop it (the under-coverage
    guard). The LLM tends to find exactly those non-crosswalked provisions applicable, so a
    broad-corpus run persists almost no findings and cannot produce the 50-plus finding sample
    the lawyer review needs.

    This scope avoids that ceiling without widening the app corpus. It runs the app's own L1 (the
    same tag and jurisdiction scoping the live product uses) and maps each app provision_id to its
    nexus_id through the crosswalk. Every candidate is crosswalked by construction, so the nexus
    cascade reads current law from the engine while every cited document still resolves. The
    sample is therefore a current-law analysis of the acts the app already has: real, if narrow.

    Returns L1Candidates carrying nexus ids (so L2's ``_load_provision_for_l2_via_nexus`` fetches
    the engine's current text), in the app L1's priority order, with the act metadata (label,
    hierarchy, title) unchanged: it is the same act in a different id space. A regsuper provision
    without a crosswalk row (unexpected at full coverage) is omitted and counted in the emitted
    event, never substituted.

    Not a serving path: only scripts/cutover_eval_harness.py calls it, inside cutover_eval_mode().
    """
    rs = await _l1_candidates_regsuper(project_id, top_k=top_k, as_of=as_of)
    if not rs:
        await emit(
            "analysis.l1.crosswalk_scoped",
            project_id=str(project_id),
            n_regsuper=0,
            n_mapped=0,
            n_unmapped=0,
        )
        return []

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ProvisionIdMap.regsuper_id, ProvisionIdMap.nexus_id).where(
                    ProvisionIdMap.regsuper_id.in_([c.provision_id for c in rs]),
                    ProvisionIdMap.nexus_corpus == NEXUS_CORPUS,
                )
            )
        ).all()
    rs_to_nexus: dict[UUID, UUID] = {rid: nid for rid, nid in rows}

    out: list[L1Candidate] = []
    for c in rs:  # preserve the app L1's priority order
        nexus_id = rs_to_nexus.get(c.provision_id)
        if nexus_id is None:
            continue  # no crosswalk row: omit it (counted below)
        out.append(replace(c, provision_id=nexus_id))  # swap to the nexus id; keep the act metadata

    await emit(
        "analysis.l1.crosswalk_scoped",
        project_id=str(project_id),
        n_regsuper=len(rs),
        n_mapped=len(out),
        n_unmapped=len(rs) - len(out),
    )
    return out


async def shadow_compare_l1(project_id: UUID, *, top_k: int = 200) -> dict:
    """Run L1 over both corpora and compare the results (read-only).

    Overlap is keyed on (source_external_id, normalised label), because provision UUIDs differ
    between corpora. Changes no app state; use it to check that the engine corpus covers the app's
    law before switching to law_source='nexus'.
    """
    rs = await _l1_candidates_regsuper(project_id, top_k=top_k)
    nx = await _l1_candidates_via_nexus(project_id, top_k=top_k)

    def _key(c: L1Candidate) -> tuple[str, str]:
        return (str(c.source_external_id or ""), (c.label or "").strip().lower())

    rs_keys, nx_keys = {_key(c) for c in rs}, {_key(c) for c in nx}
    shared = rs_keys & nx_keys
    return {
        "project_id": str(project_id),
        "regsuper_n": len(rs),
        "nexus_n": len(nx),
        "overlap_by_ref": len(shared),
        "regsuper_only": len(rs_keys - nx_keys),
        "nexus_only": len(nx_keys - rs_keys),
        "regsuper_recall_in_nexus": round(len(shared) / len(rs_keys), 3) if rs_keys else None,
    }


async def l1_candidate_ids(project_id: UUID, *, top_k: int = 200) -> list[UUID]:
    """Return just the candidate provision ids (all that L2 needs)."""
    rows = await l1_candidates(project_id, top_k=top_k)
    return [r.provision_id for r in rows]
