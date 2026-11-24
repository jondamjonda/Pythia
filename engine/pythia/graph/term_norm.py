"""Term-normalisation retrieval leg: map layman wording to statutory terms (deterministic, no LLM).

A vague layman query often names a real-world thing in words that share no lexical overlap with the
statutory wording the corpus uses. "cookies" never appears in the ePrivacy text ("koncové
zařízení"), "švarcsystém" is nowhere in the labour code ("závislá práce"), and "OSVČ" is an acronym
the statute spells out. The full-text seed then matches nothing for the one discriminative concept,
and semantic recall alone can miss the exact defining §.

When config.term_norm_enabled is on (default off), this module:
  1. Builds a lexicon once per process from the defined_term rows (the corpus's own defined
     vocabulary) plus a small, lawyer-reviewable alias file mapping layman wording to statutory
     terms (data/term_aliases.json).
  2. Matches query tokens and phrases against it (`match_terms`), exactly or by trigram similarity,
     returning the statutory terms to boost and the defining provision ids to seed.
  3. Lets seed.hybrid_seed add (a) an extra sparse leg over the statutory terms and (b) the defining
     provisions as extra seeds. It only adds candidates; it never drops, blocks or re-orders an
     existing hit.

seed.py guards every call site, so with the flag off nothing here runs and the lexicon is never
built.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from pythia.config import get_settings
from pythia.db import get_pool

# The alias file ships in the engine's `data/` directory (a sibling of pythia/). config.data_dir
# points at a different tree, so the path is resolved relative to this package instead.
_ALIAS_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "term_aliases.json"


# ------------------------------------------------------------------------- normalization primitives
def _fold(s: str) -> str:
    """Lowercase and strip diacritics (NFD, drop combining marks): 'koncové' becomes 'koncove'.

    Applied to every lexicon key and query token so matching ignores diacritics; laymen often type
    without them.
    """
    n = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in n if unicodedata.category(c) != "Mn")


def _tokens(s: str) -> list[str]:
    """Return the `\\w+` tokens of at least 3 characters in a folded string, in order.

    Mirrors the tokenizer in seed._seed_terms, so this leg uses the same tokens as the sparse seed
    it augments.
    """
    return [t for t in re.findall(r"\w+", _fold(s), re.UNICODE) if len(t) >= 3]


def _trigrams(s: str) -> set[str]:
    """Return the set of character trigrams of a padded, folded token.

    Used for fuzzy alias matching without pg_trgm. A token shorter than 3 characters yields the
    padded string as its only element.
    """
    f = f"  {_fold(s)} "
    return {f[i : i + 3] for i in range(len(f) - 2)} or {f}


def _trigram_sim(a: str, b: str) -> float:
    """Return the Jaccard similarity of two trigram profiles, in [0, 1].

    A pure-Python analogue of pg_trgm's similarity(), so matching needs no database. 1.0 means the
    trigram profiles are identical.
    """
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / len(ta | tb)


# --------------------------------------------------------------------------- lexicon model + cache
@dataclass
class LexEntry:
    """One lexicon row.

    `surface` is the folded match key, `statutory` the folded statutory terms to boost in the sparse
    leg, and `defining_ids` the defining provisions to seed (from defined_term; empty for alias rows
    with no matching defined term).
    """

    surface: str
    statutory: list[str]
    defining_ids: list[str] = field(default_factory=list)
    is_alias: bool = False


@dataclass
class TermMatch:
    """A match between a query span and a lexicon entry.

    `query_span` is the matched query text, `statutory` the terms to boost, `defining_ids` the
    provisions to seed, and `kind` either 'exact' or 'trigram' (kept for auditing).
    """

    query_span: str
    statutory: list[str]
    defining_ids: list[str]
    kind: str


# Process-global lexicon cache, built once from defined_term and the alias file.
_LEXICON: list[LexEntry] | None = None
_LEX_BY_SURFACE: dict[str, LexEntry] | None = None


# --------------------------------------------------------------------------- precompiled lexicon
@dataclass
class MaterializedLexicon:
    """Precompiled lexicon for fast matching, with the same results as `match_terms`.

    It precomputes the two things `match_terms` rebuilds on every call:

      - `by_surface`: the exact-lookup dict from folded surface to LexEntry.
      - `entries` and `trigrams`: the lexicon in its original order, with each entry's trigram
        profile computed once instead of once per query span.

    Built by `materialize()` and cached process-wide next to _LEXICON, so a query costs one dict
    lookup per span plus a scan over precomputed trigram sets. Never built while term_norm_enabled
    is off.
    """

    by_surface: dict[str, LexEntry]
    entries: list[LexEntry]  # same order as the source lexicon (the trigram-pass iteration order)
    trigrams: list[set[str]]  # trigrams[i] is _trigrams(entries[i].surface)


def materialize(lexicon: list[LexEntry]) -> MaterializedLexicon:
    """Compile a lexicon into a MaterializedLexicon (exact-lookup dict and per-entry trigrams).

    `entries` and `trigrams` keep the order of `lexicon`, so the trigram pass visits candidates in
    the same order as `match_terms` and breaks ties the same way.
    """
    return MaterializedLexicon(
        by_surface={e.surface: e for e in lexicon},
        entries=list(lexicon),
        trigrams=[_trigrams(e.surface) for e in lexicon],
    )


# Process-global cache for the compiled structure, built once from _LEXICON.
_MATERIALIZED: MaterializedLexicon | None = None


def _load_aliases() -> list[LexEntry]:
    """Parse data/term_aliases.json into LexEntry rows, one per alias surface form.

    A missing or malformed file yields [], so the leg falls back to the defined-term lexicon alone
    instead of failing.
    """
    try:
        raw = json.loads(_ALIAS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[LexEntry] = []
    for ent in raw.get("entries", []):
        statutory = [_fold(t) for t in ent.get("statutory", []) if t.strip()]
        if not statutory:
            continue
        for alias in ent.get("aliases", []):
            if alias.strip():
                out.append(LexEntry(surface=_fold(alias), statutory=statutory, is_alias=True))
    return out


async def _load_defined_terms() -> list[LexEntry]:
    """Load the corpus's own defined vocabulary: one LexEntry per defined_term.term.

    Each entry carries its defining provision ids. A term without a definition provision still
    contributes its surface form (as its own statutory term) so it can boost the sparse leg. Returns
    [] if the defined_term table does not exist.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT to_regclass('public.defined_term') IS NOT NULL"):
            return []
        rows = await conn.fetch(
            """SELECT term, array_agg(DISTINCT definition_provision_id::text)
                        FILTER (WHERE definition_provision_id IS NOT NULL) AS ids
                 FROM defined_term
                WHERE term IS NOT NULL AND length(term) >= 3
                GROUP BY term"""
        )
    out: list[LexEntry] = []
    for r in rows:
        surface = _fold(r["term"])
        if not surface:
            continue
        out.append(LexEntry(surface=surface, statutory=[surface], defining_ids=list(r["ids"] or []), is_alias=False))
    return out


async def build_lexicon(*, force: bool = False) -> list[LexEntry]:
    """Build the process-global lexicon on first use and return it.

    Merges alias rows and defined_term rows keyed by folded surface form. On a collision the alias
    wins, because its curated statutory mapping is richer, and inherits the defining ids of the
    defined term with the same surface. `force=True` rebuilds (for tests). Read-only and
    deterministic.
    """
    global _LEXICON, _LEX_BY_SURFACE
    if _LEXICON is not None and not force:
        return _LEXICON
    defined = await _load_defined_terms()
    aliases = _load_aliases()
    by_surface: dict[str, LexEntry] = {}
    for e in defined:
        by_surface[e.surface] = e
    for a in aliases:
        prior = by_surface.get(a.surface)
        if prior is not None:
            a.defining_ids = list(dict.fromkeys([*a.defining_ids, *prior.defining_ids]))
        by_surface[a.surface] = a  # the alias wins the surface key
    _LEXICON = list(by_surface.values())
    _LEX_BY_SURFACE = by_surface
    return _LEXICON


async def build_materialized(*, force: bool = False) -> MaterializedLexicon:
    """Build and cache the precompiled lexicon on first use and return it.

    Builds the lexicon first (cached separately), then compiles it. `force=True` rebuilds both (for
    tests). Read-only; its only side effect is the module cache.
    """
    global _MATERIALIZED
    if _MATERIALIZED is not None and not force:
        return _MATERIALIZED
    lex = await build_lexicon(force=force)
    _MATERIALIZED = materialize(lex)
    return _MATERIALIZED


def _reset_cache() -> None:
    """Test hook: clear the cached lexicon and compiled structure so the next build starts fresh."""
    global _LEXICON, _LEX_BY_SURFACE, _MATERIALIZED
    _LEXICON = None
    _LEX_BY_SURFACE = None
    _MATERIALIZED = None


# --------------------------------------------------------------------------- the matcher (pure)
def _query_spans(query: str) -> list[str]:
    """Return the folded query spans to match: every token plus every adjacent token pair.

    Pairs let a two-word alias such as 'osobni udaje' or 'zavisla prace' match. Order-preserving and
    de-duplicated.
    """
    toks = _tokens(query)
    spans: list[str] = list(toks)
    spans += [f"{toks[i]} {toks[i + 1]}" for i in range(len(toks) - 1)]
    seen: set[str] = set()
    out: list[str] = []
    for sp in spans:
        if sp not in seen:
            seen.add(sp)
            out.append(sp)
    return out


def match_terms(query: str, lexicon: list[LexEntry], *, trigram_threshold: float = 0.6) -> list[TermMatch]:
    """Match query spans (tokens and adjacent pairs) against the lexicon (pure, deterministic).

    For each span:

      - exact: the folded span equals a lexicon surface (highest confidence).
      - trigram: otherwise, if the span's best trigram similarity to any surface is at least
        `trigram_threshold`, it matches that single best surface (tolerates typos and inflection).

    A span that matches exactly is not also fuzzy-matched. Results are de-duplicated on the entry's
    (statutory, defining ids), so each lexicon entry fires once. Returns [] for an empty lexicon or
    no hit, in which case the caller adds nothing.
    """
    if not lexicon:
        return []
    by_surface = {e.surface: e for e in lexicon}
    spans = _query_spans(query)
    matches: list[TermMatch] = []
    seen: set[tuple] = set()

    def _emit(span: str, e: LexEntry, kind: str) -> None:
        key = (tuple(e.statutory), tuple(e.defining_ids))
        if key in seen:
            return
        seen.add(key)
        matches.append(
            TermMatch(query_span=span, statutory=list(e.statutory), defining_ids=list(e.defining_ids), kind=kind)
        )

    # Pass 1: exact surface hits over all spans first, so an exact pair ('platce dph') always beats
    # a single-token trigram near-miss ('platce') for the same entry. Longest spans go first, so a
    # multi-word exact hit is preferred and its entry cannot be claimed by a shorter span.
    for span in sorted(spans, key=lambda x: -len(x)):
        e = by_surface.get(span)
        if e is not None:
            _emit(span, e, "exact")
    # Pass 2: trigram hits for spans without an exact match (tolerates typos and inflection).
    for span in spans:
        if span in by_surface:
            continue  # already an exact hit
        best_e: LexEntry | None = None
        best_sim = trigram_threshold
        for e in lexicon:
            sim = _trigram_sim(span, e.surface)
            if sim >= best_sim:
                best_sim, best_e = sim, e
        if best_e is not None:
            _emit(span, best_e, "trigram")
    return matches


def match_terms_mat(query: str, mat: MaterializedLexicon, *, trigram_threshold: float = 0.6) -> list[TermMatch]:
    """Match a query against a precompiled lexicon; same results as `match_terms`, but faster.

    Uses the same span order, longest-first exact pass, tie-break and de-duplication as
    `match_terms`, but reads the precompiled `mat.by_surface` and `mat.trigrams` instead of
    rebuilding them per call. An equivalence test over the full alias set and edge cases pins the
    output to that of `match_terms` on the source lexicon. Returns [] for an empty lexicon or no
    hit.
    """
    if not mat.entries:
        return []
    by_surface = mat.by_surface
    spans = _query_spans(query)
    matches: list[TermMatch] = []
    seen: set[tuple] = set()

    def _emit(span: str, e: LexEntry, kind: str) -> None:
        key = (tuple(e.statutory), tuple(e.defining_ids))
        if key in seen:
            return
        seen.add(key)
        matches.append(
            TermMatch(query_span=span, statutory=list(e.statutory), defining_ids=list(e.defining_ids), kind=kind)
        )

    # Pass 1: exact surface hits, longest span first, as in match_terms.
    for span in sorted(spans, key=lambda x: -len(x)):
        e = by_surface.get(span)
        if e is not None:
            _emit(span, e, "exact")
    # Pass 2: trigram hits against the precomputed per-entry profiles. Each span's trigram set is
    # computed once and compared with mat.trigrams[i] using the same Jaccard similarity as
    # _trigram_sim. The iteration order matches the source lexicon, so the ">=" best-so-far
    # tie-break picks the same entry as match_terms.
    for span in spans:
        if span in by_surface:
            continue  # already an exact hit
        sp_tri = _trigrams(span)
        best_e: LexEntry | None = None
        best_sim = trigram_threshold
        for e, e_tri in zip(mat.entries, mat.trigrams):
            if not sp_tri or not e_tri:
                sim = 0.0
            else:
                inter = len(sp_tri & e_tri)
                sim = inter / len(sp_tri | e_tri)
            if sim >= best_sim:
                best_sim, best_e = sim, e
        if best_e is not None:
            _emit(span, best_e, "trigram")
    return matches


# --------------------------------------------------------------------------- public leg API (async)
async def term_norm_augmentation(query: str) -> tuple[list[str], list[str]]:
    """Return the extra search terms and seed provisions for a query; called by seed.hybrid_seed.

    Returns `(boost_terms, defining_seed_ids)`: the folded statutory terms to OR into an extra
    sparse leg, and the defining provisions to add as seeds (capped at config.term_norm_max_seeds).
    Both are empty when nothing matches or the lexicon is empty, so the caller adds nothing. Builds
    the lexicon on first use. Never raises: a database or file error yields ([], []). The leg only
    adds candidates, so its absence can lose a boost but never break retrieval.
    """
    s = get_settings()
    try:
        # The precompiled matcher gives the same result as match_terms(build_lexicon(), ...) without
        # rebuilding the lookup dict and every entry's trigrams on each query.
        mat = await build_materialized()
        matches = match_terms_mat(query, mat, trigram_threshold=s.term_norm_trigram_threshold)
    except Exception:  # additive leg: any failure means no augmentation
        return [], []
    boost: list[str] = []
    seeds: list[str] = []
    for m in matches:
        for t in m.statutory:
            if t not in boost:
                boost.append(t)
        for pid in m.defining_ids:
            if pid not in seeds:
                seeds.append(pid)
    return boost, seeds[: max(0, s.term_norm_max_seeds)]
