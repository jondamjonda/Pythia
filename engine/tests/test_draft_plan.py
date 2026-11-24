"""T8 (Answer-Quality Part II) — the MP-C↔MP-D DRAFT-PLAN seam: planner + formatter.

Pins the additive, default-inert typed boundary:
  • plan_from_sections builds the expected typed DraftPlan (kind/heading/range/citation_map copied verbatim +
    the derived ordered-unique seed_provision_ids) from a real split_into_sections output.
  • THE CENTRAL DELIVERABLE — the round-trip equivalence: sections_from_plan(plan_from_sections(secs), draft)
    reproduces `secs` BYTE-FOR-BYTE (kind/heading/text/range/citations_map), the safety proof that the new
    seam changes nothing.
  • empty / degenerate (marker-less) draft ⇒ empty plan, no raise; and the formatter is total on a malformed
    slot range.
  • the flag-OFF guard: answer()'s audit_trace has NO 'draft_plan' key when draft_planner_enabled is OFF, and
    the plan is a pure function of the sections that never touches the served draft/sections/citations_map.
"""

from __future__ import annotations

import pythia.reasoning.answer as ans
from pythia.config import PythiaSettings
from pythia.contract import CitedProvision, DraftPlan, DraftSlot, Section
from pythia.reasoning.draft_plan import (
    DRAFT_FORMAT_VERSION,
    SLOT_HEADINGS,
    plan_from_sections,
    sections_from_plan,
)
from pythia.reasoning.sections import _HEADINGS, split_into_sections
from pythia.reasoning.verify import verify

# ── shared fixtures (mirror tests/test_sections.py so we plan over REAL splitter output) ──────────────────
_STATUTE = [
    CitedProvision(provision_id="p1", label="§ 297", legal_hierarchy="STATUTE"),
    CitedProvision(provision_id="p3", label="§ 318", legal_hierarchy="STATUTE"),
]
_CASE_LAW = CitedProvision(provision_id="pc", label="§ 12", legal_hierarchy="CASE_LAW")
_RET_STATUTE = [("p1", "240/2013", "§ 297"), ("p3", "240/2013", "§ 318")]
_RET_MIXED = [("p1", "240/2013", "§ 297"), ("pc", "5/2019", "§ 12")]

_DRAFT_STATUTE = (
    "## Legislativní rámec\n"
    "The manager must notify [240/2013 § 297] and keep records [240/2013 § 318].\n"
    "## Hodnocení\n"
    "Both duties are mandatory and continuous."
)
_DRAFT_MIXED = (
    "## Legislativní rámec\n"
    "The manager must notify [240/2013 § 297].\n"
    "## Judikatura\n"
    "The court confirmed this duty in [5/2019 § 12].\n"
    "## Hodnocení\n"
    "The obligation is strict."
)


def _sections(draft, ret, cps):
    v = verify(draft, ret)
    assert v.verdict == "ok", (draft, v)
    return split_into_sections(draft, v.citations_map, cps)


# ── 0) VL-C-02 DRAFT-GRAMMAR pins: format version + heading drift-pin on the DraftPlan seam ────────────────
def test_draft_format_version_pinned():
    # bump when the draft grammar changes; the drift-pin (below) enforces it.
    assert DRAFT_FORMAT_VERSION == "atoms-v1"


def test_slot_headings_match_sections_source_of_truth():
    # SLOT_HEADINGS MUST equal the {kind: canonical} projection of sections._HEADINGS (the single source of
    # slot headings). Any future heading/grammar edit in sections.py that changes a heading WITHOUT bumping
    # DRAFT_FORMAT_VERSION makes this assertion fail CI (the drift-pin).
    expected = {kind: canonical for _alt, kind, canonical in _HEADINGS}
    assert SLOT_HEADINGS == expected
    # every draft-grammar kind is represented exactly once (no kind silently dropped by the projection)
    assert list(SLOT_HEADINGS) == [kind for _alt, kind, _canonical in _HEADINGS]


# ── 1) planner produces the expected typed plan from a real structured draft ──────────────────────────────
def test_plan_from_sections_expected_shape():
    secs = _sections(_DRAFT_MIXED, _RET_MIXED, [_STATUTE[0], _CASE_LAW])
    plan = plan_from_sections(secs)
    assert isinstance(plan, DraftPlan)
    assert all(isinstance(s, DraftSlot) for s in plan.slots)
    assert [s.kind for s in plan.slots] == ["legislative", "case_law", "assessment"]
    assert [s.heading for s in plan.slots] == ["Legislativní rámec", "Judikatura", "Hodnocení"]
    # range + chips copied verbatim from the sections
    for slot, sec in zip(plan.slots, secs):
        assert slot.range == sec.range
        assert slot.citation_map == sec.citations_map
    # seed_provision_ids lifted from each slot's chips (ordered-unique)
    assert plan.slots[0].seed_provision_ids == ["p1"]
    assert plan.slots[1].seed_provision_ids == ["pc"]
    assert plan.slots[2].seed_provision_ids == []


def test_seed_provision_ids_are_ordered_unique():
    # two chips citing the SAME provision + one citing another → ordered-unique, first-seen order
    secs = [
        Section(
            kind="legislative",
            heading="Legislativní rámec",
            text="x",
            range=[0, 1],
            citations_map=[
                {"provision_id": "b", "start": 0, "end": 1},
                {"provision_id": "a", "start": 1, "end": 2},
                {"provision_id": "b", "start": 2, "end": 3},
            ],
        )
    ]
    plan = plan_from_sections(secs)
    assert plan.slots[0].seed_provision_ids == ["b", "a"]


# ── 2) THE CENTRAL DELIVERABLE — round-trip equivalence: formatter∘planner == today's sections ────────────
def _assert_roundtrip(secs, draft):
    rebuilt = sections_from_plan(plan_from_sections(secs), draft)
    assert len(rebuilt) == len(secs)
    for a, b in zip(rebuilt, secs):
        # BYTE-FOR-BYTE on every Section field the sectioned contract exposes
        assert a.kind == b.kind
        assert a.heading == b.heading
        assert a.text == b.text
        assert a.range == b.range
        assert a.citations_map == b.citations_map
    # equal as pydantic models too (full structural equality)
    assert rebuilt == secs


def test_roundtrip_mixed_byte_for_byte():
    secs = _sections(_DRAFT_MIXED, _RET_MIXED, [_STATUTE[0], _CASE_LAW])
    _assert_roundtrip(secs, _DRAFT_MIXED)


def test_roundtrip_statute_only_byte_for_byte():
    secs = _sections(_DRAFT_STATUTE, _RET_STATUTE, _STATUTE)
    _assert_roundtrip(secs, _DRAFT_STATUTE)


def test_roundtrip_reslices_text_from_range():
    # the formatter re-derives text purely from draft[range]; prove it matches draft[range] exactly
    secs = _sections(_DRAFT_MIXED, _RET_MIXED, [_STATUTE[0], _CASE_LAW])
    rebuilt = sections_from_plan(plan_from_sections(secs), _DRAFT_MIXED)
    for sec in rebuilt:
        assert sec.text == _DRAFT_MIXED[sec.range[0] : sec.range[1]]


# ── 3) empty / degenerate draft ⇒ empty plan, no raise; formatter total on bad range ──────────────────────
def test_empty_sections_yield_empty_plan():
    assert plan_from_sections([]).slots == []
    assert plan_from_sections(None).slots == []


def test_markerless_draft_roundtrips_as_empty():
    # a draft with no '## heading' markers → split_into_sections == [] → plan empty → formatter []
    draft = "Just prose with no section markers at all."
    secs = split_into_sections(draft, [], [])
    assert secs == []
    assert plan_from_sections(secs).slots == []
    assert sections_from_plan(plan_from_sections(secs), draft) == []


def test_formatter_total_on_none_and_bad_range():
    assert sections_from_plan(None, "abc") == []
    assert sections_from_plan(DraftPlan(slots=[]), "abc") == []
    # a malformed range must not raise — the slot yields empty text
    bad = DraftPlan(slots=[DraftSlot(kind="legislative", heading="H", range=[5, 2], citation_map=[])])
    out = sections_from_plan(bad, "abc")
    assert out[0].text == ""
    out2 = sections_from_plan(
        DraftPlan(slots=[DraftSlot(kind="legislative", heading="H", range=[0, 99], citation_map=[])]), "abc"
    )
    assert out2[0].text == ""  # out-of-bounds end → empty (never raises / slices past end)


# ── 4) flag-OFF guard: default settings never build/attach the plan; planner is pure over sections ────────
def test_flag_defaults_off():
    assert PythiaSettings().draft_planner_enabled is False


def test_planner_does_not_mutate_sections():
    # planning is a read-only view: the source sections + their chips are untouched (no aliasing surprises)
    secs = _sections(_DRAFT_MIXED, _RET_MIXED, [_STATUTE[0], _CASE_LAW])
    before = [(s.kind, s.heading, s.text, list(s.range), [dict(c) for c in s.citations_map]) for s in secs]
    _ = plan_from_sections(secs)
    after = [(s.kind, s.heading, s.text, list(s.range), [dict(c) for c in s.citations_map]) for s in secs]
    assert before == after


# ── 5) WIRE guard through answer(): OFF ⇒ no 'draft_plan' key + byte-identical; ON ⇒ present + still identical ──
def _install_answer_harness(monkeypatch, settings):
    """Fully stub answer.py's LLM / engine / fetch so answer() runs with no LLM/DB/model (mirrors the
    claim-verify wire harness). The draft is a section-headed statute answer; the retrieved provision grounds
    the bracketed cite."""
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
            from pythia.contract import CitedProvision as _CP

            return type("R", (), {"cited_provisions": [_CP(provision_id="p1", label="§ 297", score=0.82)]})()

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


_Q = "Jaké povinnosti má depozitář podle § 297?"


async def test_wire_off_no_draft_plan_key(monkeypatch):
    """draft_planner_enabled=False (default) ⇒ NO audit_trace['draft_plan'] key (plan never built)."""
    s = PythiaSettings()
    assert s.draft_planner_enabled is False
    _install_answer_harness(monkeypatch, s)
    res = await ans.answer(_Q, method="cte")
    assert "draft_plan" not in res.audit_trace


async def test_wire_on_additive_and_byte_identical(monkeypatch):
    """ON ⇒ audit_trace['draft_plan'] present AND a lossless view of the served sections; drafted_answer /
    sections / citations_map / cited_provisions BYTE-IDENTICAL to the OFF run (the plan is a pure annotation)."""
    s_off = PythiaSettings()
    _install_answer_harness(monkeypatch, s_off)
    off = await ans.answer(_Q, method="cte")

    s_on = PythiaSettings(draft_planner_enabled=True)
    _install_answer_harness(monkeypatch, s_on)
    on = await ans.answer(_Q, method="cte")

    # additive key present ON / absent OFF
    assert "draft_plan" not in off.audit_trace
    assert "draft_plan" in on.audit_trace

    # the attached plan is a LOSSLESS view: sections_from_plan(plan) reproduces the served sections
    plan = DraftPlan.model_validate(on.audit_trace["draft_plan"])
    rebuilt = sections_from_plan(plan, on.drafted_answer)
    assert [x.model_dump() for x in rebuilt] == [x.model_dump() for x in on.sections]

    # BYTE-IDENTICAL served output (the plan touched nothing the consumer sees)
    assert on.drafted_answer == off.drafted_answer
    assert [x.model_dump() for x in on.sections] == [x.model_dump() for x in off.sections]
    assert on.citations_map == off.citations_map
    assert [c.model_dump() for c in on.cited_provisions] == [c.model_dump() for c in off.cited_provisions]
    # and the rest of audit_trace is identical apart from the single additive key
    assert {k: v for k, v in on.audit_trace.items() if k != "draft_plan"} == off.audit_trace
