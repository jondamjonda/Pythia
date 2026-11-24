"""Tests for the B2b SME-digest render layer (``analysis/render.py``).

These tests assert the PURITY/DETERMINISM contract (binder §16.6 SA-05): the five
render functions take already-fetched ORM-shaped rows and shape them with NO
persistence, NO model calls, NO entropy / wall-clock. We therefore build the
``Finding`` / ``Provision`` stand-ins as plain ``SimpleNamespace`` objects (NO DB,
NO ORM session) and assert:

  * determinism — calling each function twice on the same fixture is byte-equal;
  * shape — the four item types + ``build_digest`` populate the right fields;
  * the provision-absent skip path (a finding citing an unknown provision is
    dropped, not crashed on);
  * the unknown-modal passthrough (``MODAL_TO_CZECH`` never raises KeyError);
  * ``legal_source_url == f"/sources/{id}"`` and a non-empty ``disclaimer_id``.

Confirming the no-I/O contract: this module imports ONLY ``regsuper.analysis.render``
+ ``regsuper.analysis.disclaimers`` — no ``storage.database`` / session / OpenAI.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from regsuper.analysis import render
from regsuper.analysis.disclaimers import (
    DISCLAIMER_INFO,
    DISCLAIMER_INFO_LOW_CONFIDENCE,
    DISCLAIMER_RESERVED_ADVICE,
    DisclaimerBlock,
)
from regsuper.analysis.render import (
    ActionItem,
    ChecklistItem,
    Digest,
    GapItem,
    ObligationItem,
    build_digest,
    render_action_item,
    render_checklist_item,
    render_gap_item,
    render_obligation_item,
)

# A fixed (non-random) UUID — determinism, no entropy in the test either.
_PROV_ID = UUID("11111111-1111-1111-1111-111111111111")
_PROV_ID_2 = UUID("22222222-2222-2222-2222-222222222222")
_MISSING_ID = UUID("99999999-9999-9999-9999-999999999999")


def _provision(
    *,
    pid: UUID = _PROV_ID,
    deontic_modal: str | None = "musí",
    deontic_actor: str | None = "provozovatel",
    deontic_condition: str | None = "do 15 dnů od zahájení činnosti",
    label: str | None = "§ 5 zákona č. 455/1991 Sb.",
    eid: str | None = "sec_5",
    hierarchy_path: str = "act_455_1991.sec_5",
    jurisdiction: str = "CZ",
    legal_hierarchy: str = "act",
) -> SimpleNamespace:
    """A lightweight ``Provision`` stand-in (no ORM, no DB)."""
    return SimpleNamespace(
        id=pid,
        deontic_modal=deontic_modal,
        deontic_actor=deontic_actor,
        deontic_action="oznámit zahájení",
        deontic_condition=deontic_condition,
        label=label,
        eid=eid,
        hierarchy_path=hierarchy_path,
        jurisdiction=jurisdiction,
        legal_hierarchy=legal_hierarchy,
    )


def _finding(
    *,
    finding_type: str = "obligation",
    analysis_layer: str = "L3",
    statement: str = "Musíte oznámit zahájení činnosti.",
    severity: str = "obligation",
    confidence_overall: float = 0.82,
    review_state: str = "unreviewed",
    disclaimer_id: str | None = DISCLAIMER_INFO,
    cited_provisions: list[UUID] | None = None,
    answer_mode: str = "information",
) -> SimpleNamespace:
    """A lightweight ``Finding`` stand-in (no ORM, no DB)."""
    # Default only on None — an explicit ``[]`` must stay empty (the empty-cited
    # skip path under test); ``[] or [_PROV_ID]`` would wrongly backfill it.
    cited = [_PROV_ID] if cited_provisions is None else list(cited_provisions)
    return SimpleNamespace(
        finding_type=finding_type,
        analysis_layer=analysis_layer,
        statement=statement,
        severity=severity,
        confidence_overall=confidence_overall,
        review_state=review_state,
        disclaimer_id=disclaimer_id,
        cited_provisions=cited,
        answer_mode=answer_mode,
    )


# ---------------------------------------------------------------------------
# Determinism — twice == equal, stable repr, no exception.
# ---------------------------------------------------------------------------
def test_render_obligation_item_is_deterministic() -> None:
    f, p = _finding(), _provision()
    a = render_obligation_item(f, p)
    b = render_obligation_item(f, p)
    assert a == b
    assert repr(a) == repr(b)
    assert isinstance(a, ObligationItem)


def test_render_checklist_item_is_deterministic() -> None:
    f, p = _finding(), _provision()
    a = render_checklist_item(f, p)
    b = render_checklist_item(f, p)
    assert a == b
    assert repr(a) == repr(b)
    assert isinstance(a, ChecklistItem)


def test_render_gap_item_is_deterministic() -> None:
    f, p = _finding(finding_type="gap", analysis_layer="L4", severity="gap"), _provision()
    a = render_gap_item(f, p)
    b = render_gap_item(f, p)
    assert a == b
    assert repr(a) == repr(b)
    assert isinstance(a, GapItem)


def test_render_action_item_is_deterministic() -> None:
    f, p = _finding(), _provision()
    a = render_action_item(f, p)
    b = render_action_item(f, p)
    assert a == b
    assert repr(a) == repr(b)
    assert isinstance(a, ActionItem)


def test_build_digest_is_deterministic() -> None:
    findings = [_finding(), _finding(finding_type="gap", analysis_layer="L4")]
    provisions = [_provision()]
    a = build_digest(findings, provisions)
    b = build_digest(findings, provisions)
    assert a == b
    assert repr(a) == repr(b)


# ---------------------------------------------------------------------------
# Field-shaping correctness (the four item types).
# ---------------------------------------------------------------------------
def test_obligation_item_fields() -> None:
    item = render_obligation_item(_finding(), _provision())
    assert item.modal_label == "musíte"  # MODAL_TO_CZECH['musí']
    assert item.actor_label == "provozovatel"
    assert item.deadline == "do 15 dnů od zahájení činnosti"
    assert item.legal_source == "§ 5 zákona č. 455/1991 Sb. (CZ/act)"
    assert item.legal_source_url == f"/sources/{_PROV_ID}"
    assert item.cited_provision_id == str(_PROV_ID)
    assert item.confidence == 0.82
    assert item.review_state == "unreviewed"
    assert item.disclaimer_id == DISCLAIMER_INFO


def test_checklist_item_fields() -> None:
    item = render_checklist_item(_finding(), _provision())
    assert item.obligation == "Musíte oznámit zahájení činnosti."
    assert item.status == "neověřeno"  # from review_state='unreviewed'
    assert item.legal_source_url == f"/sources/{_PROV_ID}"
    assert item.cited_provision_id == str(_PROV_ID)
    assert item.disclaimer_id == DISCLAIMER_INFO


def test_checklist_status_for_suspect_citation() -> None:
    f = _finding(review_state="unverified_citation")
    item = render_checklist_item(f, _provision())
    assert item.status == "nejasné"


def test_gap_item_fields_and_severity_from_modal() -> None:
    f = _finding(
        finding_type="gap",
        analysis_layer="L4",
        severity="gap",
        statement="Chybí evidence oznámení.",
    )
    # 'musí' is a hard duty → critical.
    item = render_gap_item(f, _provision(deontic_modal="musí"))
    assert item.statement == "Chybí evidence oznámení."
    assert item.severity_label == "kritické"
    # 'měl by' is a soft duty → recommended.
    soft = render_gap_item(f, _provision(deontic_modal="měl by"))
    assert soft.severity_label == "doporučené"
    # Anything else → important.
    other = render_gap_item(f, _provision(deontic_modal="může"))
    assert other.severity_label == "důležité"


def test_action_item_priority_from_modal() -> None:
    f = _finding()
    assert render_action_item(f, _provision(deontic_modal="musí")).priority == "okamžitě"
    assert render_action_item(f, _provision(deontic_modal="měl by")).priority == "průběžně"
    assert render_action_item(f, _provision(deontic_modal="může")).priority == "do 30 dnů"


# ---------------------------------------------------------------------------
# Unknown-modal passthrough — never KeyError, falls back to the safe default.
# ---------------------------------------------------------------------------
def test_unknown_modal_passthrough() -> None:
    item = render_obligation_item(_finding(), _provision(deontic_modal="frobnicate"))
    assert item.modal_label == render._MODAL_DEFAULT  # safe default, no KeyError


def test_none_modal_passthrough() -> None:
    item = render_obligation_item(_finding(), _provision(deontic_modal=None))
    assert item.modal_label == render._MODAL_DEFAULT


def test_missing_actor_and_condition_defaults() -> None:
    p = _provision(deontic_actor=None, deontic_condition=None)
    item = render_obligation_item(_finding(), p)
    assert item.actor_label == render._ACTOR_DEFAULT
    assert item.deadline is None


def test_whitespace_condition_collapses_to_none() -> None:
    item = render_obligation_item(_finding(), _provision(deontic_condition="   "))
    assert item.deadline is None


# ---------------------------------------------------------------------------
# legal_source fallback chain + always-populated disclaimer id.
# ---------------------------------------------------------------------------
def test_legal_source_falls_back_to_eid_then_id() -> None:
    no_label = render_obligation_item(_finding(), _provision(label=None))
    assert no_label.legal_source == "sec_5 (CZ/act)"
    bare = render_obligation_item(
        _finding(),
        _provision(label=None, eid=None, hierarchy_path="", jurisdiction="", legal_hierarchy=""),
    )
    assert bare.legal_source == str(_PROV_ID)


def test_disclaimer_id_defaults_when_finding_has_none() -> None:
    item = render_obligation_item(_finding(disclaimer_id=None), _provision())
    assert item.disclaimer_id == DISCLAIMER_INFO
    assert item.disclaimer_id  # always populated


def test_legal_source_url_always_sources_id() -> None:
    for fn in (render_obligation_item, render_checklist_item, render_action_item):
        item = fn(_finding(), _provision())
        assert item.legal_source_url == f"/sources/{_PROV_ID}"
    gap = render_gap_item(_finding(finding_type="gap", analysis_layer="L4"), _provision())
    assert gap.legal_source_url == f"/sources/{_PROV_ID}"


# ---------------------------------------------------------------------------
# build_digest — routing, provision pairing, disclaimer, unreviewed flag.
# ---------------------------------------------------------------------------
def test_build_digest_routes_and_shapes() -> None:
    obligation = _finding(finding_type="obligation", analysis_layer="L3")
    gap = _finding(
        finding_type="gap",
        analysis_layer="L4",
        statement="Chybí evidence.",
        cited_provisions=[_PROV_ID_2],
    )
    provisions = [_provision(), _provision(pid=_PROV_ID_2)]

    digest = build_digest([obligation, gap], provisions)
    assert isinstance(digest, Digest)
    # L3 obligation → obligation + checklist + action (one each).
    assert len(digest.obligations) == 1
    assert len(digest.checklist) == 1
    assert len(digest.actions) == 1
    # L4 gap → one gap row.
    assert len(digest.gaps) == 1
    assert digest.gaps[0].statement == "Chybí evidence."
    # top-level disclaimer is a registry block for the answer mode.
    assert isinstance(digest.disclaimer, DisclaimerBlock)
    assert digest.disclaimer.id == DISCLAIMER_INFO


def test_build_digest_provision_absent_is_skipped() -> None:
    # Finding cites a provision NOT in the supplied list → skipped, no crash.
    orphan = _finding(cited_provisions=[_MISSING_ID])
    present = _finding(cited_provisions=[_PROV_ID])
    digest = build_digest([orphan, present], [_provision()])
    assert len(digest.obligations) == 1  # only the matched finding survives
    assert digest.obligations[0].cited_provision_id == str(_PROV_ID)


def test_build_digest_empty_cited_provisions_is_skipped() -> None:
    digest = build_digest([_finding(cited_provisions=[])], [_provision()])
    assert digest.obligations == []
    assert digest.checklist == []
    assert digest.actions == []
    assert digest.gaps == []


def test_build_digest_unreviewed_flag() -> None:
    # An unreviewed finding flips the flag.
    unreviewed = build_digest([_finding(review_state="unreviewed")], [_provision()])
    assert unreviewed.has_unreviewed_citations is True
    # An explicitly reviewed finding does not.
    reviewed = build_digest([_finding(review_state="reviewed")], [_provision()])
    assert reviewed.has_unreviewed_citations is False


def test_build_digest_disclaimer_per_answer_mode() -> None:
    info = build_digest([], [], answer_mode="information")
    assert info.disclaimer is not None and info.disclaimer.id == DISCLAIMER_INFO
    reserved = build_digest([], [], answer_mode="reserved_advice")
    assert reserved.disclaimer is not None
    assert reserved.disclaimer.id == DISCLAIMER_RESERVED_ADVICE
    review = build_digest([], [], answer_mode="needs_review")
    assert review.disclaimer is not None
    assert review.disclaimer.id == DISCLAIMER_INFO_LOW_CONFIDENCE
    # Unknown mode → safe info default (no KeyError).
    unknown = build_digest([], [], answer_mode="bogus")
    assert unknown.disclaimer is not None and unknown.disclaimer.id == DISCLAIMER_INFO


def test_build_digest_does_not_mutate_findings() -> None:
    f = _finding()
    snapshot = vars(f).copy()
    build_digest([f], [_provision()])
    assert vars(f) == snapshot  # raw finding preserved untouched


def test_build_digest_preserves_order() -> None:
    findings = [
        _finding(statement="first", cited_provisions=[_PROV_ID]),
        _finding(statement="second", cited_provisions=[_PROV_ID_2]),
    ]
    provisions = [_provision(), _provision(pid=_PROV_ID_2)]
    digest = build_digest(findings, provisions)
    assert [o.legal_source for o in digest.obligations][0:2] == [
        digest.obligations[0].legal_source,
        digest.obligations[1].legal_source,
    ]
    # statements preserved in input order on the checklist
    assert [c.obligation for c in digest.checklist] == ["first", "second"]
