"""Hierarchical context expansion for retrieved provisions.

When the retriever hits a leaf provision (subparagraph or sentence), the LLM needs the
surrounding article, the binding definitions and the immediate cross-references to make legal
sense of it. Each lookup is a small indexed query per hit, roughly 5 to 15 ms in total.

The output is intentionally light: each `ExpandedProvision` is a plain dataclass the analysis
layer can serialise into LLM context. The source_document row is deliberately left out (too
heavy for context); the caller joins it on demand if it needs publisher or jurisdiction.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text

from regsuper.storage.database import session_scope


@dataclass(frozen=True)
class ProvisionLite:
    """Minimal provision row, enough for LLM grounding."""

    id: UUID
    label: str | None
    text: str
    hierarchy_path: str
    legal_hierarchy: str
    jurisdiction: str
    language: str
    source_document_id: UUID
    parent_id: UUID | None


@dataclass(frozen=True)
class DefinitionLite:
    """One defined-term binding pulled in by expansion."""

    term: str
    definition_text: str
    definition_provision_id: UUID
    extracted_by: str
    confidence: float


@dataclass
class ExpandedProvision:
    """A hit with its ancestor chain, bound definitions and one-hop cross-references.

    `ancestors` is ordered child-to-root (so [0] is the immediate parent and
    [-1] is the document root); callers reverse this when rendering top-down
    context for an LLM.
    """

    hit: ProvisionLite
    ancestors: list[ProvisionLite] = field(default_factory=list)
    cross_references: list[ProvisionLite] = field(default_factory=list)
    definitions: list[DefinitionLite] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
_BASE_COLS = (
    "id, label, text, hierarchy_path::text AS hierarchy_path, "
    "legal_hierarchy, jurisdiction, language, source_document_id, parent_id"
)


def _to_lite(row: Any) -> ProvisionLite:
    return ProvisionLite(
        id=row[0],
        label=row[1],
        text=row[2],
        hierarchy_path=row[3],
        legal_hierarchy=row[4],
        jurisdiction=row[5],
        language=row[6],
        source_document_id=row[7],
        parent_id=row[8],
    )


async def _ancestors(provision_id: UUID) -> tuple[ProvisionLite | None, list[ProvisionLite]]:
    """Return (hit, ancestors ordered child to root) for `provision_id`.

    Walks the hit's `hierarchy_path` LTREE rather than `parent_id`: parent_id is NULL for about
    71% of provisions (the structural chunker set it only for a minority), so a recursive walk
    over parent_id would return an empty breadcrumb for most rows. The LTREE prefix is always
    populated, so we select every provision in the same source_document whose path is a strict
    ancestor of the hit's (`ancestor.hierarchy_path @> hit.hierarchy_path AND ancestor.id <>
    hit.id`), ordered by `nlevel` descending so the deepest ancestor, the immediate parent,
    comes first.
    """
    hit_cols = ", ".join("h." + c.split(" AS ")[0] for c in _BASE_COLS.split(", "))
    anc_cols = ", ".join("a." + c.split(" AS ")[0] for c in _BASE_COLS.split(", "))
    hit_sql = f"SELECT {hit_cols} FROM provision h WHERE h.id = :pid"
    anc_sql = f"""
        SELECT {anc_cols}
          FROM provision h
          JOIN provision a
            ON a.source_document_id = h.source_document_id
           AND a.hierarchy_path @> h.hierarchy_path
           AND a.id <> h.id
         WHERE h.id = :pid
         ORDER BY nlevel(a.hierarchy_path) DESC
    """
    async with session_scope() as session:
        hit_row = (await session.execute(text(hit_sql), {"pid": provision_id})).first()
        if hit_row is None:
            return None, []
        anc_rows = (await session.execute(text(anc_sql), {"pid": provision_id})).all()
    hit = _to_lite(hit_row)
    # Child to root: nlevel DESC puts the deepest ancestor (the immediate parent) first.
    ancestors = [_to_lite(r) for r in anc_rows]
    return hit, ancestors


async def _cross_refs(provision_id: UUID) -> list[ProvisionLite]:
    """Return the one-hop outgoing cross-references whose target is a known provision.

    Only rows with `to_provision IS NOT NULL` are kept; external references (e.g. citations to
    documents that are not loaded) are dropped.
    """
    sql = f"""
        SELECT {", ".join("p." + c.split(" AS ")[0] for c in _BASE_COLS.split(", "))}
          FROM cross_reference x
          JOIN provision p ON p.id = x.to_provision
         WHERE x.from_provision = :pid
           AND x.to_provision IS NOT NULL
    """
    async with session_scope() as session:
        rows = (await session.execute(text(sql), {"pid": provision_id})).all()
    return [_to_lite(r) for r in rows]


# Single-word terms shorter than this, or listed in the stopword guard below, are not bound on
# a bare token match: they would bind to almost every provision and add noise. Genuine
# single-word legal terms are comfortably above this length.
_MIN_SINGLE_WORD_TERM_LEN = 4

# Common CZ + EN function words that occasionally slip into `defined_term`
# (mis-extraction) and would otherwise bind to every provision mentioning them.
# Lower-cased; matched against lower(dt.term) for single-word terms only.
_DEFINITION_TERM_STOPWORDS = (
    # Czech
    "osoba",
    "den",
    "lhůta",
    "doba",
    "rok",
    "měsíc",
    "část",
    "věc",
    "stát",
    "zákon",
    "právo",
    "soud",
    "orgán",
    "úřad",
    "fond",
    "banka",
    # English
    "the",
    "and",
    "for",
    "person",
    "day",
    "year",
    "month",
    "law",
    "act",
    "court",
    "bank",
    "fund",
    "state",
    "right",
    "term",
    "date",
)


async def _bound_definitions(provision_id: UUID) -> list[DefinitionLite]:
    """Return defined_term rows whose term appears in this provision's text.

    Binding is phrase-aware, because plain token matching on `\\W+` fails both ways: a
    multi-word term ("investiční fond") never equals a single token and so is never bound,
    while short or common single-word terms bind to nearly every provision. Therefore:

      * multi-word terms (containing whitespace) match by whole-phrase, word-bounded
        containment against the provision text (`text ~* '\\m<phrase>\\M'`), so they bind
        exactly when the phrase appears verbatim;
      * single-word terms use the fast token-set membership match, but only when the term is
        at least `_MIN_SINGLE_WORD_TERM_LEN` characters long and not in
        `_DEFINITION_TERM_STOPWORDS`, which stops common words over-binding while keeping
        real single-word terms.

    Restricted by scope_document_id so definitions do not leak across acts: a term defined in
    Act A is only bound for provisions of Act A (unscoped definitions bind everywhere).
    """
    sql = """
        WITH hit_doc AS (
            SELECT id, source_document_id, text
              FROM provision
             WHERE id = :pid
        ),
        tokens AS (
            SELECT DISTINCT lower(t) AS tok
              FROM hit_doc, regexp_split_to_table(text, '\\W+') AS t
             WHERE length(t) >= :min_token_len
        )
        SELECT dt.term, dt.definition_text, dt.definition_provision_id,
               dt.extracted_by, dt.confidence
          FROM defined_term dt
          JOIN hit_doc h ON
               (dt.scope_document_id IS NULL OR
                dt.scope_document_id = h.source_document_id)
         WHERE
           -- multi-word terms: whole-phrase, word-bounded containment
           (
             dt.term ~ '\\s'
             AND h.text ~* ('\\m' || regexp_replace(dt.term,
                     '([.^$*+?()\\[\\]{}|\\\\])', '\\\\\\1', 'g') || '\\M')
           )
           OR
           -- single-word terms: token membership, length + stopword guarded
           (
             dt.term !~ '\\s'
             AND length(dt.term) >= :min_single_len
             AND lower(dt.term) <> ALL(:stopwords)
             AND lower(dt.term) IN (SELECT tok FROM tokens)
           )
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(sql),
                {
                    "pid": provision_id,
                    "min_token_len": _MIN_SINGLE_WORD_TERM_LEN,
                    "min_single_len": _MIN_SINGLE_WORD_TERM_LEN,
                    "stopwords": list(_DEFINITION_TERM_STOPWORDS),
                },
            )
        ).all()
    return [
        DefinitionLite(
            term=r[0],
            definition_text=r[1],
            definition_provision_id=r[2],
            extracted_by=r[3],
            confidence=float(r[4] or 0.0),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------
async def expand(provision_id: UUID) -> ExpandedProvision | None:
    """Return the full expansion for one hit, or None if the id does not exist.

    The None case is defensive; it should not happen when `provision_id` came from retrieval.
    """
    hit, ancestors = await _ancestors(provision_id)
    if hit is None:
        return None
    xrefs, defs = await asyncio.gather(
        _cross_refs(provision_id),
        _bound_definitions(provision_id),
    )
    return ExpandedProvision(
        hit=hit,
        ancestors=ancestors,
        cross_references=xrefs,
        definitions=defs,
    )


async def expand_many(
    provision_ids: list[UUID],
    *,
    concurrency: int = 5,
) -> list[ExpandedProvision]:
    """Expand many provisions in parallel.

    Bounded concurrency keeps the connection pool from running dry on large reranked lists.
    """
    if not provision_ids:
        return []
    sem = asyncio.Semaphore(concurrency)

    async def _one(pid: UUID) -> ExpandedProvision | None:
        async with sem:
            return await expand(pid)

    results = await asyncio.gather(*(_one(pid) for pid in provision_ids))
    return [r for r in results if r is not None]
