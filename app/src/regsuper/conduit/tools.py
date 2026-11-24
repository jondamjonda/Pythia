"""Conduit tool implementations, built on the existing engine calls.

Each tool wraps an existing engine call rather than reimplementing it, and returns its result
through `envelope.finalize`, so a tool either returns cited data or refuses:

* search_czech_law: `regsuper.retrieval.retrieve`.
* get_provision: the `expand()` helper behind `GET /api/provisions/{id}`; the provision is its
  own citation.
* whats_in_force: `pythia.client.act(external_id, as_of=...)` when `law_source='nexus'`,
  otherwise a direct SQL query. No compliance gate.
* list_obligations: corpus-level obligations ("rules for businesses of this type"). Does not
  run the blocking L0 to L4 analysis cascade.
* check_compliance: the compliance guard runs first and refuses reserved legal advice without
  any retrieval (still metered).
* answer_legal_question: grounded answer synthesis with compliance gates before and after.
  Not implemented yet.

There is deliberately no case-law or "find precedents" tool, and none should be added; the
engine's rulings endpoint stays internal.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from regsuper.conduit.envelope import (
    Citation,
    Envelope,
    RefusalReason,
    Trust,
    finalize,
)
from regsuper.conduit.schemas import (
    AnswerLegalQuestionInput,
    CheckComplianceInput,
    Domain,
    GetProvisionInput,
    ListObligationsInput,
    SearchCzechLawInput,
    WhatsInForceInput,
)

_WP = "WP-062 (Stage-2 Conduit build)"

# Maximum snippet length in search results; callers fetch full text with get_provision.
_SNIPPET_MAX = 300

# Maps the public `domains` enum onto corpus `sector_tags` (not onto the free-text `sector`
# hint). The enum values currently equal the corpus tags, so this is an identity mapping; it
# exists so that renaming a corpus tag does not change the public API.
_DOMAIN_TO_TAG: dict[Domain, str] = {
    "gdpr": "gdpr",
    "aml": "aml",
    "corporate": "corporate",
    "contracts": "contracts",
    "employment": "employment",
    "financial-market": "financial-market",
    "civil": "civil",
    "tax": "tax",
    "real-estate": "real-estate",
}


def _domains_to_tags(domains: list[Domain] | None) -> list[str]:
    if not domains:
        return []
    return [_DOMAIN_TO_TAG[d] for d in domains if d in _DOMAIN_TO_TAG]


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d is not None else None


def _in_force_on(valid_from: date | None, valid_to: date | None, as_of: date) -> bool:
    """Return True if a provision is in force on `as_of`.

    That is, `valid_from <= as_of` and (`valid_to` is NULL or `valid_to > as_of`). A NULL
    `valid_from` counts as in force: some EU acts carry no date, and they should neither get
    an invented date nor be silently dropped.
    """
    if valid_from is not None and valid_from > as_of:
        return False
    if valid_to is not None and valid_to <= as_of:
        return False
    return True


def _status_for(valid_from: date | None, valid_to: date | None, as_of: date) -> str:
    """Return in_force, not_yet_in_force or repealed from `valid_from` and `valid_to` alone.

    The corpus holds a single snapshot per provision, so no amendment history is invented; the
    envelope marks such results with grounding='fuzzy'.
    """
    if valid_from is not None and valid_from > as_of:
        return "not_yet_in_force"
    if valid_to is not None and valid_to <= as_of:
        return "repealed"
    return "in_force"


async def _temporal_for(provision_ids: list[UUID]) -> dict[UUID, tuple[date | None, date | None]]:
    """Read `valid_from` and `valid_to` for the candidate provisions (read-only, one query).

    Retrieval payloads carry no dates, so the as_of filter and the citation effective dates
    need this lookup. The database is imported lazily so importing this module does not load
    the engine.
    """
    if not provision_ids:
        return {}
    from sqlalchemy import text as sql_text

    from regsuper.storage.database import session_scope

    sql = "SELECT id, valid_from, valid_to FROM provision WHERE id = ANY(:ids)"
    async with session_scope() as session:
        rows = (await session.execute(sql_text(sql), {"ids": provision_ids})).all()
    return {r[0]: (r[1], r[2]) for r in rows}


def _grounding_for_search(result_top_score: float | None, flagged: bool) -> str:
    """Map the retrieval state to the envelope's grounding label.

    A reranked, scored result is 'verified' because a calibrated relevance score backs the
    ranking. A skipped rerank (for example no reranker key, hence no calibrated score) is
    'unverified'.
    """
    if flagged or result_top_score is None:
        return "unverified"
    return "verified"


async def search_czech_law(req: SearchCzechLawInput) -> Envelope:
    """Search the corpus for a topic and return ranked, cited provisions.

    Built on `regsuper.retrieval.retrieve`. The result's refusal, flag and rerank score feed
    the envelope's trust block, and provisions not in force on `as_of` are filtered out. Every
    surviving result carries a citation; a topic outside the corpus is refused with
    no_coverage by `finalize`.
    """
    # Imported lazily to keep module import free of the heavy retrieval stack.
    from regsuper.retrieval import RetrievalFilters, retrieve

    as_of = req.as_of
    today = date.today()
    point_in_time = as_of is not None and as_of != today
    effective_as_of = as_of or today

    jurisdictions = list(req.jurisdiction) if req.jurisdiction else []
    sector_tags = _domains_to_tags(req.domains)
    filters = RetrievalFilters(
        jurisdictions=jurisdictions,
        sector_tags=sector_tags,
        # For a past or future date, disable active_only and filter by date below; for today,
        # active_only does the filtering.
        active_only=not point_in_time,
        as_of=as_of if point_in_time else None,
    )

    result = await retrieve(
        req.query,
        jurisdiction=", ".join(jurisdictions),
        filters=filters,
        top_n_rerank=req.top_k,
        use_cache=True,
    )

    disclaimer_as_of = _iso(effective_as_of)
    grounding = _grounding_for_search(result.top_rerank_score, result.flagged)
    trust = Trust(
        in_force_as_of=disclaimer_as_of,
        confidence=result.top_rerank_score,
        confidence_source=result.refusal_source
        or ("calibrated" if grounding == "verified" else "fallback"),
        grounding=grounding,
    )
    meta = {"tool": "search_czech_law", "n_candidates": len(result.candidates)}

    # Retrieval refused (top score below the calibrated floor): refuse with no_coverage rather
    # than surface candidates it judged too weak.
    if result.refused:
        return finalize(
            None,
            [],
            refused=True,
            refusal_reason=RefusalReason.NO_COVERAGE,
            as_of=disclaimer_as_of,
            jurisdiction=jurisdictions,
            trust=trust,
            meta=meta,
        )

    temporal = await _temporal_for([h.provision_id for h in result.candidates])

    results: list[dict[str, object]] = []
    citations: list[Citation] = []
    rank = 0
    for hit in result.candidates:
        pid = hit.provision_id
        valid_from, valid_to = temporal.get(pid, (None, None))
        # Exclude provisions not in force on as_of.
        if point_in_time and not _in_force_on(valid_from, valid_to, effective_as_of):
            continue
        payload = result.payloads.get(pid) or {}
        text = (payload.get("text") or "").strip()
        snippet = text[:_SNIPPET_MAX]
        rank += 1
        ext = payload.get("external_id")
        label = payload.get("label")
        title = payload.get("title")
        hierarchy = payload.get("legal_hierarchy")
        jur = payload.get("jurisdiction")
        results.append(
            {
                "rank": rank,
                "provision_id": str(pid),
                "external_id": ext,
                "label": label,
                "act_title": title,
                "legal_hierarchy": hierarchy,
                "jurisdiction": jur,
                "snippet": snippet,
                "effective_from": _iso(valid_from),
                "effective_to": _iso(valid_to),
                "relevance": hit.rerank_score,
            }
        )
        citations.append(
            Citation(
                provision_id=str(pid),
                external_id=ext,
                label=label,
                act_title=title,
                legal_hierarchy=hierarchy,
                jurisdiction=jur,
                effective_from=_iso(valid_from),
                effective_to=_iso(valid_to),
            )
        )

    data = {"results": results, "n_results": len(results)}
    # No results means no citations, so finalize refuses with no_coverage.
    return finalize(
        data,
        citations,
        as_of=disclaimer_as_of,
        jurisdiction=jurisdictions,
        trust=trust,
        meta=meta,
    )


async def get_provision(req: GetProvisionInput) -> Envelope:
    """Return one provision with its full text, breadcrumb, cross-references and definitions.

    Built on the `expand()` helper; the returned provision is its own citation, and there is
    no compliance gate. An id or locator that resolves to nothing is refused with
    out_of_scope.
    """
    # Imported lazily because expand() pulls in the database engine.
    from regsuper.retrieval.expand import expand

    as_of = req.as_of
    today = date.today()
    effective_as_of = as_of or today
    disclaimer_as_of = _iso(effective_as_of)
    jurisdiction_echo: list[str] = []
    meta = {"tool": "get_provision"}

    pid = await _resolve_provision_id(req.act, req.section)
    if pid is None:
        # Unknown id or locator: out_of_scope rather than no_coverage, because there is no
        # candidate at all ("we do not carry this").
        return finalize(
            None,
            [],
            refused=True,
            refusal_reason=RefusalReason.OUT_OF_SCOPE,
            as_of=disclaimer_as_of,
            jurisdiction=jurisdiction_echo,
            meta=meta,
        )

    expanded = await expand(pid)
    if expanded is None or expanded.hit is None:
        return finalize(
            None,
            [],
            refused=True,
            refusal_reason=RefusalReason.OUT_OF_SCOPE,
            as_of=disclaimer_as_of,
            jurisdiction=jurisdiction_echo,
            meta=meta,
        )

    hit = expanded.hit
    valid_from, valid_to = await _single_temporal(pid)
    external_id, act_title = await _act_meta(hit.source_document_id)
    jurisdiction_echo = [hit.jurisdiction] if hit.jurisdiction else []

    breadcrumb = [
        {"id": str(a.id), "label": a.label}
        # expand() returns ancestors from child to root; render them top-down (act first).
        for a in reversed(expanded.ancestors)
    ]
    cross_refs = []
    xref_meta = await _xref_external_ids([x.id for x in expanded.cross_references])
    for x in expanded.cross_references:
        cross_refs.append({"id": str(x.id), "label": x.label, "external_id": xref_meta.get(x.id)})
    definitions = [
        {
            "term": d.term,
            "definition_text": d.definition_text,
            "definition_provision_id": str(d.definition_provision_id)
            if d.definition_provision_id
            else None,
        }
        for d in expanded.definitions
    ]

    data = {
        "provision_id": str(pid),
        "label": hit.label,
        "text": hit.text,
        "text_format": "markdown",
        "act_title": act_title,
        "external_id": external_id,
        "legal_hierarchy": hit.legal_hierarchy,
        "jurisdiction": hit.jurisdiction,
        "effective_from": _iso(valid_from),
        "effective_to": _iso(valid_to),
        "breadcrumb": breadcrumb,
        "cross_references": cross_refs,
        "definitions": definitions,
    }
    # The returned provision is its own citation, so finalize always has one.
    citation = Citation(
        provision_id=str(pid),
        external_id=external_id,
        label=hit.label,
        act_title=act_title,
        legal_hierarchy=hit.legal_hierarchy,
        jurisdiction=hit.jurisdiction,
        effective_from=_iso(valid_from),
        effective_to=_iso(valid_to),
    )
    trust = Trust(in_force_as_of=disclaimer_as_of, grounding="verified")
    return finalize(
        data,
        [citation],
        as_of=disclaimer_as_of,
        jurisdiction=jurisdiction_echo,
        trust=trust,
        meta=meta,
    )


async def whats_in_force(req: WhatsInForceInput) -> Envelope:
    """Return an act's provisions and their status on a given date.

    Uses the engine's act endpoint (`law_source='nexus'`) or a direct SQL query; there is no
    compliance gate. The corpus holds a single snapshot, so `status` is derived from
    `valid_from` and `valid_to` only and grounding is 'fuzzy'; no amendment history is
    invented.
    """
    as_of = req.as_of
    today = date.today()
    effective_as_of = as_of or today
    disclaimer_as_of = _iso(effective_as_of)
    meta = {"tool": "whats_in_force"}

    rows = await _act_provisions(req.act, effective_as_of)
    if rows is None or not rows:
        # Unknown act or no provisions: out_of_scope rather than no_coverage, because we do
        # not carry the act at all.
        return finalize(
            None,
            [],
            refused=True,
            refusal_reason=RefusalReason.OUT_OF_SCOPE,
            as_of=disclaimer_as_of,
            jurisdiction=[],
            meta=meta,
        )

    act_external_id, act_title, act_jurisdiction, provisions = rows
    # The act counts as in force on as_of if any of its provisions is.
    act_in_force = any(p["status"] == "in_force" for p in provisions)

    citations: list[Citation] = [
        Citation(
            provision_id=p["provision_id"],
            external_id=act_external_id,
            label=p["label"],
            act_title=act_title,
            jurisdiction=act_jurisdiction,
            effective_from=p["effective_from"],
            effective_to=p["effective_to"],
        )
        for p in provisions
    ]
    data = {
        "act_external_id": act_external_id,
        "act_title": act_title,
        "as_of": disclaimer_as_of,
        "in_force": act_in_force,
        "provisions": provisions,
    }
    # Status comes from a single snapshot, so grounding is 'fuzzy'.
    trust = Trust(in_force_as_of=disclaimer_as_of, grounding="fuzzy")
    jurisdiction_echo = [act_jurisdiction] if act_jurisdiction else []
    return finalize(
        data,
        citations,
        as_of=disclaimer_as_of,
        jurisdiction=jurisdiction_echo,
        trust=trust,
        meta=meta,
    )


# ---------------------------------------------------------------------------
# Read-only database helpers (engine imported lazily; no writes, no model spend)
# ---------------------------------------------------------------------------
async def _resolve_provision_id(act: str, section: str | None) -> UUID | None:
    """Resolve the `act` (+ optional `section`) input to a provision id.

    `act` may be a provision UUID, an act external id ('240/2013'), or a locator that embeds
    a section ('240/2013 §15'). A UUID is used directly; anything else is resolved through
    `source_document.external_id` and the provision `label`. Returns None when nothing
    resolves (the caller then refuses with out_of_scope).
    """
    from sqlalchemy import text as sql_text

    from regsuper.storage.database import session_scope

    raw = (act or "").strip()
    # 1) A bare UUID is used directly.
    try:
        return UUID(raw)
    except (ValueError, AttributeError):
        pass

    # 2) Split an embedded locator like '240/2013 §15' into act + section.
    ext = raw
    sect = section
    if sect is None and " " in raw:
        ext, _, tail = raw.partition(" ")
        tail = tail.strip()
        if tail:
            sect = tail

    params: dict[str, object] = {"ext": ext}
    label_clause = ""
    if sect:
        # Match the section label loosely (the chunker stores '§ 15', '§15', etc.).
        label_clause = "AND replace(lower(p.label), ' ', '') LIKE replace(lower(:sect), ' ', '')"
        params["sect"] = f"%{sect}%"
    sql = f"""
        SELECT p.id
          FROM provision p
          JOIN source_document sd ON sd.id = p.source_document_id
         WHERE sd.external_id = :ext
           {label_clause}
         ORDER BY nlevel(p.hierarchy_path) ASC
         LIMIT 1
    """
    async with session_scope() as session:
        row = (await session.execute(sql_text(sql), params)).first()
    return row[0] if row is not None else None


async def _single_temporal(provision_id: UUID) -> tuple[date | None, date | None]:
    temporal = await _temporal_for([provision_id])
    return temporal.get(provision_id, (None, None))


async def _act_meta(source_document_id: UUID) -> tuple[str | None, str | None]:
    """Return (external_id, title) for a source document (the act header)."""
    from sqlalchemy import text as sql_text

    from regsuper.storage.database import session_scope

    sql = "SELECT external_id, title FROM source_document WHERE id = :sid"
    async with session_scope() as session:
        row = (await session.execute(sql_text(sql), {"sid": source_document_id})).first()
    if row is None:
        return None, None
    return row[0], row[1]


async def _xref_external_ids(ids: list[UUID]) -> dict[UUID, str | None]:
    """Map each cross-referenced provision id to its act's external_id.

    Used for the `cross_references[*].external_id` field.
    """
    if not ids:
        return {}
    from sqlalchemy import text as sql_text

    from regsuper.storage.database import session_scope

    sql = """
        SELECT p.id, sd.external_id
          FROM provision p
          JOIN source_document sd ON sd.id = p.source_document_id
         WHERE p.id = ANY(:ids)
    """
    async with session_scope() as session:
        rows = (await session.execute(sql_text(sql), {"ids": ids})).all()
    return {r[0]: r[1] for r in rows}


async def _act_provisions(
    act: str, as_of: date
) -> tuple[str | None, str | None, str | None, list[dict[str, object]]] | None:
    """Resolve an act and return (external_id, title, jurisdiction, provisions[]).

    Uses the engine's `act()` endpoint when `law_source='nexus'` and Pythia is enabled,
    falling back to a read-only SQL query over the app's `provision` and `source_document`
    tables. Returns None when the act is unknown (the caller refuses with out_of_scope). An
    engine failure falls back to the SQL query rather than surfacing as a 500.
    """
    from regsuper.config import get_settings

    settings = get_settings()
    if settings.law_source == "nexus" and settings.pythia_enabled and settings.pythia_url:
        nexus = await _act_provisions_nexus(act, as_of)
        if nexus is not None:
            return nexus
        # Engine unreachable or act unknown there: fall back to the app database.
    return await _act_provisions_app_db(act, as_of)


async def _act_provisions_nexus(
    act: str, as_of: date
) -> tuple[str | None, str | None, str | None, list[dict[str, object]]] | None:
    from regsuper.pythia import client as pythia

    try:
        tree = await pythia.act(act, as_of=as_of.isoformat())
    except Exception:  # any engine failure falls back to the app database, never a 500
        return None
    if not isinstance(tree, dict):
        return None
    raw_provisions = tree.get("provisions") or []
    provisions: list[dict[str, object]] = []
    for p in raw_provisions:
        if not isinstance(p, dict):
            continue
        vf = _parse_iso_date(p.get("valid_from"))
        vt = _parse_iso_date(p.get("valid_to"))
        pid = p.get("id") or p.get("provision_id")
        provisions.append(
            {
                "provision_id": str(pid) if pid is not None else None,
                "label": p.get("label"),
                "status": _status_for(vf, vt, as_of),
                "effective_from": _iso(vf),
                "effective_to": _iso(vt),
            }
        )
    if not provisions:
        return None
    return (
        tree.get("external_id") or act,
        tree.get("title"),
        tree.get("jurisdiction"),
        provisions,
    )


async def _act_provisions_app_db(
    act: str, as_of: date
) -> tuple[str | None, str | None, str | None, list[dict[str, object]]] | None:
    from sqlalchemy import text as sql_text

    from regsuper.storage.database import session_scope

    sql = """
        SELECT p.id, p.label, p.valid_from, p.valid_to,
               sd.external_id, sd.title, p.jurisdiction
          FROM provision p
          JOIN source_document sd ON sd.id = p.source_document_id
         WHERE sd.external_id = :ext
         ORDER BY p.hierarchy_path ASC
    """
    async with session_scope() as session:
        rows = (await session.execute(sql_text(sql), {"ext": act})).all()
    if not rows:
        return None
    provisions: list[dict[str, object]] = []
    act_external_id = rows[0][4]
    act_title = rows[0][5]
    act_jurisdiction = rows[0][6]
    for r in rows:
        vf, vt = r[2], r[3]
        provisions.append(
            {
                "provision_id": str(r[0]),
                "label": r[1],
                "status": _status_for(vf, vt, as_of),
                "effective_from": _iso(vf),
                "effective_to": _iso(vt),
            }
        )
    return act_external_id, act_title, act_jurisdiction, provisions


def _parse_iso_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# list_obligations and check_compliance
# ---------------------------------------------------------------------------
# Obligations are framed from each provision's own deontic fields (modal, actor, action), not
# from a per-project finding: they are corpus-level "rules of this type", with no analysis
# cascade involved.
async def _deontic_for(
    provision_ids: list[UUID],
) -> dict[UUID, dict[str, object]]:
    """Read the deontic fields, label and act for the candidate provisions.

    Read-only, one query. Returns a map from provision_id to {modal, actor, action, condition,
    label, external_id, valid_from, valid_to}. A provision without a `deontic_modal` is not an
    obligation statement, and callers skip it, so an obligation is emitted only when the
    corpus marks the provision as imposing one.
    """
    if not provision_ids:
        return {}
    from sqlalchemy import text as sql_text

    from regsuper.storage.database import session_scope

    sql = """
        SELECT p.id, p.deontic_modal, p.deontic_actor, p.deontic_action,
               p.deontic_condition, p.label, sd.external_id, p.valid_from, p.valid_to
          FROM provision p
          JOIN source_document sd ON sd.id = p.source_document_id
         WHERE p.id = ANY(:ids)
    """
    async with session_scope() as session:
        rows = (await session.execute(sql_text(sql), {"ids": provision_ids})).all()
    out: dict[UUID, dict[str, object]] = {}
    for r in rows:
        out[r[0]] = {
            "modal": r[1],
            "actor": r[2],
            "action": r[3],
            "condition": r[4],
            "label": r[5],
            "external_id": r[6],
            "valid_from": r[7],
            "valid_to": r[8],
        }
    return out


# The recognised deontic modals (the closed set in the tool's output schema). A provision whose
# modal is empty or outside this set is skipped; no obligation type is ever invented.
_MODAL_VOCAB = frozenset({"shall", "may", "must_not", "should"})


def _profile_query(req: ListObligationsInput) -> str:
    """Build a retrieval query from the structured business profile.

    Pure. Only the declared sector and entity type are used (structured fields, never free
    text), so list_obligations stays abstract: "rules of this type", not a personalised
    assessment.
    """
    parts = [p for p in (req.sector, req.entity_type) if p]
    base = " ".join(parts) if parts else "compliance obligations"
    return f"povinnosti {base}".strip()


async def list_obligations(req: ListObligationsInput) -> Envelope:
    """Return corpus-level obligations for a business type.

    Returns "rules that apply to businesses of this type" and does not run the blocking L0 to
    L4 analysis cascade. Like search_czech_law it uses the read-only `retrieve` path, here over
    a query built from the structured profile fields, then frames each matched provision's own
    deontic fields (modal, actor, action) as an obligation cited by that provision. No user
    free text or per-org documents are involved. If no provision matches, or none carries a
    recognised deontic modal, the result is refused with no_coverage; an uncited or invented
    obligation is never returned.
    """
    from regsuper.retrieval import RetrievalFilters, retrieve

    as_of = req.as_of
    today = date.today()
    point_in_time = as_of is not None and as_of != today
    effective_as_of = as_of or today
    disclaimer_as_of = _iso(effective_as_of)

    jurisdictions = list(req.jurisdiction) if req.jurisdiction else []
    # `sector` is passed through as a corpus sector tag; `entity_type` only shapes the query.
    sector_tags = [t for t in (req.sector,) if t]
    filters = RetrievalFilters(
        jurisdictions=jurisdictions,
        sector_tags=sector_tags,
        active_only=not point_in_time,
        as_of=as_of if point_in_time else None,
    )
    result = await retrieve(
        _profile_query(req),
        jurisdiction=", ".join(jurisdictions),
        filters=filters,
        use_cache=True,
    )

    trust = Trust(
        in_force_as_of=disclaimer_as_of,
        confidence=result.top_rerank_score,
        grounding=_grounding_for_search(result.top_rerank_score, result.flagged),
    )
    meta = {"tool": "list_obligations", "n_candidates": len(result.candidates)}

    if result.refused or not result.candidates:
        return finalize(
            None,
            [],
            refused=True,
            refusal_reason=RefusalReason.NO_COVERAGE,
            as_of=disclaimer_as_of,
            jurisdiction=jurisdictions,
            trust=trust,
            meta=meta,
        )

    candidate_ids = [h.provision_id for h in result.candidates]
    deontic = await _deontic_for(candidate_ids)

    obligations: list[dict[str, object]] = []
    citations: list[Citation] = []
    considered = 0
    for hit in result.candidates:
        pid = hit.provision_id
        d = deontic.get(pid)
        if not d:
            continue
        considered += 1
        modal = (d.get("modal") or "").strip().lower() if isinstance(d.get("modal"), str) else None
        # Emit only obligations the corpus actually marks (a recognised modal).
        if modal not in _MODAL_VOCAB:
            continue
        payload = result.payloads.get(pid) or {}
        ext = d.get("external_id") or payload.get("external_id")
        label = d.get("label") or payload.get("label")
        vf = d.get("valid_from") if isinstance(d.get("valid_from"), date) else None
        vt = d.get("valid_to") if isinstance(d.get("valid_to"), date) else None
        obligations.append(
            {
                "actor": d.get("actor"),
                "modal": modal,
                "action": d.get("action"),
                "conditions": [d["condition"]] if d.get("condition") else [],
                "provision_id": str(pid),
                "external_id": ext,
                "label": label,
            }
        )
        citations.append(
            Citation(
                provision_id=str(pid),
                external_id=ext,
                label=label,
                act_title=payload.get("title"),
                legal_hierarchy=payload.get("legal_hierarchy"),
                jurisdiction=payload.get("jurisdiction"),
                effective_from=_iso(vf),
                effective_to=_iso(vt),
            )
        )

    data = {
        "obligations": obligations,
        "n_obligations": len(obligations),
        "applicable_provisions_considered": considered,
    }
    # No cited obligations means no citations, so finalize refuses.
    return finalize(
        data,
        citations,
        as_of=disclaimer_as_of,
        jurisdiction=jurisdictions,
        trust=trust,
        meta=meta,
    )


# Lawyer hand-off returned with a reserved-advice refusal.
_LAWYER_HANDOFF = {
    "reason": "This request reads as advice reserved to an advokát; routed to a lawyer.",
    "referral_url": "https://www.advokatikomora.cz/",
}


async def check_compliance(req: CheckComplianceInput) -> Envelope:
    """Return the law governing a described situation and its general obligations.

    Never gives advice. The compliance guard always runs first:

    1. `compliance_guard.classify_answer_mode(text=situation, where="input")`. A verdict of
       `reserved_advice` (or `needs_review`, treated the same way to fail closed) is refused
       without any retrieval, with `needs_lawyer=True`, `refusal_reason='reserved_advice'` and
       a lawyer hand-off. Metering is the caller's job (each tool call is meant to be wrapped
       in `meter.precheck` and `meter.record`), so a refusal is still counted and abuse stays
       visible.
    2. Otherwise, run retrieval over the situation text and return the governing provisions
       and their general obligations (from the same deontic fields as list_obligations), each
       cited. With no coverage, the result is refused with no_coverage.
    """
    from regsuper.analysis import compliance_guard

    as_of = req.as_of
    today = date.today()
    point_in_time = as_of is not None and as_of != today
    effective_as_of = as_of or today
    disclaimer_as_of = _iso(effective_as_of)
    jurisdictions = list(req.jurisdiction) if req.jurisdiction else []
    meta = {"tool": "check_compliance"}

    # Input gate, which always runs before any retrieval.
    verdict = await compliance_guard.classify_answer_mode(text=req.situation, where="input")
    if verdict.answer_mode in ("reserved_advice", "needs_review"):
        # Refuse without retrieval or any engine call, with the hand-off populated (the caller
        # still meters it). needs_review is treated as reserved_advice, to err on the safe
        # side.
        return finalize(
            {"lawyer_handoff": dict(_LAWYER_HANDOFF)},
            [],  # a refusal makes no claim, so no citation; finalize forces data=None
            refused=True,
            refusal_reason=RefusalReason.RESERVED_ADVICE,
            needs_lawyer=True,
            as_of=disclaimer_as_of,
            jurisdiction=jurisdictions,
            meta=meta,
        )

    # Information request: run retrieval over the situation text.
    from regsuper.retrieval import RetrievalFilters, retrieve

    filters = RetrievalFilters(
        jurisdictions=jurisdictions,
        active_only=not point_in_time,
        as_of=as_of if point_in_time else None,
    )
    result = await retrieve(
        req.situation,
        jurisdiction=", ".join(jurisdictions),
        filters=filters,
        use_cache=True,
    )
    trust = Trust(
        in_force_as_of=disclaimer_as_of,
        confidence=result.top_rerank_score,
        grounding=_grounding_for_search(result.top_rerank_score, result.flagged),
    )
    if result.refused or not result.candidates:
        return finalize(
            None,
            [],
            refused=True,
            refusal_reason=RefusalReason.NO_COVERAGE,
            as_of=disclaimer_as_of,
            jurisdiction=jurisdictions,
            trust=trust,
            meta=meta,
        )

    candidate_ids = [h.provision_id for h in result.candidates]
    deontic = await _deontic_for(candidate_ids)

    governing: list[dict[str, object]] = []
    obligations: list[dict[str, object]] = []
    citations: list[Citation] = []
    for hit in result.candidates:
        pid = hit.provision_id
        payload = result.payloads.get(pid) or {}
        text = (payload.get("text") or "").strip()
        ext = payload.get("external_id")
        label = payload.get("label")
        governing.append(
            {
                "provision_id": str(pid),
                "external_id": ext,
                "label": label,
                "snippet": text[:_SNIPPET_MAX],
            }
        )
        citations.append(
            Citation(
                provision_id=str(pid),
                external_id=ext,
                label=label,
                act_title=payload.get("title"),
                legal_hierarchy=payload.get("legal_hierarchy"),
                jurisdiction=payload.get("jurisdiction"),
            )
        )
        d = deontic.get(pid)
        if d:
            modal = (
                (d.get("modal") or "").strip().lower() if isinstance(d.get("modal"), str) else None
            )
            if modal in _MODAL_VOCAB:
                obligations.append(
                    {
                        "modal": modal,
                        "action": d.get("action"),
                        "provision_id": str(pid),
                        "label": d.get("label") or label,
                    }
                )

    data = {
        "governing_provisions": governing,
        "general_obligations": obligations,
        "lawyer_handoff": None,
    }
    return finalize(
        data,
        citations,
        as_of=disclaimer_as_of,
        jurisdiction=jurisdictions,
        trust=trust,
        meta=meta,
    )


# ---------------------------------------------------------------------------
# Not implemented yet
# ---------------------------------------------------------------------------
async def answer_legal_question(req: AnswerLegalQuestionInput) -> Envelope:
    """Return a grounded, cited information answer to a legal question. Not implemented yet.

    The heaviest tool. Compliance gates run before and after generation, the caller needs the
    `answer.generate` scope (top plan only), and `envelope.finalize` checks every supporting
    span against its cited provision (failing spans are dropped; if all fail, the answer is
    refused with no_coverage).
    """
    raise NotImplementedError(_WP)


__all__ = [
    "answer_legal_question",
    "check_compliance",
    "get_provision",
    "list_obligations",
    "search_czech_law",
    "whats_in_force",
]
