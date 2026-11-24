"""Relation registry: the single source of truth for legal link types.

A relation is `(label, description, inverse, weight)`. The GNN consumes only the sentence
embedding of each description, so adding a new legal link type means adding a row here, with no
retraining. The registry ships a deterministic seed vocabulary, R₀.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Relation:
    rel_id: int
    label: str
    inverse_of: str
    default_weight: float  # prior w₀, times edge confidence; the GNN learns a correction
    deontic: int = 0  # 0 none | 1 obligation | 2 permission | 3 prohibition
    description: str = ""  # one-sentence legal gloss tuned for embedding


# Seed vocabulary R₀: deterministic link types that can be extracted without a model.
R0: tuple[Relation, ...] = (
    Relation(
        1,
        "references",
        "referenced_by",
        0.9,
        0,
        "provision A explicitly cites or points to provision B as relevant law",
    ),
    Relation(2, "referenced_by", "references", 0.7, 0, "provision B is invoked or cited by provision A"),
    Relation(
        3,
        "parent_of",
        "child_of",
        0.6,
        0,
        "provision A structurally contains provision B as a sub-provision (context inheritance)",
    ),
    Relation(
        4, "child_of", "parent_of", 0.6, 0, "provision A is a sub-provision that narrows the scope of its parent B"
    ),
    Relation(
        5, "defines", "uses_term", 0.5, 0, "provision A is the legal definition of a term that provision B relies on"
    ),
    Relation(
        6, "uses_term", "defines", 0.5, 0, "provision A uses a term whose binding legal definition lives in provision B"
    ),
    Relation(7, "transposes", "transposed_by", 1.0, 0, "Czech provision A implements (transposes) EU provision B"),
    Relation(
        8, "transposed_by", "transposes", 1.0, 0, "EU provision A is implemented (transposed) by Czech provision B"
    ),
    # A citation within the same act pulls weaker than a cross-statute one. Split by
    # cross_reference.ref_type ('internal' vs 'external').
    Relation(
        9,
        "internal_reference",
        "internally_referenced_by",
        0.55,
        0,
        "provision A cites provision B within the same legal act (intra-act navigation)",
    ),
    Relation(
        10,
        "internally_referenced_by",
        "internal_reference",
        0.45,
        0,
        "provision B is cited from elsewhere within the same legal act by provision A",
    ),
    # The amendment / delegation / derogation family, registered as a complete vocabulary; the GNN
    # reads them as open-vocabulary relations (no retraining). Only a subset can be built from the
    # consolidated-statute corpus (delegation markers and resolved repeals); amends, consolidates
    # and supersedes need amending-act (novela) and version-lineage data.
    Relation(
        11,
        "delegates_authority",
        "implements",
        0.85,
        0,
        "statutory provision A empowers (delegates authority for) a sub-statutory decree B to be issued under it",
    ),
    Relation(
        12,
        "implements",
        "delegates_authority",
        0.85,
        0,
        "sub-statutory decree A is issued to implement the statutory empowerment in provision B",
    ),
    Relation(13, "amends", "amended_by", 0.45, 0, "amending act A (novela) modifies the text of provision B"),
    Relation(14, "amended_by", "amends", 0.35, 0, "provision A was modified by the amending act B"),
    Relation(
        15, "repeals", "repealed_by", 0.40, 0, "provision A (a repeal clause, zrušovací ustanovení) repeals legal act B"
    ),
    Relation(16, "repealed_by", "repeals", 0.30, 0, "legal act A was repealed by the repeal clause in provision B"),
    Relation(17, "supersedes", "superseded_by", 0.40, 0, "act A is the successor that supersedes the earlier act B"),
    Relation(18, "superseded_by", "supersedes", 0.30, 0, "act A was superseded by the later act B"),
    Relation(
        19,
        "consolidates_into",
        "consolidated_from",
        0.30,
        0,
        "amending text A is folded into the consolidated version B (úplné znění)",
    ),
    Relation(
        20, "consolidated_from", "consolidates_into", 0.25, 0, "consolidated version A incorporates the amending text B"
    ),
    # The case-law-to-statute construal family. A ruling links to the provision it interprets,
    # applies or unifies; the GNN reads them as open-vocabulary relations (no retraining). Those
    # three are traversable. annuls mirrors the temporal-lineage shape of repeals: weight 0 and not
    # traversable, because it records erga omnes derogation lineage rather than a navigation link.
    Relation(
        21,
        "interprets",
        "interpreted_by",
        0.80,
        0,
        "a ruling A authoritatively construes the meaning of statute provision B",
    ),
    Relation(
        22, "interpreted_by", "interprets", 0.70, 0, "provision A has its meaning authoritatively construed by ruling B"
    ),
    Relation(
        23,
        "applies",
        "applied_by",
        0.55,
        0,
        "a ruling A applies provision B to the facts without construing its meaning",
    ),
    Relation(
        24,
        "applied_by",
        "applies",
        0.45,
        0,
        "provision A is applied to the facts by ruling B without its meaning being construed",
    ),
    Relation(
        25,
        "unifies",
        "unified_by",
        0.60,
        0,
        "a unifying opinion A (sjednocující stanovisko) harmonizes a line of case law B",
    ),
    Relation(
        26,
        "unified_by",
        "unifies",
        0.50,
        0,
        "case law A is harmonized by the unifying opinion B (sjednocující stanovisko)",
    ),
    Relation(
        27,
        "annuls",
        "annulled_by",
        0.0,
        0,
        "the Constitutional Court ruling A derogates statute provision B erga omnes",
    ),
    Relation(
        28,
        "annulled_by",
        "annuls",
        0.0,
        0,
        "statute provision A was derogated erga omnes by the Constitutional Court ruling B",
    ),
    # Structural reading-order backbone. Ingestion is flat at the § level and does not turn the
    # act's own sub-structure (Část/Hlava/Díl/Oddíl) into edges, which left 26.7% of in-force
    # provisions isolated in the graph. They concentrate in the large general codes (2,329 in the
    # Civil Code), whose only intra-act links are odst./písm. references that resolve back to the
    # same §. These relations connect each provision to its immediate neighbour within the same
    # structural container (real document order): a low-weight navigation backbone, not a citation.
    # Being open-vocabulary, the GNN warm-starts on them without retraining. Materialised by
    # ingest/hierarchy_edges.py (source 'hierarchy_seq'); reversible with --down.
    Relation(
        29,
        "precedes",
        "follows",
        0.30,
        0,
        "provision A immediately precedes provision B in the same structural section of the act (document reading order)",
    ),
    Relation(
        30,
        "follows",
        "precedes",
        0.30,
        0,
        "provision A immediately follows provision B in the same structural section of the act (document reading order)",
    ),
)

BY_LABEL: dict[str, Relation] = {r.label: r for r in R0}
BY_ID: dict[int, Relation] = {r.rel_id: r for r in R0}

# --- Retrieval relation sets (single source of truth, mirrored into the retrieval SQL) ---
# annuls/annulled_by (27/28) record erga omnes derogation lineage (a status fact, weight 0). They
# are never a four-corners or PPR navigation edge, and are excluded explicitly so they cannot
# surface as a neighbour even if a non-zero weight is set later.
NON_TRAVERSABLE_REL_IDS: tuple[int, ...] = (27, 28)

# The case-law construal relations that link a statute provision out to a ruling. These are the
# only edges the "rulings that engage this §" panel walks: applied_by (24) covers rulings that apply
# the provision, interpreted_by (22) covers rulings that construe it, and unified_by (26) covers
# unifying opinions. All are statute-side inverses of ruling-side relations.
PANEL_INVERSE_CASE_LAW_REL_IDS: tuple[int, ...] = (22, 24, 26)

# The full traversable case-law construal family, in both directions: the statute-to-ruling panel
# relations (22/24/26) plus their ruling-to-statute originals (interprets/applies/unifies,
# 21/23/25). Derived from the registry's inverse links, so a new case-law relation pair (for example
# distinguishes/distinguished_by) extends this set automatically, while non-traversable lineage
# relations (annuls/annulled_by) are excluded by ID. Stage-2 GNN training data unions neighbours
# over this set, so the synthetic data covers case law without hardcoding any single rel_id.
TRAVERSABLE_CASE_LAW_REL_IDS: tuple[int, ...] = tuple(
    sorted(
        {rid for rid in PANEL_INVERSE_CASE_LAW_REL_IDS}
        | {BY_LABEL[BY_ID[rid].inverse_of].rel_id for rid in PANEL_INVERSE_CASE_LAW_REL_IDS}
        - set(NON_TRAVERSABLE_REL_IDS)
    )
)
