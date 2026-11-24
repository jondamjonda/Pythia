"""W2 — MP-D atomize CONSUMES the T8 DraftPlan (contract C-plan).

Pins the additive, default-inert rewire that lets `claims.atomize` derive its OUTER (start,end,kind) tile
partition from the typed `DraftPlan` (plan.slots' range+kind) instead of re-parsing the rendered Section list.

THE CENTRAL DELIVERABLE — the plan-sourced tiling yields the SAME claims as the section-sourced tiling:

    atomize(draft, secs, cmap) == atomize(draft, secs, cmap, plan=plan_from_sections(secs))

because a DraftSlot copies each Section's `range`+`kind` verbatim (T8 round-trip), so `_tiles_from_plan`
produces element-for-element identical tiles and atomize (pure over the tiles) produces identical claims.

Also: plan=None ⇒ byte-identical to today; degenerate/empty plan safe; the answer-path flag-OFF wire guard.
"""

from __future__ import annotations

import pytest

import pythia.reasoning.answer as ans
from pythia.config import PythiaSettings
from pythia.contract import CitedProvision, DraftPlan, DraftSlot
from pythia.reasoning.claims import _tiles_from_plan, _tiles_from_sections, atomize
from pythia.reasoning.draft_plan import plan_from_sections
from pythia.reasoning.sections import split_into_sections
from pythia.reasoning.verify import verify as verify_answer

# ── fixtures: a MIXED (3-heading) draft + a STRUCTURED 7-slot draft, both split via the REAL splitter ──────
_DRAFT_MIXED = (
    "## Legislativní rámec\n"
    "Podnikatel musí ohlásit živnost podle § 6 odst. 1 č. 455/1991 Sb. [455/1991 § 6]. "
    "Oznámení podejte nejpozději do 15 dnů.\n"
    "## Judikatura\n"
    "Soud potvrdil tuto povinnost v [5/2019 § 12].\n"
    "## Hodnocení\n"
    "Ve vašem případě jde o volnou živnost."
)
_RET_MIXED = [("p1", "455/1991", "§ 6"), ("pc", "5/2019", "§ 12")]
_CPS_MIXED = [
    CitedProvision(provision_id="p1", label="§ 6", legal_hierarchy="STATUTE"),
    CitedProvision(provision_id="pc", label="§ 12", legal_hierarchy="CASE_LAW"),
]

# A STRUCTURED 7-slot draft (the B1b action-plan shape). split_into_sections still emits the essay kinds on the
# default path, but the equivalence claim holds for WHATEVER sections the splitter returns — the plan copies
# them verbatim. This second fixture exercises a richer multi-section partition.
_DRAFT_STRUCTURED = (
    "## Vaše situace\n"
    "Jste OSVČ ve volné živnosti.\n"
    "## Co se na vás vztahuje\n"
    "Vztahuje se na vás ohlašovací povinnost [455/1991 § 6].\n"
    "## Konkrétní povinnosti a lhůty\n"
    "Oznámení podejte do 15 dnů [455/1991 § 6].\n"
    "## Postup krok za krokem\n"
    "Vyplňte formulář a podejte na úřad [455/1991 § 6].\n"
    "## Kam se obrátit\n"
    "Obraťte se na živnostenský úřad [455/1991 § 6].\n"
    "## Sankce a rizika\n"
    "Za porušení lze uložit pokutu [455/1991 § 6].\n"
    "## Co korpus nepokrývá\n"
    "Ostatní otázky nejsou v korpusu."
)
_RET_STRUCTURED = [("p1", "455/1991", "§ 6")]
_CPS_STRUCTURED = [CitedProvision(provision_id="p1", label="§ 6", legal_hierarchy="STATUTE")]


def _split(draft, ret, cps):
    v = verify_answer(draft, ret)
    return split_into_sections(draft, v.citations_map, cps), v.citations_map


# ── 1) THE CENTRAL DELIVERABLE — plan-sourced atomize == section-sourced atomize (over both fixtures) ──────
def _assert_same_claims(a, b):
    assert len(a) == len(b), (len(a), len(b))
    for x, y in zip(a, b):
        # every observable field of a Claim must be identical — the plan only re-sources the OUTER tiling
        assert x.span == y.span
        assert x.text == y.text
        assert x.slot == y.slot
        assert x.type == y.type
        assert x.type_source == y.type_source
        assert x.normative == y.normative
        assert x.chips == y.chips
        assert x.id == y.id


@pytest.mark.parametrize(
    "draft,ret,cps",
    [
        (_DRAFT_MIXED, _RET_MIXED, _CPS_MIXED),
        (_DRAFT_STRUCTURED, _RET_STRUCTURED, _CPS_STRUCTURED),
    ],
)
def test_plan_sourced_atomize_equals_section_sourced(draft, ret, cps):
    secs, cmap = _split(draft, ret, cps)
    from_sections = atomize(draft, secs, cmap)  # today's path
    from_plan = atomize(draft, secs, cmap, plan=plan_from_sections(secs))  # W2 plan-sourced path
    _assert_same_claims(from_plan, from_sections)


def test_tiles_from_plan_equals_tiles_from_sections():
    # the load-bearing lemma: the two tile-builders return element-for-element identical partitions
    for draft, ret, cps in [
        (_DRAFT_MIXED, _RET_MIXED, _CPS_MIXED),
        (_DRAFT_STRUCTURED, _RET_STRUCTURED, _CPS_STRUCTURED),
    ]:
        secs, _cmap = _split(draft, ret, cps)
        assert _tiles_from_plan(draft, plan_from_sections(secs)) == _tiles_from_sections(draft, secs)


# ── 2) plan=None (default / flag OFF) ⇒ byte-identical to today ────────────────────────────────────────────
def test_plan_none_byte_identical():
    secs, cmap = _split(_DRAFT_MIXED, _RET_MIXED, _CPS_MIXED)
    default = atomize(_DRAFT_MIXED, secs, cmap)  # no plan kwarg
    explicit_none = atomize(_DRAFT_MIXED, secs, cmap, plan=None)
    _assert_same_claims(explicit_none, default)


# ── 3) degenerate / empty plans are safe ──────────────────────────────────────────────────────────────────
def test_empty_plan_tiles_whole_draft_as_answer():
    # an empty plan (no slots) degrades to one 'answer' tile spanning the whole draft — same as empty sections
    draft = "Nějaká prostá věta bez značek."
    empty_plan = DraftPlan(slots=[])
    assert _tiles_from_plan(draft, empty_plan) == [(0, len(draft), "answer")]
    # and atomize with the empty plan == atomize with empty sections (both degrade the same way)
    _assert_same_claims(atomize(draft, [], [], plan=empty_plan), atomize(draft, [], []))


def test_markerless_draft_plan_equals_sections():
    # a marker-less draft ⇒ split_into_sections == [] ⇒ plan empty ⇒ plan path == section path
    draft = "Jen text bez nadpisů."
    secs = split_into_sections(draft, [], [])
    assert secs == []
    _assert_same_claims(atomize(draft, secs, [], plan=plan_from_sections(secs)), atomize(draft, secs, []))


def test_plan_with_malformed_range_does_not_raise():
    # a slot with a malformed range is dropped (same validation as _tiles_from_sections) and gap-filled
    draft = "abcdef"
    bad = DraftPlan(
        slots=[
            DraftSlot(kind="legislative", heading="H", range=[5, 2], citation_map=[]),
            DraftSlot(kind="assessment", heading="H2", range=[0, 3], citation_map=[]),
        ]
    )
    tiles = _tiles_from_plan(draft, bad)
    # the [5,2] slot is invalid → dropped; [0,3] kept; the tail [3,6] gap-filled with 'answer' → total cover
    assert tiles[0] == (0, 3, "assessment")
    assert tiles[-1][1] == len(draft)
    # atomize must not raise and must tile the whole draft
    claims = atomize(draft, [], [], plan=bad)
    assert claims[0].span[0] == 0
    assert claims[-1].span[1] == len(draft)


def test_empty_draft_with_plan_returns_no_claims():
    assert atomize("", [], [], plan=plan_from_sections([])) == []


# ── 4) answer-path flag guard: OFF ⇒ the plan is never built/passed (whole claim path byte-identical) ──────
async def test_answer_path_flag_off_byte_identical(monkeypatch):
    """With draft_planner_enabled OFF (default) AND claim_verify ON (shadow), the served answer + the
    claim_ledger audit annotation are byte-identical to an equivalent run — the plan is never threaded in."""

    def _harness(settings):
        monkeypatch.setattr(ans, "get_settings", lambda: settings, raising=True)

        class _LLM:
            async def available(self):
                return True

            async def json(self, prompt, schema, *, tier="cheap", stage=None):
                if "planning step" in prompt:
                    return {"sub_queries": ["q"], "jurisdiction": ["CZ"], "matter_type": "x"}
                return {"unsupported": []}

            async def text(self, prompt, *, system=None, tier="generate", stage=None):
                return (
                    "## Legislativní rámec\nPodle [240/2013 § 297] platí povinnost. "
                    "Oznámení podejte do 15 dnů.\n## Hodnocení\nOK."
                )

        monkeypatch.setattr(ans, "get_llm", lambda tier: _LLM(), raising=True)

        class _Eng:
            async def retrieve(self, q, *, method="cte"):
                return type(
                    "R", (), {"cited_provisions": [CitedProvision(provision_id="p1", label="§ 297", score=0.82)]}
                )()

        monkeypatch.setattr(ans, "PythiaEngine", lambda: _Eng(), raising=True)
        _FULL = "Depozitář musí " + "X" * 900 + " oznámit do 15 dnů " + "Y" * 400

        async def _fetch_stub(ids, *, as_of=None, render_asof=False, ctx_window=False, terms=None, keep_full=False):
            m = {
                "id": "p1",
                "label": "§ 297",
                "ext": "240/2013",
                "title": "ZISIF",
                "jurisdiction": "CZ",
                "txt": _FULL[:700],
                "ctx_txt": _FULL[:700],
                "bindingness": "hard-law",
                "is_authoritative": True,
                "temporal_status": "in-force",
                "legal_hierarchy": "STATUTE",
                "pit_status": None,
                "vector_stale": None,
            }
            if keep_full:
                m["full_txt"] = _FULL
            return {"p1": m}

        monkeypatch.setattr(ans, "_fetch", _fetch_stub, raising=True)

    q = "Jaké povinnosti má depozitář podle § 297?"
    # claim-verify ON (shadow) but draft_planner OFF — the wire must not build/pass a plan
    _harness(PythiaSettings(claim_verify_enabled=True))
    a = await ans.answer(q, method="cte")
    _harness(PythiaSettings(claim_verify_enabled=True))
    b = await ans.answer(q, method="cte")
    assert a.drafted_answer == b.drafted_answer
    assert a.audit_trace.get("claim_ledger") == b.audit_trace.get("claim_ledger")
    assert "draft_plan" not in a.audit_trace  # planner OFF ⇒ no plan attached either
