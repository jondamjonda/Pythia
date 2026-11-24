"""Multi-citation expansion: surface every provision a finding cites, not just the first.

A finding can cite more than one provision (``finding.cited_provisions = [p1, p2, p3]``), for
example an AIFMD duty grounded in a Directive article, its transposing Czech provision and an
implementing decree. By default ``render.build_digest`` surfaces only ``cited_provisions[0]``, so
the customer never sees that the same duty also rests on a section of the national act. This
module recovers those extra citations when ``digest_citation_strategy`` is set to something other
than ``"first_only"``.

* :data:`CitationStrategy`: ``"first_only"``, ``"all_direct"`` or ``"all_with_hierarchy"``.
* :func:`expand_citations`: turns a finding's ``cited_provisions`` and a ``provisions_by_id``
  lookup into a list of :class:`CitationRef`, per strategy.

The module exists to add citations and must never hide or understate a binding duty:

1. ``first_only`` reproduces render's single surfaced citation exactly (same id,
   ``is_primary=True``). ``all_direct`` is strictly additive: the same primary first, then the
   extras. ``is_primary`` is an ordering annotation, never a removal.
2. The only citation ever omitted is ungrounded case law, mirroring render's grounding guard: a
   fabricated apex-court ruling with no resolvable ``source_external_id`` must never become a
   clickable chip. Statute, EU, decree and administrative citations are always grounded.
3. A cited id missing from ``provisions_by_id`` is unresolvable (the caller did not supply the
   row) and is skipped, just as render skips a finding whose ``cited[0]`` is absent.

Deterministic, as the audit export requires: no LLM, database, network, filesystem, clock or
randomness. :func:`expand_citations` is idempotent in its id set and applies a stable total sort
(hierarchy depth, then stringified provision id), so repeated runs produce identical output.

The case-law grounding rule is replicated here rather than imported, to keep this module
self-contained. It must stay in step with ``render._is_case_law`` and
``render._case_law_grounded``: the same two signals (provenance ``source == 'court'`` or a
case-law token in the normalised ``legal_hierarchy``), the same normalisation, and the same "statute
and EU law are always grounded" rule. If the engine schema ever moves these markers, update both.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# The strategy axis:
# first_only          Exactly the first resolvable cited provision, as render.build_digest shows by
#                     default. Loses no surfaced citation by construction.
# all_direct          All resolvable cited provisions. Strictly additive over first_only and
#                     recovers every distinct resolvable provision.
# all_with_hierarchy  Like all_direct, also annotating each ref with its provision's hierarchy_path.
CitationStrategy = Literal["first_only", "all_direct", "all_with_hierarchy"]


@dataclass(frozen=True)
class CitationRef:
    """One resolved citation chip.

    Attributes:
        provision_id: The resolvable provision id, always a string. This is the chip key, using
            the same stringification as render.py so app UUIDs and nexus string ids unify.
        hierarchy_path: The LTREE breadcrumb for the provision, or None. Populated only by the
            ``all_with_hierarchy`` strategy; the other strategies make no hierarchy claim. Read
            from an already-loaded ``provision.hierarchy_path`` attribute (no lazy load).
        is_primary: True for the first resolvable cited provision (the one render surfaces by
            default), False for a recovered extra. An ordering annotation only; it never causes
            a citation to be hidden.
    """

    provision_id: str
    hierarchy_path: str | None
    is_primary: bool


# Grounding predicate, replicated from render.py and kept in step with it. Mirrors
# render._CASE_LAW_NORMALIZED_FAMILY and _PROVENANCE_COURT. A provision is case law if its
# provenance source is 'court' or its normalised legal_hierarchy contains a case-law token. A
# case-law citation is grounded only when it carries a resolvable (non-empty string)
# source_external_id; every other citation (statute, EU, decree, administrative) is always grounded
# and never dropped.
_CASE_LAW_NORMALIZED_FAMILY: frozenset[str] = frozenset({"case law", "caselaw"})
_PROVENANCE_COURT: str = "court"


def _normalize_hierarchy(legal_hierarchy: str | None) -> str:
    """Normalise a free-form ``legal_hierarchy`` token; never raises.

    Same as ``render._normalize_hierarchy``: case-fold, strip, turn every ``-`` and ``_`` into a
    space and squeeze whitespace. ``'CASE_LAW'``, ``'case-law'`` and ``' CASE_LAW '`` all become
    ``'case law'``, while ``'cz-act'`` becomes ``'cz act'`` (never a case-law match). None becomes
    ``''``.
    """
    if legal_hierarchy is None:
        return ""
    text = str(legal_hierarchy).strip().casefold()
    for sep in ("-", "_"):
        text = text.replace(sep, " ")
    return " ".join(text.split())


def _provision_source(provision: Any) -> str | None:
    """Return the provenance marker (for example ``'court'``), never triggering a lazy load.

    Same as ``render._provision_source``. The app ``Provision`` model has no ``source`` column,
    so on real rows ``getattr`` simply misses and returns None (no query can fire); a value comes
    back only when a caller pre-populates a plain ``.source`` attribute. Case-folded and stripped;
    a non-string or empty value gives None.
    """
    src = getattr(provision, "source", None)
    if not isinstance(src, str):
        return None
    src = src.strip().casefold()
    return src or None


def _source_external_id(provision: Any) -> str | None:
    """Return the act id (``SourceDocument.external_id``), never triggering a lazy load.

    Same as ``render._source_external_id``. Read only from an already-loaded
    ``provision.source_external_id`` plain attribute: the act id lives on the joined
    SourceDocument, and reaching it through a relationship would trigger a lazy database load.
    Only a real non-empty ``str`` counts; any other value (``0``, ``False``, ``[]``, ...) gives
    None so it can never be mistaken for present, which would wrongly ground a case-law chip.
    """
    ext = getattr(provision, "source_external_id", None)
    if not isinstance(ext, str):
        return None
    ext = ext.strip()
    return ext or None


def _is_case_law(provision: Any) -> bool:
    """Return True if the provision is case law by either signal (same rule as render).

    1. Provenance, robust to hierarchy-label drift: a pre-populated ``source == 'court'``.
    2. Hierarchy: the normalised ``legal_hierarchy`` contains a case-law token.

    Every statute, EU, decree or administrative value (including the ambiguous
    ``'cz-decision'``), None, ``''`` and a missing ``source`` match neither signal, so they are
    not case law and are never dropped.
    """
    if _provision_source(provision) == _PROVENANCE_COURT:
        return True
    norm = _normalize_hierarchy(getattr(provision, "legal_hierarchy", None))
    return any(token in norm for token in _CASE_LAW_NORMALIZED_FAMILY)


def _case_law_grounded(provision: Any) -> bool:
    """Return True if the citation is grounded (same rule as ``render._case_law_grounded``).

    * A citation that is not case law (statute, EU, decree, administrative) is always grounded.
    * A case-law citation is grounded only when it carries a non-empty resolvable
      ``source_external_id`` (a real case-law ``source_document.external_id``). Otherwise the chip
      is withheld, so a fabricated apex-court chip is never emitted.

    The app corpus currently contains no case law, so this never fires there; it makes a
    fabricated case chip impossible as soon as case law appears.
    """
    if not _is_case_law(provision):
        return True
    return _source_external_id(provision) is not None


def _hierarchy_depth(provision: Any) -> int:
    """Return the structural depth of a provision (0 when unknown).

    The LTREE ``hierarchy_path`` is a dot-separated path (for example ``act.part.chapter.§``);
    its depth is the number of labels. Depth is the primary sort key, so the highest-level seat
    of a duty (the act or chapter) sorts before its deeper children, giving a stable, legally
    sensible reading order. None, empty or non-string paths give 0. Depth is only a sort key: it
    is not surfaced and never affects which citations are returned.
    """
    path = getattr(provision, "hierarchy_path", None)
    if not isinstance(path, str):
        return 0
    path = path.strip()
    if not path:
        return 0
    return len([part for part in path.split(".") if part])


def _hierarchy_path(provision: Any) -> str | None:
    """Return the provision's LTREE ``hierarchy_path`` from the already-loaded attribute.

    Returns the non-empty stripped path, or None. Used to populate
    :class:`CitationRef.hierarchy_path` under the ``all_with_hierarchy`` strategy only.
    """
    path = getattr(provision, "hierarchy_path", None)
    if not isinstance(path, str):
        return None
    path = path.strip()
    return path or None


def expand_citations(
    cited_provisions: list,
    provisions_by_id: dict,
    strategy: CitationStrategy = "first_only",
) -> list[CitationRef]:
    """Expand a finding's cited provisions into resolved :class:`CitationRef` chips.

    Args:
        cited_provisions: The finding's ``cited_provisions`` ids (any hashable; stringified for
            lookup, as in ``render.build_digest``).
        provisions_by_id: Lookup from id to provision row. Keys are stringified on lookup so app
            UUIDs and nexus string ids resolve uniformly. The caller has already fetched the
            rows; this function does no I/O.
        strategy: See :data:`CitationStrategy`. Defaults to ``"first_only"``.

    Behaviour by strategy (all share the same resolution, grounding and sort rules):

    * ``first_only`` returns exactly the first cited provision as the single
      ``is_primary=True`` ref, matching ``render.build_digest``. If ``cited[0]`` itself is
      unresolvable or the finding is dropped by the grounding guard, it returns ``[]`` (render
      skips the finding) rather than falling through to a later citation.
    * ``all_direct`` returns every resolvable, grounded cited provision: the same primary,
      then the recovered extras.
    * ``all_with_hierarchy`` returns the same ids as ``all_direct``, each annotated with its
      provision's ``hierarchy_path``.

    Fewer refs than ids only ever reflects unresolvable ids or the case-law grounding guard,
    never a statutory duty and never the finding itself; the primary citation, when resolvable,
    is always present. ``is_primary`` is an ordering annotation, never a reason to hide a ref.

    The ``all_*`` strategies sort by (hierarchy depth, stringified id). The id tiebreak makes
    the order total and reproducible regardless of input order; the primary keeps its
    ``is_primary`` flag even if its position moves. Re-expanding the returned ids over the same
    lookup yields the same id set. A provision id cited twice yields one ref (the first
    occurrence wins); this collapses a duplicate chip, never a distinct citation.
    """
    # 0) Finding-level parity with render.build_digest, for every strategy: this function only
    # adds citations to a finding render keeps and never resurrects one render drops. Render's two
    # finding-level guards:
    #   (a) it skips the finding when cited[0] is not in the lookup;
    #   (b) it drops the whole finding when any cited provision present in the lookup is
    #       ungrounded case law.
    # An ungrounded chip at a later index therefore drops the finding: return [] rather than
    # promoting a surviving citation.
    cited = cited_provisions or []
    if not cited:
        return []
    if provisions_by_id.get(str(cited[0])) is None:
        return []  # (a) first citation unresolvable: render skips the finding
    for raw_id in cited:
        provision = provisions_by_id.get(str(raw_id))
        if provision is not None and not _case_law_grounded(provision):
            return []  # (b) an ungrounded case-law citation: render drops the finding

    # 1) The finding survives render's guards. Resolve its de-duplicated citations in input order.
    # After (b) every cited id present in the lookup is grounded; ids absent from the lookup are
    # unresolvable extras, which cannot be added (render shows only cited[0] anyway).
    resolved: list[tuple[str, Any]] = []  # (stringified id, provision row), in input order, deduped
    seen: set[str] = set()
    for raw_id in cited:
        sid = str(raw_id)
        if sid in seen:
            continue
        provision = provisions_by_id.get(sid)
        if provision is None:
            continue
        seen.add(sid)
        resolved.append((sid, provision))

    # 2) first_only: exactly the first citation, the one render surfaces (resolved[0] == cited[0]).
    if strategy == "first_only":
        sid, _provision = resolved[0]
        return [CitationRef(provision_id=sid, hierarchy_path=None, is_primary=True)]

    # 3) all_direct and all_with_hierarchy: every resolvable, grounded citation. The primary is
    # the first (resolved[0] == cited[0]); the rest are the extras render would otherwise omit.
    primary_id = resolved[0][0]
    want_hierarchy = strategy == "all_with_hierarchy"

    refs = [
        CitationRef(
            provision_id=sid,
            hierarchy_path=_hierarchy_path(provision) if want_hierarchy else None,
            is_primary=(sid == primary_id),
        )
        for sid, provision in resolved
    ]

    # 4) Stable total sort by (hierarchy depth, stringified id); the id tiebreak makes the order
    # reproducible. This only reorders; it never adds or removes a ref.
    depth_by_id = {sid: _hierarchy_depth(provision) for sid, provision in resolved}
    refs.sort(key=lambda r: (depth_by_id[r.provision_id], r.provision_id))
    return refs


# Evaluation scorers, in the style of applicability_eval: pure functions over an in-memory list of
# findings (each {"cited_provisions": [...]}) and a provisions_by_id lookup, so a harness can score
# them without I/O. signal_loss_rate is the key metric for this module: it must be 0 for every
# strategy (the default citation is never lost), and all_direct must recover 100%.


def _distinct_resolvable_grounded_ids(cited_provisions: list, provisions_by_id: dict) -> set[str]:
    """Return the recoverable ids for a finding: exactly the ids ``all_direct`` surfaces.

    Defining it through expand_citations keeps it consistent with render by construction: a
    finding render would drop (an ungrounded case-law citation, or an unresolvable first
    citation) has nothing to recover. So ``recovery_rate(all_direct) == 1.0`` holds even when
    such findings are present.
    """
    return {
        r.provision_id for r in expand_citations(cited_provisions, provisions_by_id, "all_direct")
    }


def signal_loss_rate(
    findings: list[dict], provisions_by_id: dict, strategy: CitationStrategy
) -> float:
    """Return the fraction of findings whose default primary citation is lost under ``strategy``.

    The default primary is the ref ``first_only`` returns. A finding counts as lost if
    ``first_only`` surfaces a citation but ``strategy`` does not include that same id. For
    ``first_only`` this is 0 by construction. The acceptance criterion is 0 for every strategy:
    expansion must never drop what the default view shows.

    Findings with no resolvable primary are excluded from the denominator (there is nothing to
    lose). Returns 0.0 when no finding counts.
    """
    lost = 0
    counted = 0
    for f in findings:
        cited = f.get("cited_provisions") or []
        primary = expand_citations(cited, provisions_by_id, "first_only")
        if not primary:
            continue
        counted += 1
        primary_id = primary[0].provision_id
        got_ids = {r.provision_id for r in expand_citations(cited, provisions_by_id, strategy)}
        if primary_id not in got_ids:
            lost += 1
    return round(lost / counted, 4) if counted else 0.0


def recovery_rate(
    findings: list[dict], provisions_by_id: dict, strategy: CitationStrategy
) -> float:
    """Return the fraction of distinct resolvable, grounded cited provisions ``strategy`` surfaces.

    Summed over all findings: ids surfaced by ``strategy`` divided by distinct recoverable cited
    ids. ``all_direct`` and ``all_with_hierarchy`` must reach 1.0; ``first_only`` falls below
    1.0 whenever a finding cites more than one distinct grounded provision (exactly the citations
    it omits). Returns 1.0 when there is nothing to recover.
    """
    universe = 0
    surfaced = 0
    for f in findings:
        cited = f.get("cited_provisions") or []
        recoverable = _distinct_resolvable_grounded_ids(cited, provisions_by_id)
        universe += len(recoverable)
        got_ids = {r.provision_id for r in expand_citations(cited, provisions_by_id, strategy)}
        # Count only ids inside the recoverable set, so nothing can inflate the rate.
        surfaced += len(got_ids & recoverable)
    return round(surfaced / universe, 4) if universe else 1.0


def score(findings: list[dict], provisions_by_id: dict) -> dict[str, Any]:
    """Run all three strategies over the findings and compute the metrics.

    Passes when ``signal_loss_rate`` is 0 for every strategy (the default primary is never
    lost) and ``all_direct`` recovery is 1.0 (every distinct resolvable provision surfaced).
    """
    metrics: dict[str, Any] = {"n_findings": len(findings)}
    for strat in ("first_only", "all_direct", "all_with_hierarchy"):
        metrics[strat] = {
            "signal_loss_rate": signal_loss_rate(findings, provisions_by_id, strat),  # must be 0
            "recovery_rate": recovery_rate(findings, provisions_by_id, strat),
        }
    metrics["ac_pass"] = (
        all(
            metrics[s]["signal_loss_rate"] == 0.0
            for s in ("first_only", "all_direct", "all_with_hierarchy")
        )
        and metrics["all_direct"]["recovery_rate"] == 1.0
    )
    return metrics
