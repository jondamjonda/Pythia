"""B2b-19 — structured citation framing + single-corpus pin + case-law guard.

These tests extend the B2b render-layer purity/determinism contract
(``tests/test_render.py``) to the B2b-19 additions, all in ``analysis/render.py``:

  * structured framing — every rendered item carries a RESOLVABLE
    :class:`render.CitedRef` ``{provision_id, label, corpus, hierarchy_path,
    legal_hierarchy, source_external_id}`` (never a bare label);
  * single-corpus pin — ``cited_ref.corpus`` == the pinned constant on every chip;
  * the CASE-LAW grounding guard (resolve-or-drop) — a CASE_LAW provision with no
    resolvable ``source_external_id`` is DROPPED, one with a real CASE_LAW
    ``external_id`` is KEPT, and a statute/EU citation is NEVER dropped;
  * PURITY/DETERMINISM — render produces byte-identical output for identical
    input and triggers NO DB I/O (proven by a tripwire provision whose
    ``source_document`` access RAISES — render must never touch it).

As in ``test_render.py`` the ``Finding`` / ``Provision`` stand-ins are plain
``SimpleNamespace`` objects (NO DB, NO ORM session). This module imports ONLY
``regsuper.analysis.render`` — no ``storage.database`` / session / OpenAI.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from regsuper.analysis import render
from regsuper.analysis.disclaimers import DISCLAIMER_INFO
from regsuper.analysis.render import (
    build_digest,
    render_action_item,
    render_checklist_item,
    render_gap_item,
    render_obligation_item,
)

_PROV_ID = UUID("11111111-1111-1111-1111-111111111111")
_PROV_ID_2 = UUID("22222222-2222-2222-2222-222222222222")


def _provision(
    *,
    pid: UUID = _PROV_ID,
    deontic_modal: str | None = "musí",
    label: str | None = "§ 5 zákona č. 455/1991 Sb.",
    eid: str | None = "sec_5",
    hierarchy_path: str = "act_455_1991.sec_5",
    jurisdiction: str = "CZ",
    legal_hierarchy: str | None = "cz-act",
    source_external_id: str | None = "455/1991",
    source: str | None = None,
) -> SimpleNamespace:
    """A lightweight ``Provision`` stand-in (no ORM, no DB).

    ``source_external_id`` is the already-loaded act-id attribute the caller
    pre-hydrates (render NEVER lazy-loads it from a joined SourceDocument).
    ``source`` is the engine provenance marker (``'court'`` for a ruling); a real
    app ``Provision`` has no such column, so it defaults to ``None`` (absent).
    """
    return SimpleNamespace(
        id=pid,
        deontic_modal=deontic_modal,
        deontic_actor="provozovatel",
        deontic_action="oznámit zahájení",
        deontic_condition="do 15 dnů od zahájení činnosti",
        label=label,
        eid=eid,
        hierarchy_path=hierarchy_path,
        jurisdiction=jurisdiction,
        legal_hierarchy=legal_hierarchy,
        source_external_id=source_external_id,
        source=source,
    )


def _finding(
    *,
    finding_type: str = "obligation",
    analysis_layer: str = "L3",
    statement: str = "Musíte oznámit zahájení činnosti.",
    review_state: str = "unreviewed",
    cited_provisions: list[UUID] | None = None,
) -> SimpleNamespace:
    cited = [_PROV_ID] if cited_provisions is None else list(cited_provisions)
    return SimpleNamespace(
        finding_type=finding_type,
        analysis_layer=analysis_layer,
        statement=statement,
        severity=finding_type,
        confidence_overall=0.82,
        review_state=review_state,
        disclaimer_id=DISCLAIMER_INFO,
        cited_provisions=cited,
        answer_mode="information",
    )


# ---------------------------------------------------------------------------
# (a) every item carries the structured framing.
# ---------------------------------------------------------------------------
def _all_renderers():
    return (
        render_obligation_item(_finding(), _provision()),
        render_checklist_item(_finding(), _provision()),
        render_gap_item(_finding(finding_type="gap", analysis_layer="L4"), _provision()),
        render_action_item(_finding(), _provision()),
    )


def test_every_item_carries_a_resolvable_cited_ref() -> None:
    for item in _all_renderers():
        ref = item.cited_ref
        assert isinstance(ref, render.CitedRef)
        # resolvable id, not a bare label
        assert ref.provision_id == str(_PROV_ID)
        assert ref.label == "§ 5 zákona č. 455/1991 Sb."
        assert ref.hierarchy_path == "act_455_1991.sec_5"
        assert ref.legal_hierarchy == "cz-act"  # real app vocab (lowercase-hyphenated)
        assert ref.source_external_id == "455/1991"
        # back-compat fields are untouched (byte-stable).
        assert item.legal_source_url == f"/sources/{_PROV_ID}"
        assert item.cited_provision_id == str(_PROV_ID)


def test_cited_ref_label_falls_back_to_eid_then_id() -> None:
    ref = render_obligation_item(_finding(), _provision(label=None)).cited_ref
    assert ref.label == "sec_5"  # eid fallback
    bare = render_obligation_item(
        _finding(), _provision(label=None, eid=None, hierarchy_path="")
    ).cited_ref
    assert bare.label == str(_PROV_ID)  # bare-id fallback


# ---------------------------------------------------------------------------
# (b) single-corpus pin — corpus == the pinned constant on every chip.
# ---------------------------------------------------------------------------
def test_corpus_is_the_pinned_constant_on_every_chip() -> None:
    assert render._PINNED_CORPUS == "regsuper"
    for item in _all_renderers():
        assert item.cited_ref.corpus == render._PINNED_CORPUS


# ---------------------------------------------------------------------------
# (b') source_external_id purity handling — nullable, never lazy-loaded.
# ---------------------------------------------------------------------------
def test_source_external_id_is_none_when_not_hydrated() -> None:
    # A provision stand-in WITHOUT the attribute → None (additive, null-safe).
    bare = SimpleNamespace(
        id=_PROV_ID,
        deontic_modal="musí",
        label="§ 5",
        hierarchy_path="a.b",
        jurisdiction="CZ",
        legal_hierarchy="cz-act",
    )
    ref = render_obligation_item(_finding(), bare).cited_ref
    assert ref.source_external_id is None


def test_source_external_id_blank_collapses_to_none() -> None:
    ref = render_obligation_item(_finding(), _provision(source_external_id="   ")).cited_ref
    assert ref.source_external_id is None


# ---------------------------------------------------------------------------
# (c)+(d) the CASE-LAW grounding guard — resolve-or-drop, statute never dropped.
#
# GROUND TRUTH: the pinned-pythia corpus carries the EXACT value
# legal_hierarchy == 'CASE_LAW' for case law (engine n5_check '200 CASE_LAW
# docs'; B2b-18 used .upper()=='CASE_LAW'). The app/regsuper corpus has 0 case
# law and uses lowercase-hyphenated tiers ('cz-act'/'eu-regulation'/'cz-decree'
# /…). The guard must FIRE on the real engine 'CASE_LAW' value AND on its case /
# hyphen-underscore variants — but NEVER on any statute/EU/decree/admin value
# from EITHER vocabulary. These tests are NON-tautological: the case-law family
# and the safe-statute set are DIFFERENT vocabularies on the two sides of every
# assertion, not the same token hardcoded twice.
# ---------------------------------------------------------------------------

# The REAL pinned-corpus case-law value + the variants normalization must fold
# into it. These are inputs the corpus could actually carry, not test-invented
# tokens: 'CASE_LAW' is the engine's literal value.
_CASE_LAW_VARIANTS = ("CASE_LAW", "case_law", "case-law", " CASE_LAW ")

# Statute / EU / decree / administrative-decision values from BOTH vocabularies
# that MUST NEVER be dropped — including the engine-style uppercase tiers AND the
# ambiguous 'cz-decision' (an administrative decision, NOT a court ruling).
_NON_CASE_LAW_HIERARCHIES = (
    "cz-act",
    "eu-regulation",
    "STATUTE",
    "EU_SECONDARY",
    "MINISTERIAL_DECREE",
    "cz-decree",
    "cz-decision",
    None,
    "",
)


def test_guard_fires_on_the_real_engine_case_law_value() -> None:
    # The guard MUST key on the engine's REAL pinned-corpus value 'CASE_LAW'
    # (not a token invented by the test). Ungrounded → dropped; grounded → kept.
    assert not render._case_law_grounded(
        _provision(legal_hierarchy="CASE_LAW", source_external_id=None)
    )
    assert render._case_law_grounded(
        _provision(legal_hierarchy="CASE_LAW", source_external_id="NS-21-Cdo-1234-2024")
    )
    # And it actually drops the chip end-to-end via build_digest.
    dropped = build_digest(
        [_finding()],
        [_provision(legal_hierarchy="CASE_LAW", source_external_id=None)],
    )
    assert dropped.obligations == []


def test_guard_drops_ungrounded_case_law_across_all_variants() -> None:
    # Every case / hyphen-underscore variant of the case-law marker must DROP
    # when there is no resolvable source_external_id (None or blank).
    for hier in _CASE_LAW_VARIANTS:
        for ext in (None, "", "   "):
            digest = build_digest(
                [_finding()],
                [_provision(legal_hierarchy=hier, source_external_id=ext)],
            )
            assert digest.obligations == [], (hier, ext)
            assert digest.checklist == [], (hier, ext)
            assert digest.actions == [], (hier, ext)
            # gap finding too
            gap = build_digest(
                [_finding(finding_type="gap", analysis_layer="L4")],
                [_provision(legal_hierarchy=hier, source_external_id=ext)],
            )
            assert gap.gaps == [], (hier, ext)


def test_guard_keeps_case_law_with_real_source_external_id() -> None:
    # A CASE_LAW provision WITH a real external_id → the chip is KEPT.
    f = _finding()
    p = _provision(legal_hierarchy="CASE_LAW", source_external_id="NS-21-Cdo-1234-2024")
    digest = build_digest([f], [p])
    assert len(digest.obligations) == 1
    ref = digest.obligations[0].cited_ref
    assert ref.legal_hierarchy == "CASE_LAW"
    assert ref.source_external_id == "NS-21-Cdo-1234-2024"


def test_guard_never_drops_statute_eu_decree_or_admin_from_either_vocab() -> None:
    # Statute / EU / decree / admin values from BOTH vocabularies are NEVER
    # dropped, with OR without a source_external_id. 'cz-decision' (an
    # administrative decision) is deliberately included — it must NOT be caught.
    for hier in _NON_CASE_LAW_HIERARCHIES:
        for ext in (None, "", "455/1991"):
            digest = build_digest(
                [_finding()],
                [_provision(legal_hierarchy=hier, source_external_id=ext)],
            )
            assert len(digest.obligations) == 1, (hier, ext)
            assert render._case_law_grounded(
                _provision(legal_hierarchy=hier, source_external_id=ext)
            ), (hier, ext)


def test_guard_drops_finding_with_ungrounded_case_law_at_index_gt_0() -> None:
    # Req 4: an ungrounded CASE_LAW citation at index>0 must NOT ride along on a
    # grounded statute at index 0 — the WHOLE finding is dropped.
    statute = _provision(pid=_PROV_ID, legal_hierarchy="cz-act", source_external_id="455/1991")
    apex = _provision(pid=_PROV_ID_2, legal_hierarchy="CASE_LAW", source_external_id=None)
    f = _finding(cited_provisions=[_PROV_ID, _PROV_ID_2])
    digest = build_digest([f], [statute, apex])
    assert digest.obligations == []  # dropped despite a grounded statute at idx 0
    # But a GROUNDED case-law at index>0 alongside the statute is kept.
    apex_ok = _provision(pid=_PROV_ID_2, legal_hierarchy="CASE_LAW", source_external_id="NS-1")
    kept = build_digest([_finding(cited_provisions=[_PROV_ID, _PROV_ID_2])], [statute, apex_ok])
    assert len(kept.obligations) == 1


def test_case_law_grounded_helper_is_total() -> None:
    # statute (real app vocab) → grounded regardless of external id.
    assert render._case_law_grounded(_provision(legal_hierarchy="cz-act", source_external_id=None))
    # case law grounded iff a resolvable external id is present.
    assert not render._case_law_grounded(
        _provision(legal_hierarchy="CASE_LAW", source_external_id=None)
    )
    assert render._case_law_grounded(
        _provision(legal_hierarchy="CASE_LAW", source_external_id="NS-1")
    )


def test_normalize_hierarchy_folds_case_and_separators() -> None:
    # Normalization is the robustness core: case + hyphen/underscore variants of
    # the case-law marker all fold into the family; app tiers fold OUTSIDE it.
    for hier in _CASE_LAW_VARIANTS:
        assert render._normalize_hierarchy(hier) in render._CASE_LAW_NORMALIZED_FAMILY, hier
    for hier in ("cz-act", "eu-regulation", "cz-decree", "cz-decision", "STATUTE", None, ""):
        assert render._normalize_hierarchy(hier) not in render._CASE_LAW_NORMALIZED_FAMILY, hier
    assert render._normalize_hierarchy("CASE_LAW") == "case law"
    assert render._normalize_hierarchy("cz-act") == "cz act"


# ---------------------------------------------------------------------------
# (c''') TWO-SIGNAL detection — the guard fail-closes on the engine's drift-proof
# provenance marker (source=='court') even when legal_hierarchy is NOT a case-law
# value, and on case-law-NAMING hierarchy compounds (INTERPRETIVE_CASE_LAW /
# cz-case-law) — but NEVER on the ambiguous admin 'cz-decision'. These are the
# REAL markers an N5 ruling carries (engine: legal_hierarchy='CASE_LAW' +
# source='court'), fed against the DIFFERENT app vocab on the other side.
# ---------------------------------------------------------------------------
def test_guard_fires_on_court_provenance_even_when_hierarchy_is_not_case_law() -> None:
    # The authoritative, drift-proof signal: a ruling whose legal_hierarchy is NOT
    # in the case-law family (e.g. a drifted 'cz-decision') but whose provenance is
    # source=='court' MUST still be guarded — ungrounded → dropped, grounded → kept.
    assert not render._case_law_grounded(
        _provision(legal_hierarchy="cz-decision", source="court", source_external_id=None)
    )
    assert render._case_law_grounded(
        _provision(
            legal_hierarchy="cz-decision", source="court", source_external_id="NS-21-Cdo-1234-2024"
        )
    )
    # End-to-end through build_digest: ungrounded court ruling is dropped whole.
    dropped = build_digest(
        [_finding()],
        [_provision(legal_hierarchy="cz-decision", source="court", source_external_id=None)],
    )
    assert dropped.obligations == []
    # source is matched casefolded/stripped.
    for src in ("Court", " COURT ", "court"):
        assert not render._case_law_grounded(
            _provision(legal_hierarchy="cz-act", source=src, source_external_id=None)
        ), src


def test_guard_fires_on_case_law_naming_hierarchy_compounds() -> None:
    # A hierarchy value that NAMES case law — the engine's bindingness token
    # INTERPRETIVE_CASE_LAW if it ever leaked into legal_hierarchy, or 'cz-case-law'
    # — is caught by token containment. Ungrounded → dropped.
    for hier in ("INTERPRETIVE_CASE_LAW", "cz-case-law", "interpretive case law"):
        assert render._is_case_law(_provision(legal_hierarchy=hier, source=None)), hier
        digest = build_digest(
            [_finding()],
            [_provision(legal_hierarchy=hier, source=None, source_external_id=None)],
        )
        assert digest.obligations == [], hier


def test_cz_decision_admin_without_court_source_is_never_dropped() -> None:
    # 'cz-decision' is an ADMINISTRATIVE decision, not a court ruling. Absent the
    # court-provenance signal it is NOT case law and must NEVER be dropped (no
    # false positive), with or without an external id.
    for ext in (None, "", "455/1991"):
        assert not render._is_case_law(
            _provision(legal_hierarchy="cz-decision", source=None, source_external_id=ext)
        ), ext
        digest = build_digest(
            [_finding()],
            [_provision(legal_hierarchy="cz-decision", source=None, source_external_id=ext)],
        )
        assert len(digest.obligations) == 1, ext


def test_provision_source_helper_is_pure_and_total() -> None:
    # Returns the casefolded/stripped source, or None for absent/blank/non-str.
    assert render._provision_source(_provision(source="court")) == "court"
    assert render._provision_source(_provision(source=" Court ")) == "court"
    assert render._provision_source(_provision(source=None)) is None
    assert render._provision_source(_provision(source="   ")) is None
    for bad in (0, False, [], {}, 123):
        assert render._provision_source(_provision(source=bad)) is None, bad
    # A provision stand-in entirely WITHOUT the attribute → None (no AttributeError).
    bare = SimpleNamespace(id=_PROV_ID, legal_hierarchy="cz-act")
    assert render._provision_source(bare) is None


def test_court_provenance_at_index_gt_0_drops_the_whole_finding() -> None:
    # An ungrounded court ruling cited at index>0 (alongside a grounded statute at
    # index 0) drops the WHOLE finding — the all-cited check covers the provenance
    # signal too, not just the hierarchy marker.
    statute = _provision(pid=_PROV_ID, legal_hierarchy="cz-act", source_external_id="455/1991")
    ruling = _provision(
        pid=_PROV_ID_2, legal_hierarchy="cz-decision", source="court", source_external_id=None
    )
    f = _finding(cited_provisions=[_PROV_ID, _PROV_ID_2])
    assert build_digest([f], [statute, ruling]).obligations == []


# ---------------------------------------------------------------------------
# (c'') _source_external_id type-tightening (Req 3) — only a real str is an id.
# ---------------------------------------------------------------------------
def test_source_external_id_rejects_non_string_falsy_and_truthy() -> None:
    # A non-string value (0, False, [], {}, 123) is NOT a resolvable act id and
    # collapses to None — it must never be treated as "present".
    for bad in (0, False, [], {}, 123, 0.0):
        ref = render_obligation_item(_finding(), _provision(source_external_id=bad)).cited_ref
        assert ref.source_external_id is None, bad
    # A non-string value on a CASE_LAW chip therefore DROPS (not-grounded).
    for bad in (0, False, [], {}):
        digest = build_digest(
            [_finding()],
            [_provision(legal_hierarchy="CASE_LAW", source_external_id=bad)],
        )
        assert digest.obligations == [], bad


# ---------------------------------------------------------------------------
# (e) PURITY / DETERMINISM — byte-identical output + NO DB I/O.
# ---------------------------------------------------------------------------
def test_cited_ref_is_deterministic() -> None:
    f, p = _finding(), _provision()
    assert render_obligation_item(f, p) == render_obligation_item(f, p)
    assert build_digest([f], [p]) == build_digest([f], [p])


class _ExplodingSourceDocument:
    """A tripwire: any attribute access blows up (simulates a lazy DB load)."""

    def __getattr__(self, _name):  # pragma: no cover - must never be reached
        raise AssertionError("render touched provision.source_document (lazy DB I/O!)")


def test_render_never_lazy_loads_source_document() -> None:
    """render must read ``source_external_id`` from a plain attribute only — it
    must NEVER walk a ``source_document`` relationship (a lazy DB load). We attach
    a tripwire object that raises on ANY access; render completing proves no I/O.
    """
    p = _provision()
    # Attach the tripwire under the relationship name render must never touch.
    p.source_document = _ExplodingSourceDocument()
    item = render_obligation_item(_finding(), p)
    # The act id still comes from the pre-hydrated plain attribute, untouched.
    assert item.cited_ref.source_external_id == "455/1991"
    digest = build_digest([_finding()], [p])
    assert len(digest.obligations) == 1


# ---------------------------------------------------------------------------
# (f) C4-18 boundary regression: the in-app source URL is LOCKED to
# '/sources/{provision_id}'. A future corpus repoint (the B3-gated live
# source-viewer move to pythia, C4-18) must not silently change this URL without
# tripping this test. Asserts the helper AND the value stamped on every chip.
# ---------------------------------------------------------------------------
def test_legal_source_url_is_locked_to_sources_provision_id() -> None:
    p = _provision()
    expected = f"/sources/{_PROV_ID}"
    # the helper
    assert render._legal_source_url(p) == expected
    # the value on every rendered item
    for item in _all_renderers():
        assert item.legal_source_url == expected
    # and end-to-end through a pinned chip in the digest
    digest = build_digest([_finding()], [p])
    assert digest.obligations[0].legal_source_url == expected
    assert digest.obligations[0].cited_ref.corpus == render._PINNED_CORPUS
