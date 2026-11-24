"""WF-3 Increment 1 — citation precision (multi-cite expansion) pure core + eval.

Covers the deterministic expander, its three strategies, and — most importantly — the SAFETY INVARIANTS that
make recovery a strictly-additive, never-suppressing layer:

  INV-1 (first_only is byte-for-byte parity): ``first_only`` returns EXACTLY the single citation
         ``render.build_digest`` surfaces today (``cited_provisions[0]``) — no more, no less. Flipping the strategy
         knob to its default can never change or drop what the customer sees. signal_loss_rate == 0.
  INV-2 (additive recovery, never suppression): ``all_direct`` returns the SAME primary plus the previously-dropped
         extras — it recovers 100% of the distinct resolvable provisions and can NEVER lose the primary.
  INV-3 (case-law grounding parity): the ONLY citation ever withheld is an UNGROUNDED case-law cite (parity with
         render.py's grounding guard); a statute/EU/decree cite is ALWAYS kept. Dropping an ungrounded apex chip
         never hides the finding or its primary citation.
  INV-4 (determinism + idempotence): same input → same output; stable total sort (hierarchy depth, then id);
         re-expanding the result yields the same id-set.

The gold fixture (``applicability_gold.json``) has only {actor, modal, statement, confidence} per obligation — it
carries NO ``cited_provisions`` arrays and NO provision rows. So the citation cases (1-cite, multi-cite, an
ungrounded-case-law cite that must be withheld) are exercised with CONSTRUCTED inputs; see ``data_gaps``. The gold
rows are still used to confirm the eval scaffold runs over realistic statement/actor shapes.

Pure — no DB, no network, no LLM.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import get_args

import pytest

from regsuper.analysis.citation_precision import (
    CitationRef,
    CitationStrategy,
    expand_citations,
    recovery_rate,
    score,
    signal_loss_rate,
)

_STRATEGIES = list(get_args(CitationStrategy))


# ── a minimal, pure provision stub (mirrors what render.py reads off an ORM row, no DB) ──────────────────────
class _Prov:
    """A bare provision-row stub carrying only the plain attributes the expander reads.

    Mirrors how render.py reads already-loaded attributes (``id``, ``legal_hierarchy``,
    ``hierarchy_path``, plus the optionally-pre-hydrated ``source`` / ``source_external_id``).
    NO ORM, NO DB — these are the exact fields the pure expander consumes.
    """

    def __init__(
        self, id, *, legal_hierarchy=None, hierarchy_path=None, source=None, source_external_id=None
    ):
        self.id = id
        self.legal_hierarchy = legal_hierarchy
        self.hierarchy_path = hierarchy_path
        # Only set the case-law-marker attributes when the caller pre-hydrates them, so a plain statute stub has
        # NO ``source`` attribute at all (exactly like a real app Provision row, which defines no source column).
        if source is not None:
            self.source = source
        if source_external_id is not None:
            self.source_external_id = source_external_id


def _by_id(*provisions) -> dict:
    """Build the ``provisions_by_id`` lookup the way render.build_digest does (stringified id keys)."""
    return {str(p.id): p for p in provisions}


# A small statute corpus: three grounded app-corpus provisions at varying hierarchy depth.
_P_ACT = _Prov("act-1", legal_hierarchy="cz-act", hierarchy_path="act")  # depth 1
_P_PARA = _Prov("para-7", legal_hierarchy="cz-act", hierarchy_path="act.part.chapter.s7")  # depth 4
_P_EU = _Prov("eu-20", legal_hierarchy="eu-directive", hierarchy_path="dir.art20")  # depth 2
_STATUTE_BY_ID = _by_id(_P_ACT, _P_PARA, _P_EU)


# ── CitationRef is a small frozen dataclass ─────────────────────────────────────────────────────────────────
def test_citation_ref_is_frozen():
    ref = CitationRef(provision_id="x", hierarchy_path=None, is_primary=True)
    assert (ref.provision_id, ref.hierarchy_path, ref.is_primary) == ("x", None, True)
    with pytest.raises(FrozenInstanceError):
        ref.provision_id = "y"  # type: ignore[misc]


# ── INV-1: first_only is byte-for-byte parity with render.build_digest (cited[0]) ───────────────────────────
def test_first_only_single_cite():
    refs = expand_citations(["act-1"], _STATUTE_BY_ID, "first_only")
    assert len(refs) == 1
    assert refs[0] == CitationRef(provision_id="act-1", hierarchy_path=None, is_primary=True)


def test_first_only_multi_cite_returns_exactly_the_first():
    """The whole WF-3 motivation: today only cited[0] surfaces. first_only reproduces that EXACTLY."""
    refs = expand_citations(["para-7", "act-1", "eu-20"], _STATUTE_BY_ID, "first_only")
    assert len(refs) == 1
    assert refs[0].provision_id == "para-7"  # cited[0], NOT sorted/reordered
    assert refs[0].is_primary is True
    assert refs[0].hierarchy_path is None  # first_only makes no hierarchy claim


def test_first_only_default_strategy():
    """The default strategy is first_only (the parity/no-op default)."""
    assert expand_citations(["act-1", "eu-20"], _STATUTE_BY_ID) == expand_citations(
        ["act-1", "eu-20"], _STATUTE_BY_ID, "first_only"
    )


def test_first_only_unresolvable_head_returns_empty_like_render_skip():
    """render.build_digest SKIPS a finding whose cited[0] is absent — first_only must NOT promote a later cite."""
    refs = expand_citations(["missing", "act-1"], _STATUTE_BY_ID, "first_only")
    assert refs == []  # the finding would be skipped by render; we don't invent a primary


def test_first_only_no_resolvable_returns_empty():
    assert expand_citations(["nope-1", "nope-2"], _STATUTE_BY_ID, "first_only") == []
    assert expand_citations([], _STATUTE_BY_ID, "first_only") == []


# ── INV-2: all_direct recovers every previously-dropped citation (strictly additive) ────────────────────────
def test_all_direct_recovers_all_resolvable():
    refs = expand_citations(["para-7", "act-1", "eu-20"], _STATUTE_BY_ID, "all_direct")
    assert {r.provision_id for r in refs} == {"para-7", "act-1", "eu-20"}  # 100% recovered


def test_all_direct_superset_of_first_only():
    """all_direct is strictly additive: its id-set always CONTAINS the first_only primary."""
    cited = ["para-7", "act-1", "eu-20"]
    primary = expand_citations(cited, _STATUTE_BY_ID, "first_only")[0].provision_id
    all_ids = {r.provision_id for r in expand_citations(cited, _STATUTE_BY_ID, "all_direct")}
    assert primary in all_ids


def test_all_direct_marks_exactly_one_primary_and_it_is_the_head():
    refs = expand_citations(["para-7", "act-1", "eu-20"], _STATUTE_BY_ID, "all_direct")
    primaries = [r for r in refs if r.is_primary]
    assert len(primaries) == 1
    assert primaries[0].provision_id == "para-7"  # the first surviving resolution = cited[0]


def test_all_direct_skips_only_unresolvable_not_the_finding():
    """An id absent from the lookup is just not resolvable — the OTHER cites (incl. the primary) still surface."""
    refs = expand_citations(["act-1", "ghost", "eu-20"], _STATUTE_BY_ID, "all_direct")
    assert {r.provision_id for r in refs} == {"act-1", "eu-20"}
    assert any(r.is_primary and r.provision_id == "act-1" for r in refs)


def test_all_direct_hierarchy_path_is_none():
    """all_direct (unlike all_with_hierarchy) leaves hierarchy_path None."""
    refs = expand_citations(["act-1", "eu-20"], _STATUTE_BY_ID, "all_direct")
    assert all(r.hierarchy_path is None for r in refs)


# ── all_with_hierarchy: same id-set as all_direct, plus the hierarchy path annotation ───────────────────────
def test_all_with_hierarchy_same_ids_as_all_direct():
    cited = ["para-7", "act-1", "eu-20"]
    direct = {r.provision_id for r in expand_citations(cited, _STATUTE_BY_ID, "all_direct")}
    hier = {r.provision_id for r in expand_citations(cited, _STATUTE_BY_ID, "all_with_hierarchy")}
    assert direct == hier


def test_all_with_hierarchy_annotates_path():
    refs = expand_citations(["act-1", "para-7"], _STATUTE_BY_ID, "all_with_hierarchy")
    by = {r.provision_id: r for r in refs}
    assert by["act-1"].hierarchy_path == "act"
    assert by["para-7"].hierarchy_path == "act.part.chapter.s7"


def test_all_with_hierarchy_missing_path_is_none():
    p = _Prov("no-path", legal_hierarchy="cz-act")  # no hierarchy_path
    refs = expand_citations(["no-path"], _by_id(p), "all_with_hierarchy")
    assert refs[0].hierarchy_path is None


# ── INV-3: case-law grounding parity (the ONLY citation ever withheld) ──────────────────────────────────────
_P_STATUTE = _Prov("s-1", legal_hierarchy="cz-act", hierarchy_path="act.s1")
_P_CASE_UNGROUNDED = _Prov("c-1", legal_hierarchy="CASE_LAW", hierarchy_path="case.1")  # no ext id
_P_CASE_GROUNDED = _Prov(
    "c-2", legal_hierarchy="CASE_LAW", hierarchy_path="case.2", source_external_id="1167/2021"
)
_P_COURT_PROVENANCE = _Prov(
    "c-3", legal_hierarchy="cz-act", hierarchy_path="x", source="court"
)  # provenance-only, no ext id


@pytest.mark.parametrize("strategy", ["first_only", "all_direct", "all_with_hierarchy"])
def test_ungrounded_case_law_at_index_gt0_drops_whole_finding(strategy):
    """RENDER PARITY (review WF-3 CRITICAL): render.build_digest drops the WHOLE finding when ANY cited
    provision is an ungrounded case-law chip — even at index>0 riding on a grounded statute at index 0. So
    expand_citations returns [] for EVERY strategy (it must never surface a finding/primary render would drop)."""
    by = _by_id(
        _P_STATUTE, _P_CASE_UNGROUNDED
    )  # cited[0]=grounded statute, cited[1]=ungrounded case law
    assert expand_citations(["s-1", "c-1"], by, strategy) == []


def test_statute_is_always_grounded_never_dropped():
    """A statute/EU/decree cite is ALWAYS grounded — even with no source_external_id, it is never withheld."""
    refs = expand_citations(
        ["act-1", "eu-20"], _STATUTE_BY_ID, "all_direct"
    )  # neither has an ext id
    assert {r.provision_id for r in refs} == {"act-1", "eu-20"}


def test_grounded_case_law_is_kept():
    by = _by_id(_P_STATUTE, _P_CASE_GROUNDED)
    refs = expand_citations(["s-1", "c-2"], by, "all_direct")
    assert {r.provision_id for r in refs} == {"s-1", "c-2"}


def test_court_provenance_ungrounded_drops_whole_finding():
    """The provenance signal (source=='court') marks case law even when legal_hierarchy isn't CASE_LAW; an
    ungrounded such cite drops the whole finding (render parity) → []."""
    by = _by_id(_P_STATUTE, _P_COURT_PROVENANCE)
    assert expand_citations(["s-1", "c-3"], by, "all_direct") == []


@pytest.mark.parametrize("strategy", ["first_only", "all_direct", "all_with_hierarchy"])
def test_ungrounded_case_law_head_drops_finding_for_every_strategy(strategy):
    """cited[0] is an ungrounded case-law chip → render drops the finding → expand_citations returns [] and
    NEVER promotes a later survivor to primary (that would surface a finding render dropped)."""
    by = _by_id(_P_CASE_UNGROUNDED, _P_STATUTE)
    assert expand_citations(["c-1", "s-1"], by, strategy) == []


# ── case-law marker normalization parity (hyphen/underscore/case variants) ──────────────────────────────────
@pytest.mark.parametrize(
    "marker", ["CASE_LAW", "case-law", "case law", " CASE_LAW ", "caselaw", "cz-case-law"]
)
def test_case_law_marker_variants_all_treated_as_case_law(marker):
    p = _Prov("v", legal_hierarchy=marker, hierarchy_path="x")  # ungrounded (no ext id)
    refs = expand_citations(["v"], _by_id(p), "all_direct")
    assert refs == []  # recognised as case law → ungrounded → withheld


@pytest.mark.parametrize(
    "marker",
    ["cz-act", "eu-regulation", "eu-directive", "cz-decree", "cnb-vyhlaska", "cz-decision"],
)
def test_non_case_law_markers_never_withheld(marker):
    """Every app/regsuper statute/EU/decree/admin value (incl. the ambiguous 'cz-decision') is always grounded."""
    p = _Prov("v", legal_hierarchy=marker, hierarchy_path="x")  # no ext id
    refs = expand_citations(["v"], _by_id(p), "all_direct")
    assert {r.provision_id for r in refs} == {"v"}


# ── dedup: a duplicate id collapses to one chip (never drops a distinct duty) ────────────────────────────────
def test_duplicate_ids_collapse_to_one():
    refs = expand_citations(["act-1", "act-1", "eu-20"], _STATUTE_BY_ID, "all_direct")
    assert sorted(r.provision_id for r in refs) == ["act-1", "eu-20"]
    assert sum(1 for r in refs if r.is_primary) == 1


# ── id stringification parity (int / UUID-like ids resolve like render.build_digest) ────────────────────────
def test_int_ids_are_stringified_for_lookup():
    p = _Prov(7, legal_hierarchy="cz-act", hierarchy_path="act.s7")  # int id; lookup key is "7"
    refs = expand_citations([7], _by_id(p), "first_only")  # cited element is the int 7
    assert refs[0].provision_id == "7"


# ── INV-4: determinism + stable total sort + idempotence ────────────────────────────────────────────────────
def test_stable_total_sort_by_depth_then_id():
    """all_* sorts by (hierarchy depth, id): act (depth1) < eu-20 (depth2) < para-7 (depth4)."""
    refs = expand_citations(["para-7", "eu-20", "act-1"], _STATUTE_BY_ID, "all_direct")
    assert [r.provision_id for r in refs] == ["act-1", "eu-20", "para-7"]


def test_sort_id_tiebreak_at_equal_depth():
    a = _Prov("zzz", legal_hierarchy="cz-act", hierarchy_path="a.b")  # depth 2
    b = _Prov("aaa", legal_hierarchy="cz-act", hierarchy_path="c.d")  # depth 2
    refs = expand_citations(["zzz", "aaa"], _by_id(a, b), "all_direct")
    assert [r.provision_id for r in refs] == ["aaa", "zzz"]  # id tiebreak at equal depth


def test_output_independent_of_input_order():
    """Determinism: permuting the cited order yields the SAME sorted output (total order)."""
    out1 = expand_citations(["para-7", "act-1", "eu-20"], _STATUTE_BY_ID, "all_direct")
    out2 = expand_citations(["eu-20", "para-7", "act-1"], _STATUTE_BY_ID, "all_direct")
    assert [r.provision_id for r in out1] == [r.provision_id for r in out2]


def test_idempotent_in_id_set():
    """Re-expanding the resulting ids over the same lookup yields the same id-set (idempotence)."""
    cited = ["para-7", "act-1", "eu-20"]
    once = [r.provision_id for r in expand_citations(cited, _STATUTE_BY_ID, "all_direct")]
    twice = [r.provision_id for r in expand_citations(once, _STATUTE_BY_ID, "all_direct")]
    assert once == twice


def test_repeated_calls_byte_identical():
    cited = ["para-7", "act-1", "eu-20"]
    assert expand_citations(cited, _STATUTE_BY_ID, "all_with_hierarchy") == expand_citations(
        cited, _STATUTE_BY_ID, "all_with_hierarchy"
    )


# ── property-style sweep: across ALL strategies, the finding is NEVER suppressed ────────────────────────────
@pytest.mark.parametrize("strategy", _STRATEGIES)
def test_property_never_suppresses_resolvable_primary(strategy):
    """Across every strategy and a range of cited lists, whenever first_only surfaces a primary, the chosen
    strategy ALSO surfaces that exact primary id (the cardinal no-suppression guarantee)."""
    cited_lists = [
        ["act-1"],
        ["para-7", "act-1", "eu-20"],
        ["eu-20", "para-7"],
        ["act-1", "act-1"],
        ["missing", "act-1", "eu-20"],
    ]
    for cited in cited_lists:
        primary = expand_citations(cited, _STATUTE_BY_ID, "first_only")
        if not primary:
            continue
        got = {r.provision_id for r in expand_citations(cited, _STATUTE_BY_ID, strategy)}
        assert primary[0].provision_id in got, (
            f"{strategy} dropped today's primary {primary[0].provision_id} for cited={cited}"
        )


@pytest.mark.parametrize("strategy", _STRATEGIES)
def test_property_all_returned_ids_are_resolvable_and_grounded(strategy):
    """No strategy ever invents a chip: every returned id is a real, grounded key in the lookup."""
    by = _by_id(_P_STATUTE, _P_CASE_UNGROUNDED, _P_CASE_GROUNDED, _P_ACT)
    cited = ["s-1", "c-1", "c-2", "act-1", "ghost"]
    for r in expand_citations(cited, by, strategy):
        assert r.provision_id in by
        from regsuper.analysis.citation_precision import _case_law_grounded

        assert _case_law_grounded(by[r.provision_id])


# ── the eval / scorers (signal_loss_rate == 0 for first_only; all_direct recovers 100%) ──────────────────────
def _constructed_findings() -> list[dict]:
    """A representative finding set the spec names: a 1-cite, a multi-cite, and one with an ungrounded case-law
    cite that must be withheld. Built here because the gold fixture carries NO cited_provisions (see data_gaps)."""
    return [
        {"cited_provisions": ["act-1"]},  # 1-cite (single grounded statute)
        {
            "cited_provisions": ["para-7", "act-1", "eu-20"]
        },  # multi-cite (3 grounded — 2 dropped today)
        {"cited_provisions": ["s-1", "c-1"]},  # statute + ungrounded case-law (withheld)
        {"cited_provisions": ["s-1", "c-2"]},  # statute + GROUNDED case-law (kept)
    ]


_EVAL_BY_ID = _by_id(_P_ACT, _P_PARA, _P_EU, _P_STATUTE, _P_CASE_UNGROUNDED, _P_CASE_GROUNDED)


def test_signal_loss_rate_zero_for_first_only():
    findings = _constructed_findings()
    assert signal_loss_rate(findings, _EVAL_BY_ID, "first_only") == 0.0


@pytest.mark.parametrize("strategy", _STRATEGIES)
def test_signal_loss_rate_zero_for_every_strategy(strategy):
    """THE cardinal WF-3 metric: no strategy ever loses today's surfaced primary."""
    findings = _constructed_findings()
    assert signal_loss_rate(findings, _EVAL_BY_ID, strategy) == 0.0


def test_all_direct_recovers_100_percent():
    findings = _constructed_findings()
    assert recovery_rate(findings, _EVAL_BY_ID, "all_direct") == 1.0
    assert recovery_rate(findings, _EVAL_BY_ID, "all_with_hierarchy") == 1.0


def test_first_only_recovery_below_one_when_multi_cite_exists():
    """first_only loses the extras of a multi-cite finding — recovery is strictly < 1.0 (the signal it drops)."""
    findings = _constructed_findings()
    assert recovery_rate(findings, _EVAL_BY_ID, "first_only") < 1.0


def test_score_acceptance_passes():
    report = score(_constructed_findings(), _EVAL_BY_ID)
    assert report["first_only"]["signal_loss_rate"] == 0.0
    assert report["all_direct"]["signal_loss_rate"] == 0.0
    assert report["all_with_hierarchy"]["signal_loss_rate"] == 0.0
    assert report["all_direct"]["recovery_rate"] == 1.0
    assert report["ac_pass"] is True


def test_score_empty_is_vacuously_safe():
    report = score([], _EVAL_BY_ID)
    assert report["ac_pass"] is True
    assert report["all_direct"]["recovery_rate"] == 1.0


# ── the gold fixture: confirm the eval scaffold runs over realistic shapes (no cited_provisions → no-op) ─────
def test_eval_over_gold_shapes_is_a_safe_noop():
    """The gold rows carry NO cited_provisions, so every strategy yields [] per finding — a safe no-op with
    signal_loss_rate == 0 and (vacuous) recovery == 1.0. Confirms the harness tolerates the real fixture shape."""
    import json
    from pathlib import Path

    gold = json.loads(
        (
            Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "applicability_gold.json"
        ).read_text(encoding="utf-8")
    )
    findings = [
        {"cited_provisions": (f.get("payload") or {}).get("cited_provisions") or []}
        for f in gold["findings"]
    ]
    report = score(findings, _EVAL_BY_ID)
    assert report["n_findings"] == 79
    assert report["ac_pass"] is True
    for strat in _STRATEGIES:
        assert report[strat]["signal_loss_rate"] == 0.0
