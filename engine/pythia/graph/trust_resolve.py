"""Per-provision trust resolver (pure and deterministic: no database, no LLM).

Every returned provision, the seeds and every graph-connected one, gets a `TrustTag`
(bindingness_tier, section_kind, temporal_status, is_rule, must_surface) resolved at retrieval time
from static inputs only: the provision's own dates, its inherited `section_kind` (from the
section-kind classifier) or `kind_override`, its `analysis_roles` cues and the source document's
bindingness. Nothing is stored, so re-tagging a document takes effect immediately; this resolver,
run in `ports._decorate`, is the single source of truth.

Invariants:
  1. The document's bindingness is an absolute floor. A provision's tier is never weaker than its
     document's tier when it carries a binding cue (roles 2/3/7) or sits in an operative or
     penalties section, and a NULL section_kind falls back to the document tier. Downgrading a
     binding rule to soft law is therefore impossible whatever the classifier gets wrong: the rule
     arm computes the harder of the section tier and the document tier.
  2. The preamble_recital-to-interpretive downgrade is the only downgrade, and it is conservative
     per provision: it applies only in a clearly preamble-marked section to a provision with no
     binding cue.
  3. is_rule = (section_kind in {operative, penalties} and tier in {hard-law, oop}) or a binding
     cue. Taking the union means a section-classifier miss is caught by the cue arm and vice versa.
  4. must_surface = is_rule and temporal_status in {in-force, future-effective}. Future-effective
     rules are shown as upcoming; annulled and superseded ones are historical and stay out of the
     active obligation register.
  5. Deterministic: the static TIER_MAP, the columns and the role set fully decide the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# ─────────────────────────────────────────────────────────────── bindingness tier vocabulary
# Must equal the CHECK vocabulary in the app schema (pinned in CI). Ordered from hardest to softest;
# `_harder` uses the index to apply the document floor (a lower index is a harder tier).
TIER_HARD_LAW = "hard-law"
TIER_OOP = "oop"
TIER_SOFT_LAW = "soft-law"
TIER_INTERPRETIVE = "interpretive"

TIER_ORDER: tuple[str, ...] = (TIER_HARD_LAW, TIER_OOP, TIER_SOFT_LAW, TIER_INTERPRETIVE)
_TIER_RANK: dict[str, int] = {t: i for i, t in enumerate(TIER_ORDER)}

# ──────────────────────────────────────────────── section kinds (mirrored to avoid an import cycle)
KIND_PREAMBLE = "preamble_recital"
KIND_DEFINITIONS = "definitions"
KIND_OPERATIVE = "operative"
KIND_PENALTIES = "penalties"
KIND_TRANSITIONAL = "transitional_final"

# Document bindingness (uppercase, taxonomy.BINDINGNESS) mapped to its tier. These four general-law
# values are the columns of the 20-cell TIER_MAP (5 kinds times 4). BINDING_IN_PERSONAM (an
# individual act, not erga omnes general law) is not a TIER_MAP column; `doc_tier()` maps it to
# interpretive.
_DOC_TIER: dict[str, str] = {
    "HARD_LAW_ERGA_OMNES": TIER_HARD_LAW,
    "BINDING_OOP": TIER_OOP,
    "SOFT_LAW": TIER_SOFT_LAW,
    "INTERPRETIVE_CASE_LAW": TIER_INTERPRETIVE,
}

# `doc_tier()` adds BINDING_IN_PERSONAM (individual acts) to the four TIER_MAP columns.
_DOC_TIER_FALLBACK: dict[str, str] = {**_DOC_TIER, "BINDING_IN_PERSONAM": TIER_INTERPRETIVE}

# Binding-cue roles (obligation, prohibition, sanction; role_classifier.ROLES 2/3/7). Any of them
# makes a provision a rule and applies the document floor.
BINDING_ROLE_IDS: frozenset[int] = frozenset({2, 3, 7})

# ───────────────────────────────────────────────────────────────── contract with the fusion re-rank
# The protected floor in pythia/graph/fusion.py (`is_protected`), which keeps a candidate from being
# pruned by the semantic re-rank, must OR in the resolved `trust.is_rule` (section-level kind plus
# per-provision override), not only the cue-based analysis_roles, so a connected rule is never
# pruned. Its relation lookup must use `BY_LABEL.get(...)`: a seed carries relation_path=['seed'],
# which is not a registry label, and a KeyError would make the engine's fail-soft handler silently
# drop fusion for the whole query. The note below records this contract;
# tests/test_trust_contract_wiring.py checks it.
PROTECTED_RULE_NOTE = (
    "WP-FUSION is_protected MUST OR in trust.is_rule (resolved rule-kind), with the seed-path .get() fix; "
    "see Relevance Fusion Initiative/01_RELEVANCE_FUSION_PLAN.md must-fix #1 + ADDENDUM fold (a)."
)

# Section kinds that are binding in themselves (the section arm of is_rule, and the document floor).
_BINDING_SECTIONS: frozenset[str] = frozenset({KIND_OPERATIVE, KIND_PENALTIES})

# Tiers that count as a rule's tier for the section arm of is_rule.
_RULE_TIERS: frozenset[str] = frozenset({TIER_HARD_LAW, TIER_OOP})

# Temporal statuses that appear in the active obligation register (must_surface).
ACTIVE_TEMPORAL: frozenset[str] = frozenset({"in-force", "future-effective"})

# Resolved temporal statuses that are not active (historical). pit_status can never lift a status
# out of this set (the never-upgrade rule in _refine_temporal_with_pit).
_NOT_ACTIVE_TEMPORAL: frozenset[str] = frozenset({"annulled", "superseded"})

# Constitutional Court annulment-effect values (temporal.ANNULMENT_EFFECT). Either means a repealed
# provision was annulled (erga omnes derogation) rather than merely superseded by a later act.
_ANNULMENT_EFFECTS: frozenset[str] = frozenset({"EX_NUNC", "EX_TUNC"})


# ─────────────────────────────────────────────────────────────────────────── the 20-cell TIER_MAP
# Maps (section_kind, uppercase document bindingness) to a tier. A section can only keep or lower
# the document's tier, never raise it. operative, penalties, transitional_final and definitions keep
# the document tier; preamble_recital in a HARD_LAW or BINDING_OOP document drops to interpretive
# (the only downgrade). In the two softer document tiers a recital is already at or below
# interpretive and keeps it. A NULL section_kind is not in the table: resolve_trust falls back to
# the document tier directly.
TIER_MAP: dict[tuple[str, str], str] = {}
for _kind in (KIND_OPERATIVE, KIND_PENALTIES, KIND_TRANSITIONAL, KIND_DEFINITIONS, KIND_PREAMBLE):
    for _binding, _doc_tier in _DOC_TIER.items():
        if _kind == KIND_PREAMBLE and _binding in ("HARD_LAW_ERGA_OMNES", "BINDING_OOP"):
            TIER_MAP[(_kind, _binding)] = TIER_INTERPRETIVE  # the downgrade
        else:
            TIER_MAP[(_kind, _binding)] = _doc_tier  # keep the document tier


def doc_tier(bindingness: str | None) -> str:
    """Return the document's own tier for its (uppercase) bindingness.

    Unknown or NULL bindingness maps to hard-law, the safe floor: an unclassified document is
    treated as the hardest law so a binding rule is never under-graded.
    """
    if not bindingness:
        return TIER_HARD_LAW
    return _DOC_TIER_FALLBACK.get(bindingness.upper(), TIER_HARD_LAW)


def _harder(a: str, b: str) -> str:
    """Return the harder of two tiers (the lower TIER_ORDER index), as used by the document floor.

    An unknown tier counts as the softest, so a known tier always wins and an unknown value can
    never weaken a real tier.
    """
    return a if _TIER_RANK.get(a, len(TIER_ORDER)) <= _TIER_RANK.get(b, len(TIER_ORDER)) else b


@dataclass
class TrustTag:
    """The resolved trust facets of one provision (the shape of CitedProvision.trust)."""

    bindingness_tier: str  # hard-law, oop, soft-law or interpretive
    section_kind: str  # inherited or overridden kind (default operative)
    temporal_status: str  # in-force, future-effective, annulled or superseded
    is_rule: bool  # a binding duty, prohibition or sanction
    must_surface: bool  # is_rule and temporally active: goes in the obligation register
    doc_tier: str  # the document's own tier (the floor), kept for auditing
    reasons: list[str] = field(default_factory=list)  # derivation trail, for auditing


def _temporal_status(
    valid_from: date | None,
    valid_to: date | None,
    as_of: date | None,
    temporal_status: str | None,
    annulment_effect: str | None,
    annuls_target: bool,
) -> str:
    """Return the provision's own temporal status as of `as_of` (None means today).

    The section kind never affects temporal status.

      - Expired (valid_to on or before as_of) or flagged repealed: "annulled" if it is the target of
        an annuls edge (relations 27/28) or its document has an annulment effect (EX_NUNC/EX_TUNC),
        otherwise "superseded". Either way it is historical, never active.
      - valid_from after as_of: "future-effective" (enacted, not yet in force).
      - Otherwise: "in-force".
    """
    today = as_of or date.today()
    repealed_flag = (temporal_status or "").upper() in ("REPEALED", "ANNULLED_BY_CC")
    expired = valid_to is not None and valid_to <= today
    if expired or repealed_flag:
        if annuls_target or (annulment_effect or "").upper() in _ANNULMENT_EFFECTS:
            return "annulled"
        return "superseded"
    if valid_from is not None and valid_from > today:
        return "future-effective"
    return "in-force"


def _refine_temporal_with_pit(step1_status: str, pit_status: str | None) -> str:
    """Refine the document-level temporal status with the provision's own point-in-time status.

    The refinement only ever restricts: pit_status can move a provision towards not active, never
    towards more active. The result is never more active than `step1_status`, and a provision that
    either signal calls superseded or annulled is never surfaced as current.

      - If `step1_status` is already not active ('annulled' or 'superseded') it is returned
        unchanged. pit_status can never lift a document-level repeal or annulment, not even when
        it says 'IN_FORCE'.
      - Otherwise ('in-force' or 'future-effective'):
          - pit_status 'SUPERSEDED' gives 'superseded'. This is the safety gain: a § superseded
            inside a still-in-force act is not surfaced as a current rule.
          - pit_status 'NOT_YET_IN_FORCE' turns 'in-force' into 'future-effective' (still active,
            but upcoming rather than current).
          - Anything else (IN_FORCE, None, unknown) leaves the status unchanged.

    Pure: depends only on the two arguments (no database, settings or `as_of`).
    """
    if step1_status in _NOT_ACTIVE_TEMPORAL:
        return step1_status  # never upgrade: pit_status cannot lift a restriction
    # Coerce to str and strip, so stray whitespace or a non-str value cannot silently skip the
    # checks below.
    pit = str(pit_status or "").strip().upper()
    if pit == "SUPERSEDED":
        return "superseded"  # superseded at provision level
    if pit == "NOT_YET_IN_FORCE" and step1_status == "in-force":
        return "future-effective"  # not yet in force: still active, but upcoming
    return step1_status  # IN_FORCE, None or unknown: no change


def resolve_trust(row, as_of: date | None = None, *, downweight: bool = False) -> TrustTag:
    """Resolve a provision's TrustTag from its columns (pure: no database, no LLM).

    Steps: (1) temporal status, (2) section kind to tier, (3) document floor, downgrade guard,
    is_rule and must_surface.

    Args:
        row: A ProvisionRow-like object or a dict. Every attribute is optional with a safe default:
            valid_from and valid_to (date), temporal_status, annulment_effect, annuls_target (bool:
            whether the provision is the target of an annuls edge, relations 27/28), bindingness
            (the document's, uppercase), inherited_kind (the section_kind found by walking up
            parent_id), kind_override, analysis_roles (list of role ids) and pit_status.
        as_of: Reference date; None means today.
        downweight: If true, refine the document-level temporal status with the provision's own
            `pit_status` (see `_refine_temporal_with_pit`) before computing must_surface. The
            refinement only restricts. If false (the default) `pit_status` is never read.
    """
    g = _getter(row)
    bindingness = g("bindingness")
    roles = set(g("analysis_roles") or ())
    binding_cue = bool(roles & BINDING_ROLE_IDS)
    dt = doc_tier(bindingness)
    reasons: list[str] = []

    # 1. Temporal status (own dates only).
    temporal_status = _temporal_status(
        g("valid_from"), g("valid_to"), as_of, g("temporal_status"), g("annulment_effect"), bool(g("annuls_target"))
    )

    # 2. Kind is the override, else the inherited kind, else operative; tier from TIER_MAP (or the
    #    document tier when no kind is set).
    kind = g("kind_override") or g("inherited_kind") or KIND_OPERATIVE
    if kind not in (KIND_PREAMBLE, KIND_DEFINITIONS, KIND_OPERATIVE, KIND_PENALTIES, KIND_TRANSITIONAL):
        kind = KIND_OPERATIVE
    raw_inherited = g("kind_override") or g("inherited_kind")
    if raw_inherited is None:
        tier = dt  # NULL section_kind: the document tier (invariant 1)
        reasons.append("null-kind→doc-tier")
    else:
        tier = TIER_MAP.get((kind, (bindingness or "").upper()), dt)

    in_binding_section = kind in _BINDING_SECTIONS

    # 3a. Downgrade guard. TIER_MAP has already downgraded a recital; undo that for any provision
    #     that carries a binding cue (a duty, prohibition or sanction inside a mis-marked preamble
    #     is a rule, not context).
    if kind == KIND_PREAMBLE and binding_cue:
        tier = dt
        reasons.append("recital-with-binding-cue→hold-doc-tier")

    # 3b. Document floor. With a binding cue, or in an operative or penalties section, the tier is
    #     never weaker than the document's: take the harder of the section tier and the document
    #     tier.
    if binding_cue or in_binding_section:
        floored = _harder(tier, dt)
        if floored != tier:
            reasons.append(f"doc-floor {tier}→{floored}")
        tier = floored

    # 3c. is_rule: (binding section and a rule tier) or any binding cue.
    is_rule = (in_binding_section and tier in _RULE_TIERS) or binding_cue
    if is_rule:
        reasons.append("is_rule")

    # 3d. Optional down-weighting (downweight=True): refine the document-level temporal status with
    #     the provision's pit_status, so a § superseded inside a still-in-force act becomes
    #     'superseded' and is not surfaced as current. It only restricts. This runs after is_rule,
    #     so it never changes is_rule.
    if downweight:
        refined = _refine_temporal_with_pit(temporal_status, g("pit_status"))
        if refined != temporal_status:
            reasons.append(f"pit-downweight {temporal_status}→{refined}")
        temporal_status = refined

    # 3e. must_surface: a rule that is temporally active (in force, or future-effective).
    must_surface = is_rule and temporal_status in ACTIVE_TEMPORAL

    return TrustTag(
        bindingness_tier=tier,
        section_kind=kind,
        temporal_status=temporal_status,
        is_rule=is_rule,
        must_surface=must_surface,
        doc_tier=dt,
        reasons=reasons,
    )


def _getter(row):
    """Return an accessor so resolve_trust works on a ProvisionRow or a plain dict (in tests)."""
    if isinstance(row, dict):
        return row.get
    return lambda key, default=None: getattr(row, key, default)
