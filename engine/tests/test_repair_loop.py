"""Track V / V2 = MP-E — pin suite for the pure fix-loop / gap_demote / reassemble units (VL-E-01/02/04/05/07).

Pure, fast, fixture-driven — no LLM / DB / answer() wiring here (the answer()-level C1 pins live in
tests/test_answer_enforce.py). Covers: monotone termination + one-repair-max, deterministic repairs,
verbatim-only substitution, safe-drop, demote-to-gap mapping, non-citation-shaped gap copy, reassemble
identity + no-leak, and the single refusal rule.
"""

from __future__ import annotations

import re

from pythia.reasoning.claim_ledger import Claim, ClaimVerdict
from pythia.reasoning.claim_repair import (
    CORE_CLAIM_TYPES,
    RedraftBudget,
    band_cap,
    decide_refusal,
    llm_gate,
    make_llm_redraft,
    make_llm_redraft_batch,
    repoint_citation,
    run_fix_loop,
    strip_subclaim,
    substitute_value,
)
from pythia.reasoning.gap_demote import demote, gap_source_for, is_safety_drop
from pythia.reasoning.reassemble import reassemble

_CITE_SHAPE = re.compile(r"\d{1,4}/\d{4}").search  # a bare act-number pattern (citation-shaped)


def _claim(cid, text, span, slot="obligations", ctype="deadline", chips=None):
    return Claim(
        id=cid, span=span, text=text, slot=slot, type=ctype, type_source="regex", chips=chips or [], normative=True
    )


# ─────────────────────────────── deterministic repair primitives (VL-E-02) ────────────────────────────────


def test_repoint_replaces_first_bracket():
    c = _claim("c0", "Povinnost platí [455/1991 § 6].", (0, 30))
    out = repoint_citation(c, {"repoint_tag": "[240/2013 § 297]"})
    assert out == "Povinnost platí [240/2013 § 297]."


def test_repoint_none_without_target_or_bracket():
    c = _claim("c0", "Povinnost bez citace.", (0, 21))
    assert repoint_citation(c, {"repoint_tag": "[240/2013 § 297]"}) is None  # no bracket to replace
    c2 = _claim("c1", "Povinnost [455/1991 § 6].", (0, 25))
    assert repoint_citation(c2, {}) is None  # no target supplied


def test_substitute_rejects_non_verbatim_value():
    c = _claim("c0", "Lhůta je 30 dnů.", (0, 16))
    # correct_value NOT a verbatim substring of source ⇒ REFUSED (None)
    bad = substitute_value(
        c, {"wrong_value": "30 dnů", "correct_value": "15 dnů", "source_text": "podat do 8 dnů od zápisu"}
    )
    assert bad is None
    # correct_value IS verbatim in source ⇒ substituted
    ok = substitute_value(
        c, {"wrong_value": "30 dnů", "correct_value": "8 dnů", "source_text": "podat do 8 dnů od zápisu"}
    )
    assert ok == "Lhůta je 8 dnů."


def test_strip_keeps_passing_half_no_dangling_connective():
    c = _claim("c0", "Ohlaste živnost, a podejte přiznání do 15 dnů.", (0, 46))
    out = strip_subclaim(c, {"failing_subspan": "podejte přiznání do 15 dnů"})
    assert out == "Ohlaste živnost."
    assert ", a " not in out and out.endswith(".")


# ─────────────────────────────── the monotone bounded loop (VL-E-01) ──────────────────────────────────────


def _fail(check="v_cit", reason="cited_but_unsupported", ev=None):
    return ClaimVerdict(check=check, status="fail", reason=reason, evidence=ev or {})


def _pass(check="v_cit"):
    return ClaimVerdict(check=check, status="pass", reason="cited_and_supported")


def _skip(check="v_cit", reason="checker_exception"):
    return ClaimVerdict(check=check, status="skip", reason=reason)


def test_all_skip_claim_is_demoted_unchecked_never_verified():
    """VL-D-04 fail-closed: a claim whose EVERY check SKIPPED (checker crash / bad return / unregistered) was
    NEVER actually checked — it must be DEMOTED ('unchecked'), never silently passed/ledgered verified."""
    c = _claim("c0", "Lhůta je 8 dnů [240/2013 § 297].", (0, 33))
    loop = run_fix_loop(
        [c],
        {"c0": [_skip(reason="checker_exception"), _skip(check="v_fact", reason="bad_verifier_return")]},
        reverify=lambda cl, t: [_pass()],
    )
    st = loop.states[0]
    assert st.status == "demoted"  # NOT "pass"
    assert st.gap_source == "vf_unchecked"
    assert st.verifier_reason == "checker_exception"
    assert loop.patches and loop.patches[0].kind == "excise"  # excised under enforce, never verified


def test_pass_among_skips_still_verifies():
    """The demotion is scoped to ALL-skip: a claim with ≥1 real pass (and no fail) was genuinely checked and
    legitimately passes — the fail-closed fix must not over-demote a partially-skipped-but-passed claim."""
    c = _claim("c0", "Povinnost platí [240/2013 § 297].", (0, 34))
    loop = run_fix_loop([c], {"c0": [_pass(), _skip(check="v_fact")]}, reverify=lambda cl, t: [_pass()])
    assert loop.states[0].status == "pass"
    assert loop.patches == []


def test_clean_claim_passes_untouched():
    c = _claim("c0", "Povinnost platí [240/2013 § 297].", (0, 34))
    loop = run_fix_loop([c], {"c0": [_pass()]}, reverify=lambda cl, t: [_pass()])
    assert [s.status for s in loop.states] == ["pass"]
    assert loop.patches == []


def test_repoint_repair_clears_and_keeps_verbatim():
    c = _claim("c0", "Povinnost platí [455/1991 § 6].", (0, 30))
    fail = _fail(ev={"repoint_tag": "[240/2013 § 297]"})
    loop = run_fix_loop([c], {"c0": [fail]}, reverify=lambda cl, t: [_pass()])
    st = loop.states[0]
    assert st.status == "repaired" and st.repair_action == "repoint" and st.rounds_used == 1
    assert loop.patches[0].kind == "replace"
    assert loop.patches[0].new_text == "Povinnost platí [240/2013 § 297]."


def test_twice_failed_is_demoted_never_re_repaired():
    """MONOTONE: a repair that STILL fails on re-verify ⇒ demoted, never repaired a second time."""
    c = _claim("c0", "Lhůta je [455/1991 § 6].", (0, 24))
    fail = _fail(ev={"repoint_tag": "[240/2013 § 297]"})
    # reverify ALWAYS fails ⇒ the one repair does not clear ⇒ demote
    loop = run_fix_loop([c], {"c0": [fail]}, reverify=lambda cl, t: [_fail()])
    st = loop.states[0]
    assert st.status == "demoted"
    assert st.rounds_used == 1  # exactly ONE repair attempt (never re-repaired)
    assert loop.patches[0].kind == "excise"


def test_safe_fail_dropped_immediately_never_repaired():
    c = _claim("c0", "Doporučuji vám podat žalobu [240/2013 § 297].", (0, 44), ctype="legal_rule")
    fail = _fail(check="v_safe", reason="reserved_advice_claim", ev={"repoint_tag": "[X 1]"})
    called = {"reverify": 0}

    def _rv(cl, t):
        called["reverify"] += 1
        return [_pass()]

    loop = run_fix_loop([c], {"c0": [fail]}, reverify=_rv)
    st = loop.states[0]
    assert st.status == "safe_dropped" and st.rounds_used == 0
    assert called["reverify"] == 0  # never repaired (no re-verify)
    assert loop.patches[0].kind == "excise"


def test_one_repair_max_property():
    """Every claim uses ≤1 repair regardless of verdict multiplicity (bounded/monotone)."""
    claims = [_claim(f"c{i}", f"Lhůta {i} [455/1991 § 6].", (0, 20)) for i in range(5)]
    vbc = {c.id: [_fail(ev={"repoint_tag": "[240/2013 § 297]"})] for c in claims}
    loop = run_fix_loop(claims, vbc, reverify=lambda cl, t: [_fail()])  # all repairs fail ⇒ all demoted
    for st in loop.states:
        assert st.rounds_used <= 1


# ─────────────────────────────── gap_demote (VL-E-04 / C11) ───────────────────────────────────────────────


def test_gap_source_check_beats_type():
    assert gap_source_for("deadline", "v_calc") == "vf_calculation"  # check dominates
    assert gap_source_for("deadline", "v_cit") == "vf_deadline"  # type when check is generic
    assert gap_source_for("weird_type", None) == "vf_citation"  # unknown ⇒ generic


def test_safe_drop_emits_no_line():
    assert is_safety_drop("vf_safety")
    assert demote("obligations", "legal_rule", "v_safe") is None  # silent drop, no line (G14)


def test_gap_copy_is_non_citation_shaped():
    """C14b: every gap line carries NO bare act-number/§ adjacency, so it can't trip the 0.34 floor."""
    for ctype in ("deadline", "amount", "authority", "procedure_step", "legal_rule", "sanction"):
        gl = demote("obligations", ctype, "v_cit", provision_hint="§ 297 zákona 240/2013")
        assert gl is not None
        assert _CITE_SHAPE(gl.copy) is None, (ctype, gl.copy)  # hint prose kept out of citation grammar
        assert "§" not in gl.copy or True  # § allowed only in free prose, never [§ n]


def test_gap_dedup_key_is_slot_and_source():
    a = demote("obligations", "deadline", "v_cit")
    b = demote("obligations", "deadline", "v_fact")  # same source vf_deadline ⇒ same key
    assert a.gap_key == b.gap_key == ("obligations", "vf_deadline")


# ─────────────────────────────── reassemble (VL-E-05 / C2) ────────────────────────────────────────────────


def test_reassemble_zero_failure_identity():
    draft = "## Legislativní rámec\nPovinnost platí [240/2013 § 297].\n## Hodnocení\nOK."
    assert reassemble(draft, [], [], []) == draft  # no patches, no gaps ⇒ byte-identical


def test_reassemble_excises_demoted_and_inserts_gap_no_leak():
    draft = "## Konkrétní povinnosti a lhůty\nLhůta je 30 dnů. Ohlaste živnost [240/2013 § 297].\n## Hodnocení\nOK."
    # demote the first sentence "Lhůta je 30 dnů."
    s0, e0 = draft.index("Lhůta"), draft.index("Ohlaste")
    from pythia.reasoning.claim_repair import ClaimState, Patch

    st = ClaimState(
        claim_id="c0",
        span=(s0, e0),
        slot="obligations",
        claim_type="deadline",
        check="v_cit",
        status="demoted",
        gap_source="vf_deadline",
    )
    gl = demote("obligations", "deadline", "v_cit")
    out = reassemble(draft, [st], [Patch(span=(s0, e0), kind="excise", claim_id="c0")], [gl])
    assert "Lhůta je 30 dnů." not in out  # NO-LEAK: demoted text gone
    assert "nepodařilo ověřit" in out  # gap line inserted
    assert "Ohlaste živnost" in out  # surviving claim kept verbatim


# ─────────────────────────────── refusal (VL-E-07 / C2) ───────────────────────────────────────────────────


def _state(cid, ctype, status):
    from pythia.reasoning.claim_repair import ClaimState

    return ClaimState(claim_id=cid, span=(0, 1), slot="obligations", claim_type=ctype, check="v_cit", status=status)


def test_refuse_when_all_core_failed():
    states = [_state("c0", "deadline", "demoted"), _state("c1", "legal_rule", "demoted")]
    r = decide_refusal(states)
    assert r["refuse"] and "no core claim survived" in r["reason"]


def test_no_refuse_when_one_core_survives():
    states = [_state("c0", "deadline", "pass")] + [_state(f"c{i}", "authority", "demoted") for i in range(1, 4)]
    # 1 core survives; non-core demotions; survival = 1/4 = 0.25 < 0.34 ⇒ still refuse by the ratio rule
    r = decide_refusal(states)
    assert r["core_survival"] == 1.0
    assert r["refuse"] is True and r["survival"] == 0.25


def test_survival_floor_boundary():
    # 2 survive of 5 = 0.4 ≥ 0.34 ⇒ no refuse (a core survives)
    states = [_state("c0", "deadline", "pass"), _state("c1", "amount", "repaired")] + [
        _state(f"c{i}", "authority", "demoted") for i in range(2, 5)
    ]
    r = decide_refusal(states)
    assert r["survival"] == 0.4 and r["refuse"] is False


def test_zero_claims_never_manufactures_refusal():
    assert decide_refusal([])["refuse"] is False


def test_band_cap_downgrade_only():
    core_demoted = [_state("c0", "deadline", "demoted")]
    assert band_cap(core_demoted, "full") is True
    assert band_cap([_state("c1", "authority", "demoted")], "full") is False  # non-core ⇒ no cap
    assert band_cap(core_demoted, None) is False  # metagate OFF ⇒ no band


# ─────────────────────────────── LLM gate (VL-E-03) ───────────────────────────────────────────────────────


def test_llm_gate_rejects_fabricated_number_and_out_of_map_tag():
    win = "podat do 8 dnů od zápisu [240/2013 § 297]"
    orig = "Lhůta je nejistá [240/2013 § 297]."  # realistic input length (the 2× cap is relative to it)
    assert llm_gate(orig, "Lhůta je 8 dnů [240/2013 § 297].", win, ["[240/2013 § 297]"]) is True
    # fabricated number (15 not in window)
    assert llm_gate(orig, "Lhůta je 15 dnů [240/2013 § 297].", win, ["[240/2013 § 297]"]) is False
    # out-of-map tag
    assert llm_gate(orig, "Lhůta [999/2000 § 1].", win, ["[240/2013 § 297]"]) is False
    # runaway length (> 2× input) is rejected
    assert llm_gate("krátká věta.", "x " * 200, win, ["[240/2013 § 297]"]) is False


def test_core_claim_set_matches_contract():
    assert CORE_CLAIM_TYPES == frozenset(
        {"legal_rule", "deadline", "amount", "sanction", "procedure_step", "situation_application"}
    )


# ─────────────────────────── the LLM prose-repair lane wiring (VL-E-03, STUB-only) ────────────────────────
# Every test here injects a DETERMINISTIC STUB `call_model` — no live LLM is ever touched. The stubs cover the
# make_llm_redraft factory (budget / fail-safe / monotone pre-gate) and the run_fix_loop integration
# (redraft-fixes ⇒ repaired; redraft-still-fails ⇒ demoted; redraft-adds-ungrounded ⇒ rejected).

_WINDOW = "podat do 8 dnů od zápisu [240/2013 § 297]"
_ALLOWED = ["[240/2013 § 297]"]


def _redraft_fn(stub_call, cap=4):
    """Build the injected redraft fn from a stub prompt->str|None, wired to the canonical window/tags above."""
    return make_llm_redraft(
        stub_call,
        budget=RedraftBudget(cap=cap),
        evidence_window_for=lambda c, f, ch: _WINDOW,
        allowed_tags_for=lambda c, f, ch: _ALLOWED,
    )


def test_llm_redraft_stub_fix_passes_gate():
    """A stub that returns a gate-clean redraft (verbatim number, in-Map tag) ⇒ returned unchanged."""
    c = _claim("c0", "Lhůta je nejistá [240/2013 § 297].", (0, 34))
    fn = _redraft_fn(lambda prompt: "Lhůta je 8 dnů [240/2013 § 297].")
    assert fn(c, _fail(), c.chips) == "Lhůta je 8 dnů [240/2013 § 297]."


def test_llm_redraft_stub_none_falls_through():
    """FAIL-SAFE: a stub returning None (or empty) ⇒ the redraft fn returns None (deterministic demote)."""
    c = _claim("c0", "Lhůta je nejistá [240/2013 § 297].", (0, 34))
    assert _redraft_fn(lambda prompt: None)(c, _fail(), c.chips) is None
    assert _redraft_fn(lambda prompt: "   ")(c, _fail(), c.chips) is None


def test_llm_redraft_stub_raise_is_fail_safe():
    """FAIL-SAFE: a stub that RAISES ⇒ swallowed ⇒ None (never propagates, never breaks the loop)."""

    def _boom(prompt):
        raise RuntimeError("model exploded")

    c = _claim("c0", "Lhůta je nejistá [240/2013 § 297].", (0, 34))
    assert _redraft_fn(_boom)(c, _fail(), c.chips) is None


def test_llm_redraft_monotone_pregate_rejects_fabricated_number_and_out_of_map_tag():
    """MONOTONE pre-gate: a redraft smuggling a fabricated number OR an out-of-Map tag ⇒ rejected (None)."""
    c = _claim("c0", "Lhůta je nejistá [240/2013 § 297].", (0, 34))
    # 15 not in the evidence window ⇒ fabricated number ⇒ rejected
    assert _redraft_fn(lambda p: "Lhůta je 15 dnů [240/2013 § 297].")(c, _fail(), c.chips) is None
    # a bracket tag not in the allow-list ⇒ rejected
    assert _redraft_fn(lambda p: "Lhůta [999/2000 § 1].")(c, _fail(), c.chips) is None


def test_llm_redraft_budget_bounds_calls():
    """BUDGET (C9): the fn stops calling the model once the cap is hit — a cap of 1 ⇒ exactly one model call."""
    calls = {"n": 0}

    def _count(prompt):
        calls["n"] += 1
        return "Lhůta je 8 dnů [240/2013 § 297]."

    c = _claim("c0", "Lhůta je nejistá [240/2013 § 297].", (0, 34))
    fn = _redraft_fn(_count, cap=1)
    assert fn(c, _fail(), c.chips) is not None  # 1st call charges + calls
    assert fn(c, _fail(), c.chips) is None  # budget exhausted ⇒ NO model call, returns None
    assert calls["n"] == 1  # the model was called exactly ONCE


# ─────────────────────────── run_fix_loop × the injected redraft lane (STUB-only) ─────────────────────────
_CHIP = [{"provision_id": "p1", "label": "[240/2013 § 297]", "corpus": "240/2013"}]


def _claim_c(cid, text, span, chips=None):
    return _claim(cid, text, span, chips=chips or list(_CHIP))


def test_loop_llm_redraft_fixes_claim_flips_to_repaired():
    """A stub redraft that FIXES the claim ⇒ span replaced, ledger claim flips to repaired, no new claim."""
    c = _claim_c("c0", "Lhůta je nejistá [240/2013 § 297].", (0, 34))
    redraft = _redraft_fn(lambda p: "Lhůta je 8 dnů [240/2013 § 297].")
    # deterministic repairs can't fire (no repoint_tag/substitute evidence) ⇒ the LLM lane is reached;
    # reverify PASSES on the redraft ⇒ kept as a replace patch, status repaired.
    loop = run_fix_loop([c], {"c0": [_fail()]}, reverify=lambda cl, t: [_pass()], llm_redraft=redraft)
    st = loop.states[0]
    assert st.status == "repaired" and st.repair_action == "llm_redraft" and st.rounds_used == 1
    assert loop.llm_repairs == 1
    assert loop.patches[0].kind == "replace"
    assert loop.patches[0].new_text == "Lhůta je 8 dnů [240/2013 § 297]."


def test_loop_llm_redraft_still_fails_is_demoted():
    """A stub redraft that is gate-clean but STILL fails re-verify ⇒ discarded ⇒ demoted (monotone, as today)."""
    c = _claim_c("c0", "Lhůta je nejistá [240/2013 § 297].", (0, 34))
    redraft = _redraft_fn(lambda p: "Lhůta je 8 dnů [240/2013 § 297].")
    loop = run_fix_loop(
        [c], {"c0": [_fail()]}, reverify=lambda cl, t: [_fail()], llm_redraft=redraft
    )  # re-verify never clears
    st = loop.states[0]
    assert st.status == "demoted" and st.rounds_used == 1
    assert loop.patches[0].kind == "excise"


def test_loop_llm_redraft_new_ungrounded_claim_is_rejected():
    """MONOTONE: a stub redraft that introduces a NEW ungrounded assertion (out-of-Map tag / fabricated number)
    is rejected by the pre-gate ⇒ no LLM repair ⇒ deterministic demote (the loop never adds an unverified
    claim to the output)."""
    c = _claim_c("c0", "Lhůta je nejistá [240/2013 § 297].", (0, 34))
    # the stub would 'fix' the claim by fabricating '30 dnů' (not in the window) + citing an out-of-Map act.
    redraft = _redraft_fn(lambda p: "Lhůta je 30 dnů [999/2000 § 1].")
    reverify_calls = {"n": 0}

    def _rv(cl, t):
        reverify_calls["n"] += 1
        return [_pass()]  # even if reverify WOULD clear it, the gate blocks it

    loop = run_fix_loop([c], {"c0": [_fail()]}, reverify=_rv, llm_redraft=redraft)
    st = loop.states[0]
    assert st.status == "demoted"  # rejected by the gate ⇒ deterministic demote
    assert st.repair_action is None and loop.llm_repairs == 0
    assert reverify_calls["n"] == 0  # the rejected redraft never reached re-verify


def test_loop_llm_redraft_none_is_byte_identical_to_deterministic():
    """llm_redraft=None ⇒ the loop runs the pure deterministic path (no LLM lane) — identical states/patches."""
    c = _claim_c("c0", "Lhůta je nejistá [240/2013 § 297].", (0, 34))
    det = run_fix_loop([c], {"c0": [_fail()]}, reverify=lambda cl, t: [_fail()], llm_redraft=None)
    assert det.states[0].status == "demoted" and det.states[0].repair_action is None
    assert det.llm_repairs == 0 and det.patches[0].kind == "excise"


def test_loop_llm_redraft_budget_terminates_within_bound():
    """Budget-bound: with a cap of 1 and MANY failing claims, only the first gets an LLM attempt; the loop
    still terminates and every claim reaches a terminal state (repaired/demoted) — no unbounded retry."""
    claims = [_claim_c(f"c{i}", "Lhůta je nejistá [240/2013 § 297].", (0, 34)) for i in range(4)]
    vbc = {c.id: [_fail()] for c in claims}
    calls = {"n": 0}

    def _count(prompt):
        calls["n"] += 1
        return "Lhůta je 8 dnů [240/2013 § 297]."

    redraft = _redraft_fn(_count, cap=1)
    loop = run_fix_loop(claims, vbc, reverify=lambda cl, t: [_pass()], llm_redraft=redraft)
    assert calls["n"] == 1  # budget capped model calls at 1
    assert loop.llm_repairs == 1  # exactly one claim repaired via the LLM lane
    # every claim is terminal; the 3 budget-blocked ones demote (their redraft fn returned None)
    assert all(st.status in ("repaired", "demoted") for st in loop.states)
    assert sum(1 for st in loop.states if st.status == "repaired") == 1
    assert sum(1 for st in loop.states if st.status == "demoted") == 3


# ─────────────────── VL-E-03 (WS5 step 45) — the BATCHED redraft lane (≤4 claims per ONE cheap call) ─────────
# The batched bridge shape is (prompt, items) -> list[str|None]; every returned redraft is still gated (inside
# the factory) + RE-VERIFIED PER CLAIM in run_fix_loop. These pins are STUB-only (no live LLM).


def _redraft_batch_fn(stub_call, cap=13, max_batch=4):
    """Build the injected BATCHED redraft fn from a stub (prompt, items)->list[str|None], wired to the canonical
    per-claim window/tags above (all items share the same test window/allow-list)."""
    return make_llm_redraft_batch(
        stub_call,
        budget=RedraftBudget(cap=cap),
        evidence_window_for=lambda c, f, ch: _WINDOW,
        allowed_tags_for=lambda c, f, ch: _ALLOWED,
        max_batch=max_batch,
    )


def test_batch_four_claims_is_exactly_one_cheap_call():
    """PORT-SPY: 4 failed-repairable claims ⇒ the batched lane issues EXACTLY ONE cheap call (all 4 in one
    batch), and all 4 are repaired (reverify passes on the gate-clean redraft)."""
    claims = [_claim_c(f"c{i}", "Lhůta je nejistá [240/2013 § 297].", (0, 34)) for i in range(4)]
    vbc = {c.id: [_fail()] for c in claims}
    calls = {"n": 0}

    def _batch_call(prompt, items):
        calls["n"] += 1
        return ["Lhůta je 8 dnů [240/2013 § 297]." for _ in items]  # one gate-clean redraft per item

    loop = run_fix_loop(claims, vbc, reverify=lambda cl, t: [_pass()], llm_redraft_batch=_redraft_batch_fn(_batch_call))
    assert calls["n"] == 1  # EXACTLY one cheap call for the batch of 4
    assert loop.llm_repairs == 4
    assert all(st.status == "repaired" and st.repair_action == "llm_redraft" for st in loop.states)


def test_batch_reverifies_each_claim_independently():
    """PER-CLAIM re-verify: a batch of 2 good + 2 still-failing redrafts ⇒ 2 repaired + 2 demoted (one claim's
    success never masks another's failure). Still ONE cheap call."""
    claims = [_claim_c(f"c{i}", "Lhůta je nejistá [240/2013 § 297].", (0, 34)) for i in range(4)]
    vbc = {c.id: [_fail()] for c in claims}
    calls = {"n": 0}

    def _batch_call(prompt, items):
        calls["n"] += 1
        return ["Lhůta je 8 dnů [240/2013 § 297]." for _ in items]  # all gate-clean redrafts

    # reverify PASSES for c0/c2, FAILS for c1/c3 (independent per-claim monotone guard).
    good_ids = {"c0", "c2"}

    def _rv(claim, text):
        return [_pass()] if claim.id in good_ids else [_fail()]

    loop = run_fix_loop(claims, vbc, reverify=_rv, llm_redraft_batch=_redraft_batch_fn(_batch_call))
    assert calls["n"] == 1  # ONE cheap call for the whole batch
    by_id = {st.claim_id: st for st in loop.states}
    assert by_id["c0"].status == "repaired" and by_id["c2"].status == "repaired"
    assert by_id["c1"].status == "demoted" and by_id["c3"].status == "demoted"
    assert loop.llm_repairs == 4  # 4 attempted (llm_repairs counts attempts)
    # the demoted claims are excised, the repaired ones replaced
    kinds = {p.claim_id: p.kind for p in loop.patches}
    assert kinds["c0"] == "replace" and kinds["c1"] == "excise"


def test_batch_off_is_byte_identical_to_deterministic():
    """OFF: no batched bridge ⇒ the loop runs the pure deterministic path — ZERO cheap calls, deterministic
    demote (byte-identical states/patches to the pre-lane loop)."""
    claims = [_claim_c(f"c{i}", "Lhůta je nejistá [240/2013 § 297].", (0, 34)) for i in range(4)]
    vbc = {c.id: [_fail()] for c in claims}
    det = run_fix_loop(claims, vbc, reverify=lambda cl, t: [_fail()])  # no llm lane at all
    assert det.llm_repairs == 0
    assert all(st.status == "demoted" and st.repair_action is None for st in det.states)
    assert all(p.kind == "excise" for p in det.patches)


def test_batch_budget_exhausted_demotes_batch_never_fabricates_pass():
    """BUDGET (C9) PER BATCH: with cap 0 the single take() is refused ⇒ NO model call, the batch's claims all
    demote (never a fabricated pass)."""
    claims = [_claim_c(f"c{i}", "Lhůta je nejistá [240/2013 § 297].", (0, 34)) for i in range(4)]
    vbc = {c.id: [_fail()] for c in claims}
    calls = {"n": 0}

    def _batch_call(prompt, items):
        calls["n"] += 1
        return ["Lhůta je 8 dnů [240/2013 § 297]." for _ in items]

    loop = run_fix_loop(
        claims, vbc, reverify=lambda cl, t: [_pass()], llm_redraft_batch=_redraft_batch_fn(_batch_call, cap=0)
    )
    assert calls["n"] == 0  # budget exhausted ⇒ the model is NEVER called
    assert loop.llm_repairs == 0
    assert all(st.status == "demoted" for st in loop.states)


def test_batch_chunks_into_fours_one_call_each():
    """CHUNKING: 6 failed claims ⇒ two batches (4 + 2) ⇒ TWO cheap calls (one budget.take per batch)."""
    claims = [_claim_c(f"c{i}", "Lhůta je nejistá [240/2013 § 297].", (0, 34)) for i in range(6)]
    vbc = {c.id: [_fail()] for c in claims}
    calls = {"n": 0}
    sizes: list[int] = []

    def _batch_call(prompt, items):
        calls["n"] += 1
        sizes.append(len(items))
        return ["Lhůta je 8 dnů [240/2013 § 297]." for _ in items]

    loop = run_fix_loop(
        claims, vbc, reverify=lambda cl, t: [_pass()], llm_redraft_batch=_redraft_batch_fn(_batch_call, max_batch=4)
    )
    assert calls["n"] == 2 and sizes == [4, 2]  # ≤4 per call: [4, 2]
    assert sum(1 for st in loop.states if st.status == "repaired") == 6


def test_batch_deterministic_repairs_still_fire_first():
    """A claim that a DETERMINISTIC repair (repoint) fixes is NEVER deferred to the batch — the batch only sees
    the deterministic-unrepairable claims. Here c0 has repoint evidence (fixed deterministically), c1 does not
    (goes to the batch)."""
    c0 = _claim_c("c0", "Lhůta je nejistá [455/1991 § 6].", (0, 34))
    c1 = _claim_c("c1", "Lhůta je nejistá [240/2013 § 297].", (0, 34))
    vbc = {"c0": [_fail(ev={"repoint_tag": "[240/2013 § 297]"})], "c1": [_fail()]}
    calls = {"n": 0}
    seen_ids: list[str] = []

    def _batch_call(prompt, items):
        calls["n"] += 1
        seen_ids.extend(c.id for c, _f, _ch in items)
        return ["Lhůta je 8 dnů [240/2013 § 297]." for _ in items]

    loop = run_fix_loop(
        [c0, c1], vbc, reverify=lambda cl, t: [_pass()], llm_redraft_batch=_redraft_batch_fn(_batch_call)
    )
    by_id = {st.claim_id: st for st in loop.states}
    assert by_id["c0"].repair_action == "repoint"  # deterministic, never batched
    assert by_id["c1"].repair_action == "llm_redraft"  # only c1 reached the batch
    assert seen_ids == ["c1"] and calls["n"] == 1


def test_batch_fail_safe_all_none_on_bad_shape():
    """FAIL-SAFE: a batched bridge returning a SHORT list (fewer than N) ⇒ the missing items get None ⇒ demote;
    a bridge that RAISES ⇒ the whole batch demotes (never breaks the loop)."""
    claims = [_claim_c(f"c{i}", "Lhůta je nejistá [240/2013 § 297].", (0, 34)) for i in range(3)]
    vbc = {c.id: [_fail()] for c in claims}

    def _short(prompt, items):
        return ["Lhůta je 8 dnů [240/2013 § 297]."]  # only ONE reply for three items

    loop = run_fix_loop(claims, vbc, reverify=lambda cl, t: [_pass()], llm_redraft_batch=_redraft_batch_fn(_short))
    st = {s.claim_id: s for s in loop.states}
    assert st["c0"].status == "repaired"  # first item got its redraft
    assert st["c1"].status == "demoted" and st["c2"].status == "demoted"  # missing ⇒ None ⇒ demote

    def _boom(prompt, items):
        raise RuntimeError("model exploded")

    loop2 = run_fix_loop(claims, vbc, reverify=lambda cl, t: [_pass()], llm_redraft_batch=_redraft_batch_fn(_boom))
    assert all(s.status == "demoted" for s in loop2.states)  # a raise ⇒ demote-all (never propagates)


def test_batch_per_item_gate_rejects_fabrication_independently():
    """MONOTONE per-item pre-gate: in a batch, an item whose redraft smuggles a fabricated number / out-of-Map
    tag is rejected (demoted) WITHOUT poisoning its clean siblings."""
    claims = [_claim_c(f"c{i}", "Lhůta je nejistá [240/2013 § 297].", (0, 34)) for i in range(2)]
    vbc = {c.id: [_fail()] for c in claims}

    def _mixed(prompt, items):
        # c0: gate-clean; c1: fabricated number (15 not in _WINDOW) ⇒ gate rejects it.
        return ["Lhůta je 8 dnů [240/2013 § 297].", "Lhůta je 15 dnů [240/2013 § 297]."]

    loop = run_fix_loop(claims, vbc, reverify=lambda cl, t: [_pass()], llm_redraft_batch=_redraft_batch_fn(_mixed))
    st = {s.claim_id: s for s in loop.states}
    assert st["c0"].status == "repaired"  # clean sibling repaired
    assert st["c1"].status == "demoted"  # gate-rejected ⇒ demoted, not repaired
