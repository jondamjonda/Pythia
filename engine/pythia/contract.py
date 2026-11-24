"""The Pythia engine's public data contract.

Everything downstream consumes `PythiaAnswer`. `PythiaQuery` is the request, `CitedProvision` one
retrieved provision, and the remaining models are optional, additive parts of an answer (sections,
evidence bundle, question-gate verdict, structured action plan). `compliance_packet` is still a
free-form dict.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class TrustTag(BaseModel):
    """The resolved trust facet of one provision (`CitedProvision.trust`).

    Set in `engine._decorate` by the trust resolver (pythia.graph.trust_resolve.resolve_trust) for
    every returned provision, seeds and graph-connected ones alike. All fields are optional, so a row
    whose trust could not be resolved (for example a fetch path that omits the columns) yields an
    empty tag rather than a fabricated badge, matching the app's null-trust contract.

    Frozen: a resolved tag is an immutable derivation. The flat `CitedProvision.bindingness` and
    `temporal_status` facets remain for backward compatibility. `bindingness_tier` is the hyphenated
    tier (hard-law / oop / soft-law / interpretive), a derived view distinct from the raw
    document-level `bindingness` (upper case).
    """

    model_config = ConfigDict(frozen=True)

    bindingness_tier: str | None = None  # hard-law / oop / soft-law / interpretive
    section_kind: str | None = None  # operative / penalties / transitional_final / definitions / preamble_recital
    temporal_status: str | None = None  # in-force / future-effective / annulled / superseded
    is_rule: bool = False  # a binding duty, prohibition or sanction (by section kind or by cue)
    must_surface: bool = False  # is_rule and temporally active: goes into the obligation register
    kind_source: str | None = None  # audit: how the kind and tier were derived (the resolver's reasons)


class Section(BaseModel):
    """One machine-parseable section of a drafted answer.

    Additive: the flat `drafted_answer` remains the complete answer. Built purely from the
    section-headed draft by `reasoning.sections.split_into_sections` (no LLM, database or clock).

    - `kind` is the canonical machine tag (Legislativní rámec: legislative, Judikatura: case_law,
      Hodnocení: assessment); `heading` is the canonical display heading.
    - `text` is this section's verbatim slice of the draft, exactly
      `drafted_answer[range[0]:range[1]]`. It includes the section's own '##' heading line; the
      parsed `heading` field carries the heading separately so a UI may strip it.
    - `range` is [start, end] in draft-relative character offsets.
    - `citations_map` holds the chips of the flat `PythiaAnswer.citations_map` whose span falls inside
      this section, unchanged (offsets stay draft-relative, never re-based).

    The sections tile the whole draft contiguously (ordered by range, the first starts at 0, the last
    ends at len, each ends where the next begins: no gap, no overlap). The union of their
    `citations_map` is therefore a true partition of the flat map: every grounded chip is in exactly
    one section, and a chip's offsets still slice its span out of the whole `drafted_answer`.
    """

    # The essay kinds (legislative / case_law / assessment) plus the seven action-plan kinds
    # (situation / applies / obligations / steps / where / risks / gaps). The action-plan kinds are
    # emitted only with structured_answer_enabled: sections.py shares one _HEADINGS table, but the
    # structured headings only match a draft produced by _GEN_PROMPT_STRUCTURED. Otherwise the
    # splitter emits only the three essay kinds (a test pins this).
    kind: Literal[
        "legislative",
        "case_law",
        "assessment",
        "situation",
        "applies",
        "obligations",
        "steps",
        "where",
        "risks",
        "gaps",
    ]
    heading: str
    text: str
    range: list[int] = Field(default_factory=list)  # [start, end] draft-relative char offsets
    citations_map: list[dict] = Field(default_factory=list)


class DraftSlot(BaseModel):
    """One slot of the typed draft plan, mirroring one `Section`.

    The draft plan lets claim verification consume the structure of a draft directly instead of
    re-deriving it from the rendered sections. A slot copies the section's `kind`, display `heading`,
    draft-relative `range` ([start, end]) and the `citation_map` chips inside it, so the section can
    be rebuilt losslessly from the slot and the draft (`draft[range[0]:range[1]]`). It adds
    `seed_provision_ids`: the ordered, unique provision ids behind the slot's chips. That field is
    derived and is not read back when rebuilding the section, so it never affects the round trip.
    """

    kind: Literal[
        "legislative",
        "case_law",
        "assessment",
        "situation",
        "applies",
        "obligations",
        "steps",
        "where",
        "risks",
        "gaps",
    ]
    heading: str
    range: list[int] = Field(default_factory=list)  # [start, end] draft-relative char offsets
    citation_map: list[dict] = Field(default_factory=list)
    seed_provision_ids: list[str] = Field(default_factory=list)


class DraftPlan(BaseModel):
    """The typed, ordered draft plan: what the draft says and how it is structured.

    Its `slots` mirror `PythiaAnswer.sections` one for one and in order, tiling [0, len(draft)]
    contiguously just as the sections do, so the plan is a lossless typed view of the draft:
    `pythia.reasoning.draft_plan.sections_from_plan(plan, draft)` reproduces the section list
    exactly. Built only when `config.draft_planner_enabled` is on, and attached as
    `audit_trace['draft_plan']`.
    """

    slots: list[DraftSlot] = Field(default_factory=list)


class PythiaQuery(BaseModel):
    # Retrieve versus answer is chosen by the `method` query parameter at the service edge and by
    # which engine entry point the caller invokes, not by a field on this model.
    query: str
    jurisdiction: list[str] = Field(default_factory=lambda: ["CZ", "EU"])
    project_context: dict[str, Any] | None = None
    max_hops: int = 3  # "four corners" expansion depth
    as_of: date | None = None  # point-in-time law; None means current law
    verify: Literal["off", "gated", "always"] = "gated"
    k: int = 20
    # Optional override for hybrid_seed's rerank candidate pool. None means
    # rk = max(k * 2, _RERANK_POOL); when set, rk = max(k * 2, rerank_pool). answer() sets it only
    # when seed_pool_scale_enabled is on.
    rerank_pool: int | None = None
    # Per-request override for the fusion re-rank (graph score combined with semantic cosine). None
    # means engine.retrieve follows the global fusion_enabled setting. answer() sets fusion=True under
    # answer_path_fusion_enabled, so the answer path can fuse while the app's /v1/retrieve lane keeps
    # following the global flag.
    fusion: bool | None = None


class CitedProvision(BaseModel):
    provision_id: str
    label: str | None = None
    jurisdiction: str | None = None
    legal_hierarchy: str | None = None
    source_title: str | None = None
    text_md: str | None = None
    relation_path: list[str] = Field(default_factory=list)  # the route of typed edges that reached it
    hops: int = 0
    score: float = 0.0
    # Document-level taxonomy facets of the source instrument, used to render trust (optional).
    # bindingness: hard-law / OOP / soft-law / ...; is_authoritative: False for a consolidated text
    # (informative only); temporal_status: in-force / repealed / ...
    bindingness: str | None = None
    is_authoritative: bool | None = None
    temporal_status: str | None = None
    # Section-level trust: the resolved tier, kind, temporal status and is_rule tag, set in
    # engine._decorate for every provision. None until the resolver runs (for example a bare
    # CitedProvision built outside the engine), never a fabricated tier. The flat facets above stay
    # for backward compatibility; `trust` is the structured, rule-aware view a trust-rendering UI reads.
    trust: TrustTag | None = None
    # Fused retrieval confidence and its per-provision breakdown, copied from
    # NeighborHit.fused_score / .why in engine._decorate when fusion runs. None on the structural-only
    # path and for any provision without a semantic score.
    confidence_retrieval: float | None = None
    confidence_breakdown: dict | None = None
    # Temporal display facets, filled in answer.py from ProvisionRow.pit_status / .stale_vector only
    # when config.temporal_consumer_enabled is on (otherwise None). They let a citation show its
    # temporal standing honestly but are not ranking or refusal inputs: they do not change which
    # provisions are cited, their order, the resolved `trust` or the obligation register.
    # `pit_status` is the provision's own stored point-in-time status, distinct from the flat
    # `temporal_status` (document level) and from `trust.temporal_status` (resolved at as_of).
    pit_status: str | None = None  # IN_FORCE / SUPERSEDED / NOT_YET_IN_FORCE (per provision)
    vector_stale: bool | None = None  # the embedding was computed from old text (embedded_text_hash <> text_hash)


class EvidenceProvision(BaseModel):
    """One row of the EvidenceBundle: a provision's id, its full text and its act/section locator.

    This is the single canonical full-text carrier that the citation verifier, the practical-fact
    and slot passes and the section splitter all read, so no consumer re-fetches or re-slices a
    provision. Every character span a consumer emits is an index into `text` (the verbatim invariant
    `value_raw == text[start:end]` holds against this field).

    Built once via NexusPort.fetch_provisions inside reasoning/evidence_bundle.py, and only when
    `config.evidence_bundle_enabled` is on. `promoted` marks a row pulled in by neighbour promotion
    (a cited section referred to an adjacent section or clause outside the original selection)
    rather than by the top-k selection; it is a display and audit facet, never a ranking input.
    `source_pid` records the parent section a clause-snapped row descended from (None for a
    top-level row). Every field has a safe default, so a row whose text could not be fetched still
    builds with an empty text.
    """

    model_config = ConfigDict(frozen=True)

    provision_id: str
    ext: str | None = None  # the act identifier (CZ "N/YYYY" Sb. number or EU CELEX)
    label: str | None = None  # the §/Article locator ("§ 297", "Article 27")
    text: str = ""  # the full provision text, never truncated (the base for span indices)
    promoted: bool = False  # True if pulled in by neighbour promotion rather than the top-k selection
    source_pid: str | None = None  # the parent § a clause-snapped row descended from (else None)
    # Labels ("(2)", "a)") of the clauses that clause snapping (ctx_clause_snap_enabled) narrowed this
    # provision's context windows to. None unless that flag is on and the provision_clause index had a
    # matching clause. A display and audit facet, never a ranking or grounding input.
    clause_labels: list[str] | None = None

    @field_serializer("text", when_used="json")
    def _redact_text_on_wire(self, _text: str) -> str:
        """Blank the full provision text in JSON output.

        The full text is an internal verifier carrier (up to 20k characters per provision) and must
        never cross the /v1/answer wire. Only JSON serialization is affected: internal consumers read
        the text by attribute access, `text_for()` or `cited_texts()`, and python-mode `model_dump()`
        keeps it. Spans, labels and ids still serialize; every span indexes the carrier the engine
        holds, not the wire payload.
        """
        return ""


class ResolvedCitation(BaseModel):
    """One draft citation, resolved (or not) against the EvidenceBundle.

    A citation in the draft (a `[act § N]` bracket or a bare `§ N`) is resolved to a provision id in
    the bundle, together with the character span it occupies in the draft. A citation whose
    (act, section) is not in the bundle even after neighbour promotion is reported with
    resolved=False, never silently dropped, so the citation verifier can demote it. `snapped_from`
    records the parent provision id when clause snapping narrowed a citation of a parent section to a
    subsection or clause row. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    act: str | None = None  # the parsed act id (None for a bare § citation)
    section: str | None = None  # the parsed §/Article number
    draft_start: int = 0  # the citation span's char offset in the draft
    draft_end: int = 0
    label: str | None = None  # the verbatim citation marker text in the draft
    resolved: bool = False  # True if mapped to a provision_id in the bundle
    provision_id: str | None = None  # the resolved bundle provision (None when unresolved)
    snapped_from: str | None = None  # the parent § id clause snapping descended from (else None)


class EvidenceBundle(BaseModel):
    """The canonical full-text provision carrier plus the citation-resolution result.

    Shared by the citation verifier, the practical-fact passes and the section splitter so no
    consumer re-fetches or re-slices a provision. Built once in
    reasoning/evidence_bundle.build_bundle (via NexusPort.fetch_provisions), and only when
    `config.evidence_bundle_enabled` is on; otherwise `PythiaAnswer.evidence` is None.

    `provisions` is the ordered carrier: the top-k selection first, then any neighbour-promoted or
    clause-snapped rows. `resolved` lists every draft citation with its outcome (unresolved
    citations are reported, never dropped). Total: an empty selection or a malformed draft yields an
    empty bundle, never an exception.
    """

    provisions: list[EvidenceProvision] = Field(default_factory=list)
    resolved: list[ResolvedCitation] = Field(default_factory=list)

    def by_id(self) -> dict[str, EvidenceProvision]:
        """Return an id-to-row index of the carrier (first seen wins).

        Derived on each call rather than stored, to keep the model simple.
        """
        out: dict[str, EvidenceProvision] = {}
        for p in self.provisions:
            out.setdefault(p.provision_id, p)
        return out

    def text_for(self, provision_id: str) -> str:
        """Return the full carrier text for a provision id ("" if absent).

        This is the accessor consumers index spans into: `value_raw == text_for(pid)[start:end]`.
        """
        return self.by_id().get(provision_id, EvidenceProvision(provision_id=provision_id)).text

    def cited_texts(self) -> dict[str, str]:
        """Return {provision_id: full text}, read by the citation verifier as evidence['cited_texts']."""
        return {p.provision_id: p.text for p in self.provisions}

    def snap_map(self) -> dict[str, str]:
        """Return {parent_pid: child_pid} for every resolved citation that was clause-snapped.

        This is the substitution table consumers apply: when a citation landed on a parent section but
        was narrowed to a subsection or clause row, a consumer keyed on the parent id should read the
        narrower child text. First seen wins; the resolver already refuses ambiguous snaps, so a parent
        maps to at most one child. Derived on each call, never stored.
        """
        out: dict[str, str] = {}
        for c in self.resolved:
            if c.resolved and c.snapped_from and c.provision_id:
                out.setdefault(c.snapped_from, c.provision_id)
        return out

    def text_for_cited(self, provision_id: str) -> str:
        """Return the carrier text a consumer should read for a cited provision id.

        Applies clause snapping: the narrower subsection or clause text when this id was snapped to a
        child, otherwise the id's own full text. The result is one row's `.text` verbatim, so the
        span invariant `value_raw == text_for_cited(pid)[start:end]` holds. Returns "" when the
        (snapped) id is not in the bundle.
        """
        return self.text_for(self.snap_map().get(provision_id, provision_id))

    def unresolved(self) -> list["ResolvedCitation"]:
        """Return every draft citation that did not resolve to a bundle provision."""
        return [c for c in self.resolved if not c.resolved]


class MetaVerdict(BaseModel):
    """The question gate's typed verdict: the anti-hallucination audit record.

    Produced by `reasoning.metagate.classify_meta` before generation and attached to
    `PythiaAnswer.meta` only when `config.metagate_enabled` is on.

    `route` is the first-match-wins decision of the pure `route(intent, coverage, premise)`
    function; the only route to a generated, grounded answer is `sound` (all three axes pass). The
    three axis verdicts and their confidences are recorded for audit and calibration. Every non-sound
    route carries only grounded citations (ids from the retrieved set): `closest_provisions` for
    `not_in_corpus` and `correcting_citations` for `false_premise`, so even an honest non-answer
    cannot fabricate. Every field has a safe default, so an unparseable, fail-closed verdict is still
    a valid object.
    """

    route: Literal["sound", "not_a_legal_question", "not_in_corpus", "false_premise"]
    intent: Literal["legal", "not_legal"] = "legal"
    intent_confidence: float = 0.0
    coverage: Literal["covered", "weak_coverage", "no_coverage", "out_of_scope"] = "covered"
    coverage_confidence: float = 0.0
    premise: Literal["sound", "false_premise"] = "sound"
    premise_confidence: float = 0.0
    # not_in_corpus: the closest related provisions, flagged as not on point. Grounded (ids from the
    # retrieved set) and resolved through fetch_provisions, so the chips are real.
    closest_provisions: list[CitedProvision] = Field(default_factory=list)
    # false_premise: the assumed (false) claim and the grounded correction. Grounded
    # correcting_citations (provision ids from the retrieved set) are a hard precondition for
    # adopting a false_premise verdict.
    assumed_claim: str | None = None
    correction: str | None = None
    correcting_citations: list[CitedProvision] = Field(default_factory=list)
    reason: str = ""
    # Diagnostics for calibration: the deterministic coverage-axis features the classifier already
    # computed for `_classify_coverage` ({top_raw, n_strong, strong_cos, support, support_conf,
    # coverage_floor}), recording which signal passed or failed (the LLM support test or the
    # strong-cosine signal). Observability only: never re-run and never used for routing. Present
    # only when the coverage axis ran (intent == legal).
    coverage_features: dict | None = None


class PracticalItem(BaseModel):
    """One routed practical fact, keyed to a cited (grounded) provision.

    Produced only with structured_answer_enabled, by the deterministic slot router.

    - `slot`: the action-plan slot the fact was routed to (applies / obligations / steps / where /
      risks).
    - `fact_type`: the practical-fact extractor type (deadline / authority / penalty / form / role /
      kind) or a routing-signal tag.
    - `value_raw`: the verbatim excerpt from the provision text, never transformed.
    - `provision_id`: the grounded provision it came from.
    - `grounded`: whether slot_verify confirmed that the slot's prose carries a grounded chip (False
      means the slot was demoted to an honest gap line).
    """

    model_config = ConfigDict(frozen=True)

    slot: str
    fact_type: str
    value_raw: str | None = None
    provision_id: str | None = None
    grounded: bool = True


# Design note: the claim-verify outcome deliberately has no dedicated fields on PythiaAnswer.
#   - Gaps live on `PracticalBlock.gaps` (the structured sidecar below). A gap is a property of the
#     practical block, not of the whole answer, so there is no top-level `PythiaAnswer.gaps`.
#   - The claim-verify outcome is carried by `audit_trace["claim_ledger"]` (rows plus a summary:
#     mode, claims_total, per_status, demoted, ...), which the app bridge (`_emit_claims_summary`)
#     and /metrics already consume.
#   Revisit this if a first-class consumer ever needs the loop outcome outside audit_trace.
class PracticalBlock(BaseModel):
    """The structured action-plan sidecar (`PythiaAnswer.practical`).

    Present only when `config.structured_answer_enabled` is on. Carries the deterministically routed
    practical `items` (each keyed to a grounded provision); the `gaps`, which are draft copy pending
    advocate review for known-absent data classes plus any claim slot slot_verify demoted; and
    `profile_used`, whether a business profile seeded the 'Vaše situace' slot. No consumer is
    required to read it: the flat `drafted_answer` and `sections` remain the complete answer.
    """

    items: list[PracticalItem] = Field(default_factory=list)
    gaps: list[dict] = Field(default_factory=list)  # {gap_class, text, status} draft gap lines
    demoted_slots: list[str] = Field(default_factory=list)  # claim slots slot_verify demoted for lack of grounding
    profile_used: bool = False


class PythiaAnswer(BaseModel):
    refused: bool = False
    # The grounding outcome. A UI must tell an honest "no provision applies" (abstain) apart from "the
    # draft grounded nothing" (refuse); both set refused=True, so refused alone cannot distinguish
    # them. None for a retrieve-only call (no answer was drafted, so there is no verdict).
    verdict: Literal["ok", "abstain", "refuse"] | None = None
    reason: str | None = None
    drafted_answer: str | None = None
    cited_provisions: list[CitedProvision] = Field(default_factory=list)
    # The obligation register: a connected provision of rule kind is a limit on the user and must be
    # surfaced, never buried. Holds the provision_ids of every cited provision whose resolved trust
    # has must_surface=True (is_rule and temporally active: in force or future-effective). The render
    # layer lists these, and the fusion re-rank's protected floor keeps them from being pruned.
    obligation_register: list[str] = Field(default_factory=list)
    reasoning_subgraph: dict[str, Any] = Field(default_factory=dict)  # nodes + typed edges used
    compliance_packet: dict[str, Any] | None = None  # free-form; not yet structured
    confidence: float = 0.0
    # Span-to-provision citation map: one {start, end, provision_id, label, corpus} entry per grounded
    # inline citation in `drafted_answer`. `start` / `end` are character offsets into the draft, so a
    # UI can render a clickable chip over the exact citation text. `provision_id` is the grounded
    # provision (always among the grounded cited provisions; there is never a chip without one),
    # `corpus` is its act identifier (CZ "N/YYYY" Sb. number or EU CELEX), and `label` the matched
    # marker text. Empty on a retrieve-only call, an abstain or a wholly ungrounded refuse.
    citations_map: list[dict] = Field(default_factory=list)
    # The drafted answer split into sections: in the essay shape Legislativní rámec (legislative),
    # Judikatura (case_law) and Hodnocení (assessment). Each carries its prose slice and the chips of
    # `citations_map` inside it (the same dicts, with draft-relative offsets). The sections tile the
    # whole draft, so their chips partition the flat map: every grounded chip is in exactly one
    # section. The Judikatura section is absent rather than empty: when there is no grounded case-law
    # citation it is merged into a neighbour instead of emitted on its own, so a statute chip the LLM
    # misplaced under it is kept and statute-only answers carry no case_law section yet lose no chip.
    # Empty when the draft has no parseable section markers (`drafted_answer` is still the complete
    # answer) and on retrieve-only, abstain and refuse calls.
    sections: list[Section] = Field(default_factory=list)
    # The structured action-plan sidecar, present only when config.structured_answer_enabled is on:
    # routed practical facts keyed to grounded provisions, draft gap lines, the claim slots
    # slot_verify demoted, and whether a business profile seeded the situation slot. None for essay
    # answers and for retrieve-only, abstain and refuse calls.
    practical: PracticalBlock | None = None
    audit_trace: dict[str, Any] = Field(default_factory=dict)
    # The question-gate verdict, present only when config.metagate_enabled is on. A non-sound route
    # sets refused=True (no generated obligation answer) and carries the grounded closest or
    # correcting citations.
    meta: MetaVerdict | None = None
    # The confidence-band presentation grade, set only on an answer the question gate cleared
    # (full or hedged). It never decides whether to answer or refuse.
    band: Literal["full", "hedged", "closest", "notlegal", "falsepremise", "refuse"] | None = None
    render_mode: str | None = None
    hedge: dict[str, Any] | None = None
    # The evidence bundle, present only when config.evidence_bundle_enabled is on: the canonical
    # full-text provision carrier and the per-citation resolution result (unresolved citations
    # reported, never dropped). None on retrieve-only calls.
    evidence: "EvidenceBundle | None" = None
    # Why this answer is a non-answer, or was served in a degraded render_mode: one of the
    # NoAnswerCause values (intent_not_legal / coverage_no_coverage / coverage_weak_coverage /
    # out_of_scope / premise_false / grounding_floor / compliance_block / verify_enforce), or "none"
    # for a normally served, grounded answer. Observe only: derived from signals answer() already
    # computed (the question-gate verdict, the grounding verdict, the enforce-refusal flag), so it
    # never changes what is served. The same string is in audit_trace["no_answer_cause"]. It makes a
    # distinction `refused` and `render_mode` cannot: `grounding_floor` (a draft was produced but
    # grounded too little) is separate from the question-gate coverage and premise routes and from a
    # compliance block.
    no_answer_cause: str = "none"
