"""VL-C-04 (WS5) — planner: unit tests for the PURE deterministic 7-slot slot planner.

Pins the router's contract:
  • INVARIANTS — all_ids ⊆ ordered, every ordered id in ≥1 slot, provisions never in situation/assessment/gaps.
  • GOLDEN ROUTING — each real analysis-role code (from pythia.ports.ROLES) + fact_type lands in the right slot.
  • LLM SUB-CONSUMER — a raising bridge degrades to the deterministic plan; a refining bridge may re-slot but
    can never drop a provision or inject a hallucinated id; OFF ⇒ the bridge is never invoked.
  • TOTAL — empty input + missing routing fields never raise.

The role codes used below are the REAL ones from pythia.ports.ROLES:
  1=definition_rule 2=obligation_rule 3=prohibition_rule 5=condition_rule 7=sanction_rule 8=scope_rule
  9=procedure_rule.
"""

from __future__ import annotations

import pytest

from pythia.ports import ROLES
from pythia.reasoning.planner import Plan, SlotPlan, plan_slots

# Sanity-anchor the golden tests to the REAL role map so a code re-number breaks here loudly, not silently.
assert ROLES[2] == "obligation_rule"
assert ROLES[7] == "sanction_rule"
assert ROLES[9] == "procedure_rule"
assert ROLES[8] == "scope_rule"

_FORBIDDEN = {"situation", "assessment", "gaps"}


def _assert_invariants(plan: Plan, ordered: list[str]) -> None:
    assert plan.all_ids() <= set(ordered)  # no id invented
    for pid in ordered:  # every retrieved id lands somewhere
        assert plan.slots_for(pid), f"{pid} routed to no slot"
    for sp in plan.slots:  # never a provision-forbidden slot
        assert sp.slot not in _FORBIDDEN


# ── 1. INVARIANTS over a mix of role'd + role-less + case-law provisions ─────────────────────────────────
def test_invariants_over_mixed_provisions() -> None:
    meta = {
        "o1": {"analysis_roles": [2], "legal_hierarchy": "STATUTE"},  # obligation
        "s1": {"analysis_roles": [7], "legal_hierarchy": "STATUTE"},  # sanction
        "bare": {},  # no roles, no hierarchy
        "cl": {"analysis_roles": [], "legal_hierarchy": "CASE_LAW"},  # case-law
        "multi": {"analysis_roles": [8, 2, 9], "legal_hierarchy": "STATUTE"},  # scope+obl+procedure
    }
    ordered = ["o1", "s1", "bare", "cl", "multi"]
    plan = plan_slots(meta, ordered)
    _assert_invariants(plan, ordered)
    # a role-less, hierarchy-less provision still lands via the legislative BASE
    assert "legislative" in plan.slots_for("bare")


# ── 2. GOLDEN routing per role / fact / hierarchy ────────────────────────────────────────────────────────
def test_golden_role_and_fact_routing() -> None:
    meta = {
        "obl": {"analysis_roles": [2], "legal_hierarchy": "STATUTE"},  # obligation_rule → obligations
        "san": {"analysis_roles": [7], "legal_hierarchy": "STATUTE"},  # sanction_rule   → risks
        "proc": {"analysis_roles": [9], "legal_hierarchy": "STATUTE"},  # procedure_rule  → steps
        "scope": {"analysis_roles": [8], "legal_hierarchy": "STATUTE"},  # scope_rule      → applies
        "case": {"analysis_roles": [], "legal_hierarchy": "CASE_LAW"},  # case-law        → case_law
        "fdl": {"analysis_roles": [], "legal_hierarchy": "STATUTE"},  # deadline fact   → obligations
        "faut": {"analysis_roles": [], "legal_hierarchy": "STATUTE"},  # authority fact  → where
    }
    ordered = ["obl", "san", "proc", "scope", "case", "fdl", "faut"]
    facts = {"fdl": ["deadline"], "faut": ["authority"]}
    plan = plan_slots(meta, ordered, facts=facts)

    assert "obligations" in plan.slots_for("obl")
    assert "risks" in plan.slots_for("san")
    assert "steps" in plan.slots_for("proc")
    assert "applies" in plan.slots_for("scope")
    assert "case_law" in plan.slots_for("case")
    assert "obligations" in plan.slots_for("fdl")
    assert "where" in plan.slots_for("faut")
    _assert_invariants(plan, ordered)


def test_definition_and_condition_route_to_applies_and_section_kind_penalties() -> None:
    meta = {
        "def": {"analysis_roles": [1], "legal_hierarchy": "STATUTE"},  # definition_rule → applies
        "cond": {"analysis_roles": [5], "legal_hierarchy": "STATUTE"},  # condition_rule  → applies
        "pen": {"analysis_roles": [], "section_kind": "penalties", "legal_hierarchy": "STATUTE"},  # → risks
    }
    ordered = ["def", "cond", "pen"]
    plan = plan_slots(meta, ordered)
    assert "applies" in plan.slots_for("def")
    assert "applies" in plan.slots_for("cond")
    assert "risks" in plan.slots_for("pen")
    _assert_invariants(plan, ordered)


def test_within_slot_order_is_retrieval_rank_and_deduped() -> None:
    # two obligations + one that carries obligation twice (dup role) — order preserved, no dup id.
    meta = {
        "a": {"analysis_roles": [2], "legal_hierarchy": "STATUTE"},
        "b": {"analysis_roles": [2, 2], "legal_hierarchy": "STATUTE"},
        "c": {"analysis_roles": [2], "legal_hierarchy": "STATUTE"},
    }
    ordered = ["c", "a", "b"]  # deliberately not sorted
    plan = plan_slots(meta, ordered)
    assert plan.ids_for("obligations") == ["c", "a", "b"]


# ── 3. LLM-FAILURE DEGRADE ───────────────────────────────────────────────────────────────────────────────
class _Settings:
    def __init__(self, on: bool) -> None:
        self.structured_plan_llm_enabled = on


def test_llm_raise_degrades_to_deterministic() -> None:
    meta = {"o1": {"analysis_roles": [2], "legal_hierarchy": "STATUTE"}, "cl": {"legal_hierarchy": "CASE_LAW"}}
    ordered = ["o1", "cl"]

    def _boom(prompt: str, schema: dict):
        raise RuntimeError("cheap-tier down")

    plan = plan_slots(meta, ordered, settings=_Settings(True), llm_json=_boom)
    # equal to the pure deterministic plan
    baseline = plan_slots(meta, ordered)
    assert plan.as_dict() == baseline.as_dict()
    _assert_invariants(plan, ordered)


def test_llm_none_reply_degrades() -> None:
    meta = {"o1": {"analysis_roles": [2], "legal_hierarchy": "STATUTE"}}
    ordered = ["o1"]
    plan = plan_slots(meta, ordered, settings=_Settings(True), llm_json=lambda p, s: None)
    assert plan.as_dict() == plan_slots(meta, ordered).as_dict()


# ── 4. LLM REFINE + GUARDRAILS ───────────────────────────────────────────────────────────────────────────
def test_llm_refine_drops_hallucination_and_readds_omitted() -> None:
    meta = {
        "p1": {"analysis_roles": [2], "legal_hierarchy": "STATUTE"},  # deterministic: obligations
        "p2": {"analysis_roles": [7], "legal_hierarchy": "STATUTE"},  # deterministic: risks
    }
    ordered = ["p1", "p2"]

    # LLM: moves p1 to steps, invents "GHOST", and OMITS p2 entirely.
    def _refine(prompt: str, schema: dict):
        return {"steps": ["p1", "GHOST"]}

    plan = plan_slots(meta, ordered, settings=_Settings(True), llm_json=_refine)
    # (a) hallucinated id dropped
    assert "GHOST" not in plan.all_ids()
    # p1 was re-slotted by the LLM to steps
    assert "steps" in plan.slots_for("p1")
    # (b) p2, omitted by the LLM, re-added to its deterministic slot (risks)
    assert "risks" in plan.slots_for("p2")
    _assert_invariants(plan, ordered)


def test_llm_forbidden_slot_in_reply_is_ignored() -> None:
    meta = {"p1": {"analysis_roles": [2], "legal_hierarchy": "STATUTE"}}
    ordered = ["p1"]

    def _refine(prompt: str, schema: dict):
        return {"gaps": ["p1"]}  # LLM tries to push a provision into a forbidden slot

    plan = plan_slots(meta, ordered, settings=_Settings(True), llm_json=_refine)
    assert "gaps" not in plan.as_dict()
    # p1 re-added to its deterministic obligations slot (never dropped)
    assert "obligations" in plan.slots_for("p1")
    _assert_invariants(plan, ordered)


# ── 5. OFF ⇒ the bridge is NEVER invoked ─────────────────────────────────────────────────────────────────
def test_off_flag_never_invokes_bridge() -> None:
    meta = {"o1": {"analysis_roles": [2], "legal_hierarchy": "STATUTE"}}
    ordered = ["o1"]
    called = {"hit": False}

    def _spy(prompt: str, schema: dict):
        called["hit"] = True
        return {"obligations": ["o1"]}

    # sub-flag OFF ⇒ no call
    plan_slots(meta, ordered, settings=_Settings(False), llm_json=_spy)
    assert called["hit"] is False

    # sub-flag ON but no bridge ⇒ no call (and no crash)
    called["hit"] = False
    plan_slots(meta, ordered, settings=_Settings(True), llm_json=None)
    assert called["hit"] is False


# ── 6. TOTAL — empty + missing fields never raise ────────────────────────────────────────────────────────
def test_empty_and_missing_fields_are_total() -> None:
    empty = plan_slots({}, [])
    assert isinstance(empty, Plan)
    assert empty.slots == ()
    assert empty.all_ids() == set()
    assert empty.ids_for("obligations") == []
    assert empty.slots_for("x") == []
    assert empty.as_dict() == {}

    # meta with a pid missing analysis_roles / section_kind / legal_hierarchy entirely
    meta = {"p1": {}, "p2": {"analysis_roles": None, "section_kind": None, "legal_hierarchy": None}}
    ordered = ["p1", "p2"]
    plan = plan_slots(meta, ordered)
    _assert_invariants(plan, ordered)
    # both fell to the legislative base
    assert plan.slots_for("p1") == ["legislative"]
    assert plan.slots_for("p2") == ["legislative"]


def test_slotplan_shape_is_frozen_tuple() -> None:
    plan = plan_slots({"p1": {"analysis_roles": [2], "legal_hierarchy": "STATUTE"}}, ["p1"])
    sp = plan.slots[0]
    assert isinstance(sp, SlotPlan)
    assert isinstance(sp.provision_ids, tuple)
    with pytest.raises(Exception):  # noqa: PT011 — frozen dataclass: any mutation attempt must fail
        sp.slot = "other"  # type: ignore[misc]
