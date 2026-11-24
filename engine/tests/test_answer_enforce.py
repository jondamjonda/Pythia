"""Track V / V2 = MP-E — the answer()-level ENFORCE pins (contracts C1/C2). Fast, forced-enforce settings,
fully-stubbed LLM/engine/fetch (no real LLM / DB). Covers:
  • shadow == OFF byte-identity of the served output (the free A/B stays clean);
  • enforce DEMOTES an ungrounded claim to a gap line + keeps verified claims verbatim;
  • enforce REFUSES iff all-core-failed / grounding < floor;
  • strict-retry-as-round-0 fires EXACTLY ONCE on grounded-nothing under enforce;
  • faithfulness is SKIPPED under enforce (called OFF/shadow-only);
  • the real enforce ledger carries final_status ∈ {verified, repaired_verified, demoted_gap, …}.
"""

from __future__ import annotations

import pytest

import pythia.reasoning.answer as ans
from pythia.config import PythiaSettings

pytestmark = pytest.mark.asyncio


def _harness(
    monkeypatch, settings, *, draft: str, full_text: str, gen_calls: dict | None = None, faith_calls: dict | None = None
):
    """Stub answer.py's LLM / engine / fetch. `draft` is what GENERATE returns; `full_text` is the cited
    provision's FULL text the verifiers check against (evidence['cited_texts'][pid])."""
    monkeypatch.setattr(ans, "get_settings", lambda: settings, raising=True)

    class _LLM:
        async def available(self):
            return True

        async def json(self, prompt, schema, *, tier="cheap", stage=None):
            if "planning step" in prompt:
                return {"sub_queries": ["q"], "jurisdiction": ["CZ"], "matter_type": "x"}
            if faith_calls is not None:
                faith_calls["n"] = faith_calls.get("n", 0) + 1
            return {"unsupported": []}

        async def text(self, prompt, *, system=None, tier="generate", stage=None):
            if gen_calls is not None:
                gen_calls[stage] = gen_calls.get(stage, 0) + 1
            return draft

    monkeypatch.setattr(ans, "get_llm", lambda tier: _LLM(), raising=True)

    class _Eng:
        async def retrieve(self, q, *, method="cte"):
            from pythia.contract import CitedProvision as _CP

            return type("R", (), {"cited_provisions": [_CP(provision_id="p1", label="§ 297", score=0.82)]})()

    monkeypatch.setattr(ans, "PythiaEngine", lambda: _Eng(), raising=True)

    async def _fetch_stub(ids, *, as_of=None, render_asof=False, ctx_window=False, terms=None, keep_full=False):
        m = {
            "id": "p1",
            "label": "§ 297",
            "ext": "240/2013",
            "title": "ZISIF",
            "jurisdiction": "CZ",
            "txt": full_text[:700],
            "ctx_txt": full_text[:700],
            "bindingness": "hard-law",
            "is_authoritative": True,
            "temporal_status": "in-force",
            "legal_hierarchy": "STATUTE",
            "pit_status": None,
            "vector_stale": None,
        }
        if keep_full:
            m["full_txt"] = full_text
        return {"p1": m}

    monkeypatch.setattr(ans, "_fetch", _fetch_stub, raising=True)


_Q = "Jaké povinnosti má depozitář podle § 297?"

# A draft with a grounded, well-supported rule sentence. The full text repeats the rule tokens so V-CIT's
# lexical-support score is high (pass).
_GOOD_FULL = (
    "Depozitář musí oznámit údaje podle § 297 do 15 dnů. "
    + "X" * 200
    + " Depozitář oznamuje údaje bez zbytečného odkladu."
)
_GOOD_DRAFT = "## Legislativní rámec\nDepozitář musí oznámit údaje podle [240/2013 § 297].\n## Hodnocení\nOK."


async def test_shadow_equals_off_served_output(monkeypatch):
    """SHADOW served output is byte-identical to OFF (loop is a pure audit annotation)."""
    _harness(monkeypatch, PythiaSettings(), draft=_GOOD_DRAFT, full_text=_GOOD_FULL)
    off = await ans.answer(_Q, method="cte")
    _harness(
        monkeypatch,
        PythiaSettings(claim_verify_enabled=True, claim_verify_mode="shadow"),
        draft=_GOOD_DRAFT,
        full_text=_GOOD_FULL,
    )
    sh = await ans.answer(_Q, method="cte")
    assert "claim_ledger" not in off.audit_trace
    assert sh.audit_trace["claim_ledger"]["summary"]["mode"] == "shadow"
    assert sh.drafted_answer == off.drafted_answer
    assert sh.citations_map == off.citations_map
    assert [c.model_dump() for c in sh.cited_provisions] == [c.model_dump() for c in off.cited_provisions]


async def test_enforce_keeps_verified_claim_verbatim(monkeypatch):
    """A grounded+supported rule survives enforce verbatim; the ledger records it verified/repaired_verified."""
    _harness(
        monkeypatch,
        PythiaSettings(claim_verify_enabled=True, claim_verify_mode="enforce"),
        draft=_GOOD_DRAFT,
        full_text=_GOOD_FULL,
    )
    res = await ans.answer(_Q, method="cte")
    led = res.audit_trace["claim_ledger"]
    assert led["summary"]["mode"] == "enforce"
    assert not res.refused
    # the rule sentence text survives in the served answer
    assert "Depozitář musí oznámit údaje podle" in res.drafted_answer
    finals = {r["final"] for r in led["rows"] if r["normative"]}
    assert finals <= {"verified", "repaired_verified", "demoted_gap", "dropped_safety", "dropped_unverifiable"}


# A draft whose ONLY normative claim is an UNCITED rule ⇒ V-CIT fails 'uncited_normative_claim' ⇒ demoted ⇒
# all core failed ⇒ refuse. (Uncited ⇒ verify_answer grounds nothing anyway, exercising the round-0 retry.)
_UNCITED_DRAFT = "## Legislativní rámec\nDepozitář musí vždy oznámit vše okamžitě bez výjimky.\n## Hodnocení\nOK."


async def test_enforce_round0_strict_retry_fires_once_on_grounded_nothing(monkeypatch):
    """Grounded-nothing under enforce ⇒ the strict retry runs EXACTLY ONCE as fix-loop round 0 (stamped)."""
    gen = {}
    _harness(
        monkeypatch,
        PythiaSettings(claim_verify_enabled=True, claim_verify_mode="enforce"),
        draft=_UNCITED_DRAFT,
        full_text=_GOOD_FULL,
        gen_calls=gen,
    )
    res = await ans.answer(_Q, method="cte")
    # exactly one strict regeneration on the grounded-nothing draft
    assert gen.get("generate_retry", 0) == 1
    # refused (no grounded citation at all) — the round-0 stamp is on the ledger when the loop ran; on a pure
    # grounded-nothing the answer refuses at verify pass-1 and the enforce assembly is not reached, so we assert
    # the retry fired (round-0 mechanism) and the answer is a refuse.
    assert res.refused


async def test_faithfulness_skipped_under_enforce_only(monkeypatch):
    """_check_faithfulness (the cheap json 'unsupported' call) runs OFF/shadow but is SKIPPED under enforce."""
    # SHADOW: faithfulness runs (verify='always' path reaches it on an ok verdict)
    fc_sh = {}
    _harness(
        monkeypatch,
        PythiaSettings(claim_verify_enabled=True, claim_verify_mode="shadow"),
        draft=_GOOD_DRAFT,
        full_text=_GOOD_FULL,
        faith_calls=fc_sh,
    )
    await ans.answer(_Q, method="cte", verify="always")
    # ENFORCE: faithfulness skipped
    fc_en = {}
    _harness(
        monkeypatch,
        PythiaSettings(claim_verify_enabled=True, claim_verify_mode="enforce"),
        draft=_GOOD_DRAFT,
        full_text=_GOOD_FULL,
        faith_calls=fc_en,
    )
    await ans.answer(_Q, method="cte", verify="always")
    assert fc_sh.get("n", 0) >= 1  # faithfulness json call happened in shadow
    assert fc_en.get("n", 0) == 0  # …and NOT under enforce (subsumed by V-CIT)


async def test_enforce_demotes_ungrounded_and_refuses_when_all_core_fail(monkeypatch):
    """A grounded-but-unsupported core rule ⇒ V-CIT fail ⇒ demote-to-gap ⇒ all core failed ⇒ refuse, with a
    non-citation-shaped gap line and the withheld claim text absent."""
    # The draft cites a real provision but the rule tokens do NOT appear in the full text ⇒ cited_but_unsupported
    unsupported_full = "Tento text hovoří o něčem úplně jiném. " + "Z" * 300
    bad_draft = (
        "## Legislativní rámec\n"
        "Depozitář musí zaplatit pokutu milion korun okamžitě [240/2013 § 297].\n"
        "## Hodnocení\nOK."
    )
    _harness(
        monkeypatch,
        PythiaSettings(claim_verify_enabled=True, claim_verify_mode="enforce"),
        draft=bad_draft,
        full_text=unsupported_full,
    )
    res = await ans.answer(_Q, method="cte")
    led = res.audit_trace["claim_ledger"]
    assert led["summary"]["mode"] == "enforce"
    # the unsupported core claim was demoted (or the answer refused because all core failed)
    assert res.refused or led["summary"].get("demoted", 0) >= 1


# ─────────────────────────── the VL-E-03 LLM prose-repair lane at answer() level (STUB redraft) ────────────
# A draft whose ONLY rule sentence is grounded (cites §297) but UNSUPPORTED by the full text ⇒ V-CIT fails ⇒
# without the LLM lane it demotes and (all core failed) the answer refuses. With the sub-flag ON and a STUB
# redraft that rewrites the sentence to the SUPPORTED form, the claim is REPAIRED and the answer is NOT refused.
_LANE_FULL = (
    "Depozitář musí oznámit údaje podle § 297 do 15 dnů. "
    + "X" * 200
    + " Depozitář oznamuje údaje bez zbytečného odkladu."
)
# The bad draft's rule sentence shares almost NO content tokens with _LANE_FULL ⇒ V-CIT support < 0.2 ⇒ fail.
_LANE_BAD_DRAFT = (
    "## Legislativní rámec\n"
    "Podnikatel zaplatí pokutu milion korun okamžitě hejtmanovi kraje [240/2013 § 297].\n"
    "## Hodnocení\nOK."
)
# The supported rewrite: its content tokens appear in _LANE_FULL (Depozitář/oznámit/údaje) and it cites the
# same in-Map tag ⇒ V-CIT support ≥ 0.5 ⇒ re-verify passes ⇒ the claim is repaired.
_LANE_FIX = "Depozitář musí oznámit údaje podle [240/2013 § 297]."


def _stub_redraft_batch_call(reply, *, calls=None):
    """A deterministic sync stub for _make_sync_redraft_batch_call — the batched bridge shape
    ((prompt, items) -> list[str|None]). Returns `reply` for EVERY item (or all-None when reply is None), and
    optionally counts the cheap calls (one per batch) into `calls['n']`. Never a live model."""

    def _factory(_llm):
        def _call(_prompt, items):
            if calls is not None:
                calls["n"] = calls.get("n", 0) + 1
            return [reply for _ in items]

        return _call

    return _factory


async def test_enforce_llm_lane_off_by_default_no_bridge(monkeypatch):
    """claim_repair_llm_enabled OFF (default) ⇒ the bridge is NEVER built (no redraft) — deterministic demote."""
    built = {"n": 0}
    monkeypatch.setattr(
        ans,
        "_make_sync_redraft_batch_call",
        lambda llm: built.__setitem__("n", built["n"] + 1) or (lambda p, it: [_LANE_FIX]),
        raising=True,
    )
    _harness(
        monkeypatch,
        PythiaSettings(claim_verify_enabled=True, claim_verify_mode="enforce"),
        draft=_LANE_BAD_DRAFT,
        full_text=_LANE_FULL,
    )
    res = await ans.answer(_Q, method="cte")
    assert built["n"] == 0  # the sub-flag is OFF ⇒ the bridge is never built
    led = res.audit_trace["claim_ledger"]
    assert led["summary"]["mode"] == "enforce"
    assert res.refused or led["summary"].get("demoted", 0) >= 1  # deterministic demote, as today


async def test_enforce_llm_lane_stub_fix_repairs_claim(monkeypatch):
    """Sub-flag ON + a STUB BATCHED redraft that returns the SUPPORTED sentence ⇒ the core claim is REPAIRED
    (kept in the answer, ledger repaired_verified), NOT demoted, the answer is NOT refused, and EXACTLY ONE
    cheap call was issued for the batch (port-spy)."""
    calls = {"n": 0}
    monkeypatch.setattr(
        ans, "_make_sync_redraft_batch_call", _stub_redraft_batch_call(_LANE_FIX, calls=calls), raising=True
    )
    _harness(
        monkeypatch,
        PythiaSettings(claim_verify_enabled=True, claim_verify_mode="enforce", claim_repair_llm_enabled=True),
        draft=_LANE_BAD_DRAFT,
        full_text=_LANE_FULL,
    )
    res = await ans.answer(_Q, method="cte")
    led = res.audit_trace["claim_ledger"]
    assert not res.refused
    assert led["summary"].get("repaired", 0) >= 1 or led["summary"].get("llm_repairs", 0) >= 1
    assert "repaired_verified" in {r["final"] for r in led["rows"] if r["normative"]}
    # the repaired (supported) sentence is what survives in the served answer
    assert "Depozitář musí oznámit údaje podle" in res.drafted_answer
    assert calls["n"] == 1  # ONE cheap call for the (single-item) batch


async def test_enforce_llm_lane_stub_none_falls_back_to_demote(monkeypatch):
    """Sub-flag ON but the STUB batched redraft returns None (fail-safe) ⇒ deterministic demote (lane OFF)."""
    monkeypatch.setattr(ans, "_make_sync_redraft_batch_call", _stub_redraft_batch_call(None), raising=True)
    _harness(
        monkeypatch,
        PythiaSettings(claim_verify_enabled=True, claim_verify_mode="enforce", claim_repair_llm_enabled=True),
        draft=_LANE_BAD_DRAFT,
        full_text=_LANE_FULL,
    )
    res = await ans.answer(_Q, method="cte")
    led = res.audit_trace["claim_ledger"]
    assert led["summary"].get("llm_repairs", 0) == 0
    assert res.refused or led["summary"].get("demoted", 0) >= 1


# ─────────────────────── VL-E-11 (WS5 step 39) — precedence guard + post-reassembly sidecar recompute ───────
# _GOOD_FULL carries a deadline ("15 dnů") in the CITED PROVISION text but the verified answer (_GOOD_DRAFT)
# does NOT state it. Under enforce the loop owns grounding, so the practical sidecar is rebuilt from the FINAL
# reassembled text: it carries NO slot_verify demotions and NO gaps of its own (single gap source = the draft),
# and the ⊆ filter DROPS the provision-only deadline (it is not in the served answer). The ⊆ assertion bites if
# the filter is removed: verify_slots would then surface the deadline item whose value is absent from the answer.

# A structured draft with an UNGROUNDED 'steps' claim slot (no citation in it) ⇒ the loop-OFF slot_verify path
# DEMOTES it. Used to pin that claim_verify OFF still runs the pre-loop demotion unchanged (Part-I).
_UNGROUNDED_STEPS_DRAFT = (
    "## Legislativní rámec\n"
    "Depozitář musí oznámit údaje podle [240/2013 § 297].\n"
    "## Postup krok za krokem\n"
    "Podejte oznámení na příslušný úřad.\n"
    "## Hodnocení\nOK."
)


async def test_enforce_structured_sidecar_recompute(monkeypatch):
    """Enforce+structured: the practical sidecar is rebuilt from the FINAL reassembled text — ZERO slot_verify
    demotions (the loop owns demotion), NO gaps of its own (single source = the draft), and every surfaced
    item's text is present in the served answer (`sidecar text ⊆ final answer text`; a provision-only fact is
    dropped)."""
    _harness(
        monkeypatch,
        PythiaSettings(claim_verify_enabled=True, claim_verify_mode="enforce", structured_answer_enabled=True),
        draft=_GOOD_DRAFT,
        full_text=_GOOD_FULL,
    )
    res = await ans.answer(_Q, method="cte")
    assert not res.refused
    pb = res.practical
    assert pb is not None
    assert pb.demoted_slots == []  # (1) zero slot_verify demotions under enforce
    assert pb.gaps == []  # (2) single gap source — the reassembled draft owns them
    # (3) ⊆ final text — a provision-only deadline is NOT surfaced; any item that IS surfaced is in the answer.
    assert all(it.value_raw and it.value_raw in res.drafted_answer for it in pb.items)


async def test_off_structured_slot_verify_demotes_pre_loop(monkeypatch):
    """Loop OFF (structured ON): the pre-loop slot_verify path is UNCHANGED — an ungrounded claim slot is still
    demoted (the precedence gate only defers under enforce, never on the OFF/shadow path)."""
    _harness(
        monkeypatch, PythiaSettings(structured_answer_enabled=True), draft=_UNGROUNDED_STEPS_DRAFT, full_text=_GOOD_FULL
    )
    res = await ans.answer(_Q, method="cte")
    pb = res.practical
    assert pb is not None
    assert "steps" in pb.demoted_slots  # verify_slots demoted the ungrounded 'steps' slot


# ─────────────────────── VL-C-04 (WS5 step 40) — the deterministic slot planner integrated ahead of GENERATE ──
async def test_structured_stamps_slot_plan_covering_all_ids(monkeypatch):
    """structured_answer_enabled ON ⇒ the slot plan is BUILT ahead of GENERATE + stamped on audit_trace, and it
    covers every retrieved id (∪ ⊆ ordered ∧ every id in ≥1 slot — the add-never-gate invariant end-to-end)."""
    _harness(monkeypatch, PythiaSettings(structured_answer_enabled=True), draft=_GOOD_DRAFT, full_text=_GOOD_FULL)
    res = await ans.answer(_Q, method="cte")
    sp = res.audit_trace.get("slot_plan")
    assert sp is not None  # ON ⇒ the plan is stamped
    all_ids = {i for ids in sp.values() for i in ids}
    assert all_ids <= {"p1"} and "p1" in all_ids  # ∪ ⊆ ordered ∧ every retrieved id in ≥1 slot


async def test_essay_off_has_no_slot_plan(monkeypatch):
    """structured OFF (the essay default) ⇒ the plan is NEVER built and no slot_plan key exists (byte-identical)."""
    _harness(monkeypatch, PythiaSettings(), draft=_GOOD_DRAFT, full_text=_GOOD_FULL)
    res = await ans.answer(_Q, method="cte")
    assert "slot_plan" not in res.audit_trace


async def test_planner_failure_cannot_kill_the_answer(monkeypatch):
    """A planner that RAISES ⇒ the guarded build degrades to slot_plan=None; the answer is served unchanged
    (planner-failure-can't-kill-answer — the annotate-only planner can never break the answer path)."""
    _harness(monkeypatch, PythiaSettings(structured_answer_enabled=True), draft=_GOOD_DRAFT, full_text=_GOOD_FULL)
    import pythia.reasoning.planner as _pl

    def _boom(*a, **k):
        raise RuntimeError("planner blew up")

    monkeypatch.setattr(_pl, "plan_slots", _boom, raising=True)
    res = await ans.answer(_Q, method="cte")
    assert not res.refused  # the answer still serves
    assert "slot_plan" not in res.audit_trace  # …with no plan (the failure degraded to None)


# ─────────────────────── VL-E-06 (WS5 step 46) — verify='always' ⇒ any-demoted-core ⇒ refuse ────────────────
# TWO core claims: sentence 1 (an obligation) is SUPPORTED by _GOOD_FULL and survives; sentence 2 (a sanction
# amount) is UNSUPPORTED ⇒ demoted. NOT all-core-fail (one survives), so the loop does NOT refuse on its own —
# only verify='always' (the stricter any-demoted-core rule) withholds the answer.
_MIXED_CORE_DRAFT = (
    "## Legislativní rámec\n"
    "Depozitář musí oznámit údaje podle [240/2013 § 297].\n"
    "Depozitář musí zaplatit pokutu deset milionů korun okamžitě [240/2013 § 297].\n"
    "## Hodnocení\nOK."
)


async def test_verify_always_refuses_on_a_single_demoted_core(monkeypatch):
    """VL-E-06: default verify ('gated') SERVES the answer when one core survives (the loop refuses only on
    all-core-fail); verify='always' is stricter — a SINGLE demoted core claim ⇒ ROUTE_REFUSE."""
    _harness(
        monkeypatch,
        PythiaSettings(claim_verify_enabled=True, claim_verify_mode="enforce"),
        draft=_MIXED_CORE_DRAFT,
        full_text=_GOOD_FULL,
    )
    res_default = await ans.answer(_Q, method="cte")  # verify='gated' (default)
    assert not res_default.refused  # one core survived ⇒ served
    _harness(
        monkeypatch,
        PythiaSettings(claim_verify_enabled=True, claim_verify_mode="enforce"),
        draft=_MIXED_CORE_DRAFT,
        full_text=_GOOD_FULL,
    )
    res_always = await ans.answer(_Q, method="cte", verify="always")
    assert res_always.refused and res_always.verdict == "refuse"  # a single demoted core ⇒ refuse


# ─────────────────────── VL-C-08 (WS5 step 43) — the 3 ON-only draft-provenance stamps + premise precedence ──
async def test_structured_stamps_draft_provenance_on_only(monkeypatch):
    """VL-C-08: draft_format{version,schema} + plan{source,slot_coverage} + gen_prompt_hash are stamped ON the
    structured path and ABSENT on the essay/OFF path (bytes pin)."""
    from pythia.reasoning.draft_plan import DRAFT_FORMAT_VERSION

    _harness(monkeypatch, PythiaSettings(structured_answer_enabled=True), draft=_GOOD_DRAFT, full_text=_GOOD_FULL)
    on = (await ans.answer(_Q, method="cte")).audit_trace
    assert on["draft_format"] == {"version": DRAFT_FORMAT_VERSION, "schema": "structured"}
    assert on["plan"]["source"] == "deterministic" and isinstance(on["plan"]["slot_coverage"], int)
    assert isinstance(on["gen_prompt_hash"], str) and len(on["gen_prompt_hash"]) == 64
    _harness(monkeypatch, PythiaSettings(), draft=_GOOD_DRAFT, full_text=_GOOD_FULL)
    off = (await ans.answer(_Q, method="cte")).audit_trace
    assert "draft_format" not in off and "plan" not in off and "gen_prompt_hash" not in off


async def test_false_premise_precedes_structured(monkeypatch):
    """VL-C-08: a false_premise route (metagate enforce) leads the correction-led GENERATE even when structured
    is ON — the premise prompt wins, the 7-slot structured prompt is NOT used."""
    from pythia.contract import CitedProvision as _CP
    from pythia.contract import MetaVerdict

    _harness(
        monkeypatch,
        PythiaSettings(metagate_enabled=True, metagate_mode="enforce", structured_answer_enabled=True),
        draft=_GOOD_DRAFT,
        full_text=_GOOD_FULL,
    )
    prompts: list[str] = []

    class _RecLLM:
        async def available(self):
            return True

        async def json(self, prompt, schema, *, tier="cheap", stage=None):
            if "planning step" in prompt:
                return {"sub_queries": ["q"], "jurisdiction": ["CZ"], "matter_type": "x"}
            return {"unsupported": []}

        async def text(self, prompt, *, system=None, tier="generate", stage=None):
            prompts.append(prompt)
            return _GOOD_DRAFT

    monkeypatch.setattr(ans, "get_llm", lambda tier: _RecLLM(), raising=True)
    fp = MetaVerdict(
        route="false_premise",
        premise="false_premise",
        assumed_claim="X je vždy povinné",
        correction="X ve skutečnosti povinné není",
        correcting_citations=[_CP(provision_id="p1", label="§ 297")],
    )

    async def _fake_classify(*a, **k):
        return fp

    monkeypatch.setattr(ans, "classify_meta", _fake_classify, raising=True)
    await ans.answer(_Q, method="cte")
    assert prompts, "GENERATE must have been called"
    # the correction-led premise prompt was used, NOT the 7-slot structured prompt (precedence)
    assert "Vaše situace" not in prompts[0]
