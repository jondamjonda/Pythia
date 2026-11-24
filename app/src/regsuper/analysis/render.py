"""Render findings into the plain-language digest for small and medium businesses.

Shapes stored findings into four outputs (obligation register, compliance checklist, gap report,
prioritised action list), assembled by ``build_digest`` and served by ``api/digest.py``.

These functions are pure: they take ``list[Finding]`` and ``list[Provision]`` already fetched by the
caller and shape them with no database access, no model calls and no non-deterministic inputs
(randomness or the clock). Determinism keeps the audit export reproducible. Every item carries
``legal_source``, ``legal_source_url`` and a disclaimer id, and the raw findings are left untouched.
Every field derives from finding and provision row fields through the module-level vocabulary
maps (``MODAL_TO_CZECH`` and others).

Citation framing:

* every item carries a :class:`CitedRef` (``cited_ref``), a resolvable structured citation
  (provision_id, label, corpus, hierarchy_path, legal_hierarchy, source_external_id), so a
  consumer resolves a chip by id and corpus instead of parsing the human ``legal_source`` label
  (kept unchanged alongside ``cited_provision_id``);
* the answer view is pinned to one corpus (:data:`_PINNED_CORPUS`);
* a case-law citation without a resolvable ``source_external_id`` drops its finding, so a
  fabricated apex-court chip can never be emitted. Case law is identified by either the engine's
  ``'CASE_LAW'`` hierarchy marker or ``source == 'court'`` provenance; statute and EU citations are
  never dropped. ``source_external_id`` and ``source`` are read only from already-loaded
  attributes, never lazy-loaded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal

# Disclaimer ids and registry: pure reference data with no database access at
# import, so importing them keeps this module pure.
from regsuper.analysis.disclaimers import (
    DISCLAIMER_INFO,
    DISCLAIMER_INFO_LOW_CONFIDENCE,
    DISCLAIMER_RESERVED_ADVICE,
    DisclaimerBlock,
    get_disclaimer,
)

if TYPE_CHECKING:  # type-only, so no ORM or runtime behaviour is imported at load
    from regsuper.storage.models import Finding, Provision

# Internal tracking tag, as in compliance_guard and model_resolver; not used at runtime.
_IMPL_WP: Final[str] = "B2b / B2b-09"

# ---------------------------------------------------------------------------
# Single-corpus render contract and structured citation framing.
#
# The answer view is pinned to one corpus: every chip this module emits resolves
# against ``regsuper``, the app's own provision and source-document corpus, which the
# ``/sources/{id}`` viewer reads. This constant records the pin and stamps it on each
# chip so a UI knows which corpus to resolve against; pointing the source viewer at
# the engine corpus would be a separate change.
#
# Naming: the engine's answer chip (``PythiaAnswer.citations_map``) has a field named
# ``corpus`` that actually holds the act id. Here ``corpus`` is the corpus name (below)
# and ``source_external_id`` is the act id. Unifying the engine's field name is left
# to a future schema alignment between the two.
_PINNED_CORPUS: Final[str] = "regsuper"

# Canonical case-law marker + the normalized family it expands to.
#
# The engine corpus marks case-law provisions with exactly
# ``legal_hierarchy == 'CASE_LAW'`` (upper case, underscore), so that is the canonical
# marker. The app corpus currently contains no case law and uses a different,
# lower-case hyphenated vocabulary (``'cz-act'``, ``'eu-regulation'``,
# ``'eu-directive'``, ``'cz-decree'``, ``'cnb-vyhlaska'``, ...; a free-form Text column
# with no database enum). None of those is case law, so none must ever be dropped.
#
# Because ``legal_hierarchy`` is free-form text, the guard does not exact-match one
# sentinel (variants like ``'case_law'``, ``' CASE_LAW '`` or ``'case-law'`` would slip
# past). It normalises the value (case-fold, strip, turn ``-`` and ``_`` into spaces)
# and checks whether it contains a case-law token. That matches ``'CASE_LAW'`` and any
# compound that still names case law (``'cz-case-law'``, or a stray
# ``'INTERPRETIVE_CASE_LAW'`` bindingness token), but never an ambiguous value like
# ``'cz-decision'`` (an administrative decision, not a court ruling).
#
# A provision is case law if either signal holds:
#   (1) its provenance ``source == 'court'``: the engine marks every ruling this way
#       (``provenance.source_of``). This is the authoritative marker, robust to
#       hierarchy-label drift, read from a pre-populated attribute
#       (:func:`_provision_source`);
#   (2) its ``legal_hierarchy`` names case law (token containment, above). The engine
#       writes ``'CASE_LAW'`` for every case-law provision and enforces it with a
#       corpus-depth check (``corpus_depth_standard.py`` fails on any other value inside
#       a case-law document).
# Either signal alone triggers the guard, so drift in one marker can never let a
# fabricated apex-court chip through. :data:`_CASE_LAW_NORMALIZED_FAMILY` and
# :data:`_PROVENANCE_COURT` define what counts as case law; re-check them if the engine
# ever changes either marker.
_CASE_LAW_HIERARCHY: Final[str] = "CASE_LAW"

# The normalised case-law tokens. A value is case law if its normalised form (see
# :func:`_normalize_hierarchy`) contains one of them; ``'case law'``, ``'case_law'``,
# ``'case-law'`` and ``' CASE_LAW '`` all normalise to ``'case law'``. Deliberately
# small and unambiguous: no ``'decision'``, ``'ruling'`` or ``'judgment'``, which could
# catch a statute or an administrative act.
_CASE_LAW_NORMALIZED_FAMILY: Final[frozenset[str]] = frozenset({"case law", "caselaw"})

# The authoritative case-law provenance value: in the engine schema a provision-level
# ``source == 'court'`` marks every court ruling (``provenance.source_of``). The app's
# ``Provision`` model has no ``source`` column, so this only fires when a caller
# pre-populates a plain ``.source`` attribute (no query can run). Compared case-folded
# (see :func:`_provision_source`).
_PROVENANCE_COURT: Final[str] = "court"

# Plain-language Czech label vocabularies, as closed enums.
ChecklistStatus = Literal["splněno", "nesplněno", "neověřeno", "nejasné"]
GapSeverityLabel = Literal["kritické", "důležité", "doporučené"]
ActionPriority = Literal["okamžitě", "do 30 dnů", "do 90 dnů", "průběžně"]


# ---------------------------------------------------------------------------
# Deterministic label vocabularies: module-level maps that, with the ORM row fields,
# are the only inputs to the render functions. Every lookup uses ``.get(...)`` with a
# safe default, so an unknown or ``None`` value never raises.
# ---------------------------------------------------------------------------

# Stored deontic modal to second-person plain Czech. Keys cover the modal tokens
# stored on ``provision.deontic_modal`` (written by tagging) and the L3 payload
# modals. Unknown or ``None`` gives ``_MODAL_DEFAULT``.
_MODAL_DEFAULT: Final[str] = "vztahuje se na vás"
MODAL_TO_CZECH: Final[dict[str, str]] = {
    # mandatory / duty
    "musí": "musíte",
    "musi": "musíte",
    "shall": "musíte",
    "must": "musíte",
    "obligation": "musíte",
    "mandatory": "musíte",
    "duty": "musíte",
    # prohibition
    "nesmí": "nesmíte",
    "nesmi": "nesmíte",
    "shall_not": "nesmíte",
    "must_not": "nesmíte",
    "prohibition": "nesmíte",
    "prohibited": "nesmíte",
    # permission / right
    "může": "můžete",
    "muze": "můžete",
    "may": "můžete",
    "can": "můžete",
    "permission": "můžete",
    "right": "můžete",
    # recommendation / soft duty
    "měl by": "měli byste",
    "mel by": "měli byste",
    "should": "měli byste",
    "recommended": "měli byste",
    "recommendation": "měli byste",
    "soft": "měli byste",
}

# Neutral actor label when ``provision.deontic_actor`` is missing.
_ACTOR_DEFAULT: Final[str] = "dotčená osoba"

# Modal to gap-severity label: how critical a missing obligation is depends on the
# kind of norm. Mandatory or prohibition is critical, a soft recommendation is
# advisory, and everything else (including unknown) is important.
_GAP_SEVERITY_CRITICAL: Final[GapSeverityLabel] = "kritické"
_GAP_SEVERITY_IMPORTANT: Final[GapSeverityLabel] = "důležité"
_GAP_SEVERITY_RECOMMENDED: Final[GapSeverityLabel] = "doporučené"

# Modal to action priority (how urgently to act on a gap), on the same axis.
_PRIORITY_IMMEDIATE: Final[ActionPriority] = "okamžitě"
_PRIORITY_30D: Final[ActionPriority] = "do 30 dnů"
_PRIORITY_ONGOING: Final[ActionPriority] = "průběžně"

# Canonical modals that count as a hard duty or prohibition, and as a soft duty.
_MODAL_HARD: Final[frozenset[str]] = frozenset({"musíte", "nesmíte"})
_MODAL_SOFT: Final[frozenset[str]] = frozenset({"měli byste"})

# Checklist status from a finding's review_state. The law alone does not tell us
# whether the business complies, so a verified but unreviewed obligation is
# ``neověřeno`` (not yet checked) and one with a suspect citation is ``nejasné`` (unclear).
_CHECKLIST_STATUS_BY_REVIEW: Final[dict[str, ChecklistStatus]] = {
    "unreviewed": "neověřeno",
    "needs_quote": "nejasné",
    "needs_review": "nejasné",
    "unverified_citation": "nejasné",
}
_CHECKLIST_STATUS_DEFAULT: Final[ChecklistStatus] = "neověřeno"

# Review states that count as already reviewed by a lawyer. Any other state sets
# ``Digest.has_unreviewed_citations``.
_REVIEWED_STATES: Final[frozenset[str]] = frozenset({"reviewed", "approved", "verified"})

# Finding routing (as in l3_obligations and l4_gaps): obligations are the L3
# ``finding_type='obligation'`` rows; gaps are the L4 ``finding_type='gap'`` rows.
_OBLIGATION_LAYER: Final[str] = "L3"
_OBLIGATION_TYPE: Final[str] = "obligation"
_GAP_LAYER: Final[str] = "L4"
_GAP_TYPE: Final[str] = "gap"

# Answer mode to top-level disclaimer id. Mirrors ``compliance_guard.disclaimer_for``
# without importing it, since that module loads database and LLM clients at import.
# Unknown or ``None`` gives the normal information block.
_DISCLAIMER_ID_BY_MODE: Final[dict[str, str]] = {
    "information": DISCLAIMER_INFO,
    "reserved_advice": DISCLAIMER_RESERVED_ADVICE,
    "needs_review": DISCLAIMER_INFO_LOW_CONFIDENCE,
}


@dataclass(frozen=True)
class CitedRef:
    """A resolvable structured citation, never a bare label.

    Carried on every rendered item so a consumer resolves a chip by id and corpus rather than
    by parsing a human label. All fields derive deterministically from already-loaded
    ``provision`` attributes, with no I/O.

    Attributes:
        provision_id: The provision id as a string (the resolvable key).
        label: The human label (``provision.label``, eid or path), for display only.
        corpus: The corpus name the chip resolves against (``_PINNED_CORPUS``).
        hierarchy_path: The LTREE breadcrumb (structural framing).
        legal_hierarchy: The norm tier, a free-form corpus value. The app vocabulary is
            lower-case hyphenated (``'cz-act'``, ``'eu-regulation'``, ``'eu-directive'``,
            ``'cz-decree'``, ``'cnb-vyhlaska'``, ...); the engine's case-law value is
            ``'CASE_LAW'``, the token the grounding guard keys on after normalisation.
        source_external_id: The act id (for example ``'240/2013'``), the SourceDocument
            ``external_id``. Nullable: it lives on the joined SourceDocument, so render never
            lazy-loads it and reads it only from an already-loaded
            ``provision.source_external_id`` attribute the caller may pre-populate. When it is
            None the UI falls back to ``provision_id``; on a case-law chip, None triggers the
            grounding guard in ``build_digest``.
    """

    provision_id: str
    label: str
    corpus: str
    hierarchy_path: str | None
    legal_hierarchy: str | None
    source_external_id: str | None


@dataclass(frozen=True)
class ObligationItem:
    """One row of the plain-language obligation register."""

    modal_label: str  # MODAL_TO_CZECH[deontic_modal]: 'musíte', 'nesmíte', ...
    actor_label: str
    deadline: str | None
    legal_source: str  # '§ … zákona č. …/… Sb.' (CZ) or '… čl. …' (EU)
    legal_source_url: str  # '/sources/{provision_id}'
    cited_provision_id: str
    confidence: float
    review_state: str
    disclaimer_id: str  # stable id into the disclaimers registry
    cited_ref: CitedRef | None = None  # structured citation framing
    # Entity-role applicability verdict. Every item is 'yes' unless entity_scoping_enabled is on
    # and the finding carries payload['applicability'].
    binds: str = "yes"  # 'yes' | 'conditional' | 'out_of_scope'
    binds_reason: str = ""  # plain-language reason for the verdict (empty when binds == 'yes')
    # Applicability confidence: how legally solid the binds verdict is. Empty unless
    # applicability_confidence_enabled is on.
    confidence_label: str = ""  # 'high' | 'moderate' | 'low' | '' (absent)
    confidence_reason: str = ""
    # The full grounded citation set, empty unless digest_citation_strategy is 'all_direct' or
    # 'all_with_hierarchy'. Each entry: {provision_id, hierarchy_path, is_primary}. The primary
    # (cited[0]) is always included with is_primary=True; the rest are added extras.
    all_cited_refs: tuple = ()
    # Multi-limb and duplicate annotations, empty unless multi_limb_enabled is on. Keys: limbs
    # (tuple of split sub-duties), is_duplicate (collapse hint), dedup_group (int) and
    # n_alternatives (mutually exclusive siblings). Annotation only; nothing is dropped.
    wf4: dict = field(default_factory=dict)
    # Temporal-freshness verdict for the cited provision, empty unless temporal_freshness_enabled
    # is on. Keys: verdict (current, superseded, future_effective, annulled or unknown), reason,
    # stale_since, ... Annotation only.
    temporal: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ChecklistItem:
    """One compliance-checklist row (a status per obligation)."""

    obligation: str
    status: ChecklistStatus
    legal_source: str
    legal_source_url: str
    cited_provision_id: str
    disclaimer_id: str
    cited_ref: CitedRef | None = None  # structured citation framing


@dataclass(frozen=True)
class GapItem:
    """One gap-report row (the report lists critical gaps first)."""

    statement: str
    severity_label: GapSeverityLabel
    legal_source: str
    legal_source_url: str
    cited_provision_id: str
    disclaimer_id: str
    cited_ref: CitedRef | None = None  # structured citation framing
    # Why this gap was demoted to context_gaps (it is never dropped). Empty for a normal gap.
    suppressed_by: tuple = ()


@dataclass(frozen=True)
class ActionItem:
    """One prioritised action-list row."""

    action: str
    priority: ActionPriority
    legal_source: str
    legal_source_url: str
    cited_provision_id: str
    disclaimer_id: str
    cited_ref: CitedRef | None = None  # structured citation framing


@dataclass(frozen=True)
class Digest:
    """The assembled digest: the four lists and the top-level disclaimer.

    ``disclaimer`` is the block shown with the whole digest; ``has_unreviewed_citations``
    mirrors the digest response contract.
    """

    obligations: list[ObligationItem] = field(default_factory=list)
    checklist: list[ChecklistItem] = field(default_factory=list)
    gaps: list[GapItem] = field(default_factory=list)
    actions: list[ActionItem] = field(default_factory=list)
    disclaimer: DisclaimerBlock | None = None
    has_unreviewed_citations: bool = False
    # Obligations the applicability gate judges do not bind this entity in this role
    # (out_of_scope). Shown here for transparency, never dropped. Empty unless
    # entity_scoping_enabled is on; 'yes' and 'conditional' stay in ``obligations`` with a
    # ``binds`` flag.
    does_not_bind: list[ObligationItem] = field(default_factory=list)
    # Gaps demoted to a visible lower-priority context section (the underlying obligation is
    # out_of_scope, a duplicate, or based on superseded law). Never dropped: every gap is in
    # either ``gaps`` or ``context_gaps``. Empty unless a gap-demotion flag is on.
    context_gaps: list[GapItem] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure field-derivation helpers. Each maps ORM row fields to a display string with
# a deterministic rule that never raises. No I/O, no randomness.
# ---------------------------------------------------------------------------
def _finding_payload(finding: Finding | None) -> dict:
    """Return the already-loaded ``finding.payload`` dict; never raises.

    L3 writes the per-duty deontic data (``modal``, ``actor``, ``deadline``) onto
    ``finding.payload``; the ``provision.deontic_*`` columns are always NULL in the live
    corpus. Reads only the already-loaded attribute (no I/O, as in
    :func:`_source_external_id`). A missing or non-dict payload, or ``finding is None``,
    gives ``{}``, so every payload-aware helper falls back to the provision column.
    """
    payload = getattr(finding, "payload", None) if finding is not None else None
    return payload if isinstance(payload, dict) else {}


def _modal_label(provision: Provision, finding: Finding | None = None) -> str:
    """Map the obligation's deontic modal to second-person plain Czech.

    Prefers the duty's own modal from L3, ``finding.payload['modal']`` (``shall``, ``may``,
    ``must_not`` or ``should``), mapped through :data:`MODAL_TO_CZECH`, and falls back to the
    ``provision.deontic_modal`` column (always NULL in the live corpus). Unknown or ``None``
    from both sources gives :data:`_MODAL_DEFAULT`.
    """
    modal = _finding_payload(finding).get("modal")
    if modal is None:
        modal = getattr(provision, "deontic_modal", None)
    if modal is None:
        return _MODAL_DEFAULT
    return MODAL_TO_CZECH.get(modal, _MODAL_DEFAULT)


def _actor_label(provision: Provision, finding: Finding | None = None) -> str:
    """Return the obligation's deontic actor or the neutral default (never empty).

    Prefers the duty's own actor from L3, ``finding.payload['actor']``, and falls back to the
    ``provision.deontic_actor`` column (always NULL in the live corpus).
    """
    actor = _finding_payload(finding).get("actor")
    if not actor:
        actor = getattr(provision, "deontic_actor", None)
    if actor:
        return actor
    return _ACTOR_DEFAULT


def _deadline(provision: Provision, finding: Finding | None = None) -> str | None:
    """Return the obligation's deadline text, or None; never fabricates a date.

    Prefers the duty's extracted deadline from L3, ``finding.payload['deadline']`` (``''`` when
    there is none), and falls back to the verbatim ``provision.deontic_condition`` text (always
    NULL in the live corpus). The result is the condition text (for example 'do 15 dnů od ...'),
    or None when neither source has a non-blank value.
    """
    deadline = _finding_payload(finding).get("deadline")
    if not (isinstance(deadline, str) and deadline.strip()):
        deadline = getattr(provision, "deontic_condition", None)
    if not isinstance(deadline, str):
        return None
    stripped = deadline.strip()
    return stripped or None


def _legal_source(provision: Provision) -> str:
    """Build a deterministic citation string from provision fields.

    Format: ``"<label> (<jurisdiction>/<legal_hierarchy>)"``. The head prefers the human label,
    then the structural eid, then the LTREE ``hierarchy_path``, then the bare id. The suffix is
    appended only when jurisdiction or legal_hierarchy is present.
    """
    label = getattr(provision, "label", None)
    eid = getattr(provision, "eid", None)
    hierarchy_path = getattr(provision, "hierarchy_path", None)

    head = label or eid or hierarchy_path
    if not head:
        head = str(getattr(provision, "id", ""))

    jurisdiction = getattr(provision, "jurisdiction", None)
    legal_hierarchy = getattr(provision, "legal_hierarchy", None)
    qualifier = "/".join(part for part in (jurisdiction, legal_hierarchy) if part)
    if qualifier:
        return f"{head} ({qualifier})"
    return head


def _legal_source_url(provision: Provision) -> str:
    """Return the stable in-app source URL, ``/sources/{provision_id}``."""
    return f"/sources/{getattr(provision, 'id', '')}"


def _cited_provision_id(provision: Provision) -> str:
    """Return the provision id as a string (the digest contract uses string ids)."""
    return str(getattr(provision, "id", ""))


def _source_external_id(provision: Provision) -> str | None:
    """Return the act id (``SourceDocument.external_id``), never triggering a lazy load.

    The act ``external_id`` lives on the joined ``SourceDocument``, not on ``Provision``.
    Reaching it through a relationship would trigger a lazy database load and break this
    module's purity (and the reproducible audit export). So it is read only from an
    already-loaded ``provision.source_external_id`` attribute, which the caller may
    pre-populate (for example from a joined select). When absent or empty this returns None,
    and the source viewer falls back to ``provision_id``. (``Provision`` has no
    ``source_document`` relationship anyway; this helper reads one plain attribute.)

    Only a real ``str`` counts. Any other value (``0``, ``False``, ``[]``, ``{}``, ...) is not
    a resolvable act id and gives None; treating it as present would let an ungrounded
    case-law citation through the guard.
    """
    ext = getattr(provision, "source_external_id", None)
    if not isinstance(ext, str):
        return None
    ext = ext.strip()
    return ext or None


def _cited_ref(provision: Provision) -> CitedRef:
    """Build the resolvable structured citation for a provision.

    Every field derives from already-loaded ``provision`` attributes by a rule that never
    raises. ``corpus`` is the single-corpus pin (:data:`_PINNED_CORPUS`).
    ``source_external_id`` may be None (see :func:`_source_external_id`). ``label`` uses the
    same head as :func:`_legal_source`, so the display text and the structured citation agree.
    """
    label = getattr(provision, "label", None)
    eid = getattr(provision, "eid", None)
    hierarchy_path = getattr(provision, "hierarchy_path", None)
    head = label or eid or hierarchy_path or str(getattr(provision, "id", ""))
    return CitedRef(
        provision_id=_cited_provision_id(provision),
        label=head,
        corpus=_PINNED_CORPUS,
        hierarchy_path=hierarchy_path,
        legal_hierarchy=getattr(provision, "legal_hierarchy", None),
        source_external_id=_source_external_id(provision),
    )


def _normalize_hierarchy(legal_hierarchy: str | None) -> str:
    """Normalise a free-form ``legal_hierarchy`` token; never raises.

    ``legal_hierarchy`` is a free-form Text column with two disjoint vocabularies: the engine's
    case-law marker ``'CASE_LAW'`` (upper case, underscore) and the app's lower-case hyphenated
    norm tiers (``'cz-act'``, ``'eu-regulation'``, ...). Normalising makes the case-law match
    robust to case and to hyphen or underscore spelling, so variants such as ``'case_law'``,
    ``'case-law'`` or ``' CASE_LAW '`` cannot slip past: case-fold, strip, turn every ``-`` and
    ``_`` into a space and squeeze whitespace. None becomes ``''``.

    Examples: ``'CASE_LAW'`` and ``'case-law'`` become ``'case law'``; ``'cz-act'`` becomes
    ``'cz act'``; ``'eu-regulation'`` becomes ``'eu regulation'``.
    """
    if legal_hierarchy is None:
        return ""
    text = str(legal_hierarchy).strip().casefold()
    # Treat '-' and '_' as equivalent word separators, then squeeze whitespace.
    for sep in ("-", "_"):
        text = text.replace(sep, " ")
    return " ".join(text.split())


def _provision_source(provision: Provision) -> str | None:
    """Return the provenance marker (for example ``'court'``), never triggering a lazy load.

    The engine marks every case-law ruling with provision-level provenance
    ``source == 'court'``. The app's ``Provision`` model has no ``source`` column or
    relationship, so on a real row ``getattr`` simply misses and returns None (no query can
    fire, as with :func:`_source_external_id`). A value is returned only when a caller
    pre-populates a plain ``.source`` attribute. Case-folded and stripped, so ``'Court'`` and
    ``' COURT '`` match; a non-string or empty value gives None.
    """
    src = getattr(provision, "source", None)
    if not isinstance(src, str):
        return None
    src = src.strip().casefold()
    return src or None


def _is_case_law(provision: Provision) -> bool:
    """Return True if the provision is case law by either of two signals.

    1. Provenance (authoritative, robust to hierarchy-label drift): a pre-populated
       ``source == 'court'``, which the engine sets on every ruling
       (:func:`_provision_source`).
    2. Hierarchy: the normalised ``legal_hierarchy`` contains a case-law token
       (:data:`_CASE_LAW_NORMALIZED_FAMILY`), so the engine's ``'CASE_LAW'`` and any compound
       that still names case law (``'cz-case-law'``, a stray ``'INTERPRETIVE_CASE_LAW'``)
       match.

    Every app statute, EU, decree or administrative value (``'cz-act'``, ``'eu-regulation'``,
    ``'cz-decree'``, the ambiguous ``'cz-decision'``, ...), None, ``''`` and a missing
    ``source`` match neither signal, so they are not case law and never dropped.
    ``'cz-decision'`` is deliberately excluded: it is an administrative decision, not a court
    ruling.
    """
    if _provision_source(provision) == _PROVENANCE_COURT:
        return True
    norm = _normalize_hierarchy(getattr(provision, "legal_hierarchy", None))
    return any(token in norm for token in _CASE_LAW_NORMALIZED_FAMILY)


def _case_law_grounded(provision: Provision) -> bool:
    """Return True if the citation is grounded: the case-law guard, extending cite-or-refuse.

    * A citation that is not case law (statute, EU, decree, administrative; see
      :func:`_is_case_law`) is always grounded, in either vocabulary. This guard must never
      drop such a chip.
    * A case-law citation is grounded only when it carries a non-empty, resolvable
      ``source_external_id`` (a real case-law ``source_document.external_id``). Otherwise
      ``build_digest`` drops it, so a fabricated apex-court chip is never emitted.

    Case law is recognised by either marker (see :func:`_is_case_law`): engine provenance
    ``source == 'court'`` or the ``'CASE_LAW'`` hierarchy token (:data:`_CASE_LAW_HIERARCHY`),
    each normalised and matched by containment. Both markers are defined by the engine's
    case-law schema; if it changes either, update :data:`_CASE_LAW_NORMALIZED_FAMILY` and
    :data:`_PROVENANCE_COURT` to match.

    The app corpus currently has no case law, so this is purely defensive; it makes a
    fabricated case chip impossible as soon as case law appears.
    """
    if not _is_case_law(provision):
        return True
    return _source_external_id(provision) is not None


def _disclaimer_id(finding: Finding) -> str:
    """Return ``finding.disclaimer_id`` or the normal-info default (never empty)."""
    disclaimer_id = getattr(finding, "disclaimer_id", None)
    if disclaimer_id:
        return disclaimer_id
    return DISCLAIMER_INFO


def _gap_severity_label(provision: Provision, finding: Finding | None = None) -> GapSeverityLabel:
    """Map the obligation's modal to a gap-severity label (used to list critical gaps first).

    Uses the duty's own L3 modal, ``finding.payload['modal']`` (via :func:`_modal_label`), not
    the cited provision's article-level modal: a missing duty is as critical as that duty. The
    provision column remains the fallback.
    """
    modal = _modal_label(provision, finding)
    if modal in _MODAL_HARD:
        return _GAP_SEVERITY_CRITICAL
    if modal in _MODAL_SOFT:
        return _GAP_SEVERITY_RECOMMENDED
    return _GAP_SEVERITY_IMPORTANT


def _action_priority(provision: Provision, finding: Finding | None = None) -> ActionPriority:
    """Map the obligation's modal to a remediation priority.

    Uses the duty's own L3 modal, ``finding.payload['modal']`` (via :func:`_modal_label`), not
    the cited provision's article-level modal. The provision column remains the fallback.
    """
    modal = _modal_label(provision, finding)
    if modal in _MODAL_HARD:
        return _PRIORITY_IMMEDIATE
    if modal in _MODAL_SOFT:
        return _PRIORITY_ONGOING
    return _PRIORITY_30D


def _checklist_status(finding: Finding) -> ChecklistStatus:
    """Map ``finding.review_state`` to a checklist status (default ``neověřeno``)."""
    review_state = getattr(finding, "review_state", None) or ""
    return _CHECKLIST_STATUS_BY_REVIEW.get(review_state, _CHECKLIST_STATUS_DEFAULT)


def _is_unreviewed(review_state: str) -> bool:
    """Return True unless ``review_state`` is an explicitly lawyer-reviewed terminal state."""
    return review_state not in _REVIEWED_STATES


def _digest_disclaimer(answer_mode: str) -> DisclaimerBlock:
    """Resolve the top-level :class:`DisclaimerBlock` for an answer mode.

    Maps the mode to its canonical disclaimer id (as ``compliance_guard.disclaimer_for`` does
    at ``confidence=1.0``; an unknown or None mode gives the normal information block) and
    returns the registry block. A plain dictionary lookup, no database.
    """
    disclaimer_id = _DISCLAIMER_ID_BY_MODE.get(answer_mode, DISCLAIMER_INFO)
    return get_disclaimer(disclaimer_id)


def render_obligation_item(
    finding: Finding,
    provision: Provision,
    all_cited_refs: tuple = (),
    wf4: dict | None = None,
    temporal: dict | None = None,
) -> ObligationItem:
    """Shape one L3 obligation finding into an :class:`ObligationItem`.

    Deterministic: every field derives from ``finding`` and ``provision`` row fields through
    the module-level maps (no I/O, randomness or clock).
    """
    # Surface the applicability verdict if the scoring pass stamped one; otherwise the 'yes'
    # defaults apply. Routing and demotion happen in build_digest. A malformed or partial
    # verdict (not a dict, or binds outside the closed set) falls back to 'yes': it must never
    # crash render or the downstream Pydantic models, and the fallback can only keep a finding.
    appl = (getattr(finding, "payload", None) or {}).get("applicability")
    appl = appl if isinstance(appl, dict) else {}
    binds = appl.get("binds", "yes")
    if binds not in ("yes", "conditional", "out_of_scope"):
        binds = "yes"
    binds_reason = str(appl.get("reason", "")) if binds != "yes" else ""
    # Surface the applicability-confidence annotation if present ('' otherwise).
    confidence_label = str(appl.get("confidence_label", "") or "")
    confidence_reason = str(appl.get("confidence_reason", "") or "")
    return ObligationItem(
        modal_label=_modal_label(provision, finding),
        actor_label=_actor_label(provision, finding),
        deadline=_deadline(provision, finding),
        legal_source=_legal_source(provision),
        legal_source_url=_legal_source_url(provision),
        cited_provision_id=_cited_provision_id(provision),
        confidence=getattr(finding, "confidence_overall", 0.0),
        review_state=getattr(finding, "review_state", None) or "",
        disclaimer_id=_disclaimer_id(finding),
        cited_ref=_cited_ref(provision),
        binds=binds,
        binds_reason=binds_reason,
        confidence_label=confidence_label,
        confidence_reason=confidence_reason,
        all_cited_refs=tuple(all_cited_refs),
        wf4=dict(wf4) if wf4 else {},
        temporal=dict(temporal) if temporal else {},
    )


def render_checklist_item(finding: Finding, provision: Provision) -> ChecklistItem:
    """Shape one finding into a :class:`ChecklistItem`.

    The checklist ``obligation`` text is the finding statement. ``status`` derives from the
    finding's ``review_state``: the law alone cannot establish whether the business complies,
    so it is ``neověřeno`` (not yet checked) or ``nejasné`` (unclear).
    """
    return ChecklistItem(
        obligation=getattr(finding, "statement", None) or "",
        status=_checklist_status(finding),
        legal_source=_legal_source(provision),
        legal_source_url=_legal_source_url(provision),
        cited_provision_id=_cited_provision_id(provision),
        disclaimer_id=_disclaimer_id(finding),
        cited_ref=_cited_ref(provision),
    )


def render_gap_item(
    finding: Finding, provision: Provision, extra_suppressed: tuple = ()
) -> GapItem:
    """Shape one L4 gap finding into a :class:`GapItem`.

    ``statement`` is the finding's gap statement; ``severity_label`` derives from the underlying
    obligation's modal via :func:`_gap_severity_label`. ``suppressed_by`` combines the reasons
    L4 stamped (``payload['suppressed_by']``: applicability, duplicate) with any
    ``extra_suppressed`` computed at render time (temporal 'stale_law'), de-duplicated in order.
    A non-empty value moves the gap to context_gaps, where it is still shown; it never drops it.
    """
    payload_suppressed = (getattr(finding, "payload", None) or {}).get("suppressed_by")
    # A malformed value (a bare string, an int, None) must never crash render or be unpacked
    # character by character. Only a real list or tuple counts; otherwise no L4 reason applies
    # (the render-time extras still do).
    if not isinstance(payload_suppressed, (list, tuple)):
        payload_suppressed = ()
    suppressed = tuple(dict.fromkeys([*payload_suppressed, *extra_suppressed]))
    return GapItem(
        statement=getattr(finding, "statement", None) or "",
        severity_label=_gap_severity_label(provision, finding),
        legal_source=_legal_source(provision),
        legal_source_url=_legal_source_url(provision),
        cited_provision_id=_cited_provision_id(provision),
        disclaimer_id=_disclaimer_id(finding),
        cited_ref=_cited_ref(provision),
        suppressed_by=suppressed,
    )


def render_action_item(finding: Finding, provision: Provision) -> ActionItem:
    """Shape one finding into a prioritised :class:`ActionItem`.

    The ``action`` text is the finding statement; ``priority`` derives from the obligation's
    modal via :func:`_action_priority`.
    """
    return ActionItem(
        action=getattr(finding, "statement", None) or "",
        priority=_action_priority(provision, finding),
        legal_source=_legal_source(provision),
        legal_source_url=_legal_source_url(provision),
        cited_provision_id=_cited_provision_id(provision),
        disclaimer_id=_disclaimer_id(finding),
        cited_ref=_cited_ref(provision),
    )


def build_digest(
    findings: list[Finding],
    provisions: list[Provision],
    *,
    answer_mode: str = "information",
    citation_strategy: str = "first_only",
    multi_limb: bool = False,
    temporal_as_of: Any = None,
    temporal_gap_suppression: bool = False,
    extra: dict[str, Any] | None = None,
) -> Digest:
    """Assemble the full :class:`Digest` from raw findings and provisions.

    Routing follows ``l3_obligations`` and ``l4_gaps``: an L3 ``finding_type='obligation'``
    finding yields one obligation, checklist and action row; an L4 ``finding_type='gap'``
    finding yields one gap row. Each finding is paired with its first cited provision
    (``cited_provisions[0]``) looked up in ``provisions`` by id; a finding whose provision is
    missing from the list is skipped. The lists preserve the input order.
    ``has_unreviewed_citations`` is true when any rendered item came from a finding no lawyer has
    reviewed yet. The top-level ``disclaimer`` is the registry block for ``answer_mode``. The
    findings are read-only; none is mutated.

    This is the one place the case-law grounding guard is applied: a finding is dropped whole
    if any of its cited provisions (not only the first) is ungrounded case law, so an ungrounded
    apex-court chip at a later index can never ride along on a grounded statute at index 0. The
    public ``render_*_item`` builders do not apply the guard; callers that bypass
    ``build_digest`` are responsible for grounding.

    The optional flags (``citation_strategy``, ``multi_limb``, ``temporal_as_of``,
    ``temporal_gap_suppression``) only add annotations or demote items to visible secondary
    sections; with their defaults the digest is the plain four lists. ``temporal_as_of`` is
    passed in by the caller so this function never reads the clock.

    Deterministic: no persistence, no model calls, no randomness or clock.
    """
    # Keyed by stringified id, so an app Provision (UUID id) and an engine provision view (string
    # id) are looked up the same way; cited_provisions elements are stringified to match. Without
    # this a nexus citation would fail to resolve and its finding would silently disappear.
    by_id: dict[str, Provision] = {str(getattr(p, "id", None)): p for p in provisions}

    obligations: list[ObligationItem] = []
    checklist: list[ChecklistItem] = []
    gaps: list[GapItem] = []
    actions: list[ActionItem] = []
    does_not_bind: list[ObligationItem] = []
    context_gaps: list[GapItem] = []
    has_unreviewed = False

    # Optional pre-pass (multi_limb): compute the multi-limb, duplicate and mutual-exclusion
    # annotations over the obligation findings, keyed by object identity so they line up however
    # many findings the loop later keeps. Annotation only; nothing here drops a finding.
    wf4_by_obj: dict[int, dict] = {}
    if multi_limb:
        from regsuper.analysis.multi_limb import dedup_groups, mutual_exclusion_group, split_limbs

        _obl = [
            f
            for f in findings
            if getattr(f, "finding_type", None) == _OBLIGATION_TYPE
            or getattr(f, "analysis_layer", None) == _OBLIGATION_LAYER
        ]
        _dicts = [
            {
                "actor": (getattr(f, "payload", None) or {}).get("actor"),
                "statement": getattr(f, "statement", None),
                "modal": (getattr(f, "payload", None) or {}).get("modal"),
                "law_source": (getattr(f, "payload", None) or {}).get("law_source"),
                "management_structure": (getattr(f, "payload", None) or {}).get(
                    "management_structure"
                ),
            }
            for f in _obl
        ]
        _dedup = dedup_groups(_dicts)
        _alts: dict[int, int] = {}
        for _grp in mutual_exclusion_group(_dicts):
            for _i in _grp:
                _alts[_i] = len(_grp) - 1
        for _i, _f in enumerate(_obl):
            wf4_by_obj[id(_f)] = {
                "limbs": tuple(split_limbs(getattr(_f, "statement", None))),
                "is_duplicate": _dedup[_i]["is_duplicate"],
                "dedup_group": _dedup[_i]["dedup_group"],
                "n_alternatives": _alts.get(_i, 0),
            }

    for finding in findings:
        cited = getattr(finding, "cited_provisions", None) or []
        if not cited:
            continue
        provision = by_id.get(str(cited[0]))
        if provision is None:
            # The finding cites a provision the caller did not supply: skip it
            # rather than crash.
            continue

        # Case-law grounding guard: drop the whole finding if any of its cited
        # provisions present in ``provisions`` is ungrounded case law (no resolvable
        # source_external_id). Checking every cited provision, not only ``cited[0]``,
        # stops an ungrounded apex-court chip at a later index riding along on a
        # grounded statute at index 0. Statute, EU and decree citations are never
        # dropped here. A cited id missing from ``provisions`` cannot be surfaced as a
        # chip, so it is no reason to drop the finding (rows only show ``cited[0]``).
        if any(
            (cp := by_id.get(str(pid))) is not None and not _case_law_grounded(cp) for pid in cited
        ):
            continue

        finding_type = getattr(finding, "finding_type", None)
        analysis_layer = getattr(finding, "analysis_layer", None)
        review_state = getattr(finding, "review_state", None) or ""

        if finding_type == _GAP_TYPE or analysis_layer == _GAP_LAYER:
            # Temporal gap demotion happens here, where the provision is available: add 'stale_law'
            # when the cited provision is superseded or annulled. render_gap_item combines it with
            # the reasons L4 stamped. A gap with any reason goes to the visible context_gaps list.
            extra_suppressed: tuple = ()
            if temporal_gap_suppression and temporal_as_of is not None:
                from regsuper.analysis.temporal_freshness import temporal_freshness

                tv = temporal_freshness(
                    getattr(provision, "valid_from", None),
                    getattr(provision, "valid_to", None),
                    getattr(provision, "temporal_status", None),
                    temporal_as_of,
                )
                if tv.get("verdict") in ("superseded", "annulled"):
                    extra_suppressed = ("stale_law",)
            gap_item = render_gap_item(finding, provision, extra_suppressed=extra_suppressed)
            if gap_item.suppressed_by:
                context_gaps.append(gap_item)  # demoted but still visible
            else:
                gaps.append(gap_item)
            if _is_unreviewed(review_state):
                has_unreviewed = True
        elif finding_type == _OBLIGATION_TYPE or analysis_layer == _OBLIGATION_LAYER:
            # With a strategy other than 'first_only', recover the full grounded citation set.
            # Imported lazily, only when needed. expand_citations applies the same grounding guard.
            all_refs: tuple = ()
            if citation_strategy != "first_only":
                from regsuper.analysis.citation_precision import expand_citations

                all_refs = tuple(
                    {
                        "provision_id": r.provision_id,
                        "hierarchy_path": r.hierarchy_path,
                        "is_primary": r.is_primary,
                    }
                    for r in expand_citations(
                        getattr(finding, "cited_provisions", None) or [], by_id, citation_strategy
                    )
                )
            # Per-finding temporal-freshness verdict when the caller supplies an as-of date (so
            # build_digest never reads the clock). Annotation only.
            temporal_ann: dict | None = None
            if temporal_as_of is not None:
                from regsuper.analysis.temporal_freshness import temporal_freshness

                temporal_ann = temporal_freshness(
                    getattr(provision, "valid_from", None),
                    getattr(provision, "valid_to", None),
                    getattr(provision, "temporal_status", None),
                    temporal_as_of,
                )
                # Make the dict JSON-safe: dates become ISO strings, so any consumer (not only
                # FastAPI's encoder) can json.dumps it. Duck-typed, so no date import is needed.
                temporal_ann = {
                    k: (v.isoformat() if hasattr(v, "isoformat") else v)
                    for k, v in temporal_ann.items()
                }
            obligation_item = render_obligation_item(
                finding,
                provision,
                all_cited_refs=all_refs,
                wf4=wf4_by_obj.get(id(finding)),
                temporal=temporal_ann,
            )
            # Demote, never drop: an 'out_of_scope' obligation goes to the visible
            # ``does_not_bind`` section and stays out of the checklist and action lists (it does
            # not bind, so there is nothing to check or do). 'conditional' and 'yes' obligations
            # flow normally, with the verdict carried on the item.
            if obligation_item.binds == "out_of_scope":
                does_not_bind.append(obligation_item)
            else:
                obligations.append(obligation_item)
                checklist.append(render_checklist_item(finding, provision))
                actions.append(render_action_item(finding, provision))
            if _is_unreviewed(review_state):
                has_unreviewed = True

    disclaimer = _digest_disclaimer(answer_mode)

    return Digest(
        obligations=obligations,
        checklist=checklist,
        gaps=gaps,
        actions=actions,
        disclaimer=disclaimer,
        has_unreviewed_citations=has_unreviewed,
        does_not_bind=does_not_bind,
        context_gaps=context_gaps,
    )
