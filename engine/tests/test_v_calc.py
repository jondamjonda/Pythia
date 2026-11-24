"""Track V — V-CALC verifier (VL-D-07) unit tests. Fast, pure, crafted fixtures only (no LLM/DB/fetch).

Pins the DELIBERATELY-INVERTED computed-value guard:
  • PASS  — every number/date token in the claim is a VERBATIM substring of a cited source.
  • FAIL  'computed_value_not_in_source' — a number/date NOT in any cited source (a computed value) FAILS
    BY DEFAULT (verbatim-only rule).
  • SKIP  'no_computed_value'   — no number/date to guard.
  • SKIP  'no_source_available' — tokens present but no cited source text to check against (never a silent
    pass, never a hard-fail on absent evidence).
  • VL-D-07 4-LEG WORKED-EXAMPLE GATE (flag ON, default OFF, ships DARK): a claim that shows its working
    `a op b = c` PASSES only if ALL FOUR legs hold (rule present · inputs grounded · arithmetic correct ·
    result matches claim); drop any one leg ⇒ it does NOT pass (falls through to the default FAIL). With the
    flag OFF the branch is BYTE-IDENTICAL to the pre-gate verbatim-only rule (a would-be FAIL stays a FAIL) —
    pinned explicitly.
"""

from __future__ import annotations

from types import SimpleNamespace

from pythia.reasoning.claim_ledger import Claim, ClaimVerdict
from pythia.reasoning.v_calc import verify

# ── fixtures ───────────────────────────────────────────────────────────────────────────────────────────────

_OFF = SimpleNamespace(claim_calc_worked_example_enabled=False)
_ON = SimpleNamespace(claim_calc_worked_example_enabled=True)


def _claim(text: str, *, chips=None, type_: str = "deadline") -> Claim:
    return Claim(
        id="c0",
        span=(0, len(text)),
        text=text,
        slot="lhuty",
        type=type_,
        type_source="regex",
        chips=list(chips or []),
        normative=True,
    )


def _chip(pid: str) -> dict:
    return {"start": 0, "end": 1, "provision_id": pid, "label": "", "corpus": ""}


def _evi(cited_texts: dict) -> dict:
    return {"cited_texts": cited_texts, "chips": []}


def _one(verdicts) -> ClaimVerdict:
    assert isinstance(verdicts, list) and len(verdicts) == 1, verdicts
    v = verdicts[0]
    assert isinstance(v, ClaimVerdict)
    assert v.check == "v_calc"
    return v


# ── PASS: verbatim value present in the cited source ───────────────────────────────────────────────────────


def test_pass_amount_verbatim_in_source():
    claim = _claim("Pokuta činí 5 000 Kč.", chips=[_chip("p1")], type_="amount")
    evi = _evi({"p1": "Za přestupek lze uložit pokutu až do výše 5 000 Kč podle tohoto zákona."})
    v = _one(verify(claim, evi, _OFF))
    assert v.status == "pass"
    assert v.reason == "all_values_in_source"


def test_pass_date_verbatim_in_source():
    claim = _claim("Účinnost nastává 1.1.2025.")
    evi = _evi({"p1": "Tento zákon nabývá účinnosti dnem 1.1.2025."})
    claim.chips = [_chip("p1")]
    v = _one(verify(claim, evi, _OFF))
    assert v.status == "pass"


def test_pass_whitespace_nbsp_normalized():
    # source uses a NBSP thousands separator, claim a plain space — verbatim value is the same, must PASS.
    claim = _claim("Poplatek je 1 000 Kč.", chips=[_chip("p1")], type_="amount")
    evi = _evi({"p1": "Správní poplatek činí 1 000 Kč."})
    v = _one(verify(claim, evi, _OFF))
    assert v.status == "pass"


def test_pass_falls_back_to_bundle_when_no_chips():
    # claim carries no chips → the whole cited bundle is considered; value quoted from any source → PASS.
    claim = _claim("Lhůta je 15 dnů.", chips=[])
    evi = _evi({"p9": "Oznámení podejte do 15 dnů ode dne zahájení."})
    v = _one(verify(claim, evi, _OFF))
    assert v.status == "pass"


# ── FAIL: a computed value not present verbatim in any cited source ─────────────────────────────────────────


def test_fail_computed_value_not_in_source():
    # 30 dnů is asserted but the source only says "15 dnů" — a computed/invented value → FAIL by default.
    claim = _claim("Celková lhůta je 30 dnů.", chips=[_chip("p1")])
    evi = _evi({"p1": "První oznámení do 15 dnů a druhé do 15 dnů."})
    v = _one(verify(claim, evi, _OFF))
    assert v.status == "fail"
    assert v.reason == "computed_value_not_in_source"
    assert "30" in v.evidence["computed_values"]


def test_fail_computed_date_not_in_source():
    claim = _claim("Konec lhůty připadá na 31.1.2025.", chips=[_chip("p1")])
    evi = _evi({"p1": "Zákon nabývá účinnosti 1.1.2025, lhůta je 30 dnů."})
    v = _one(verify(claim, evi, _OFF))
    assert v.status == "fail"
    assert v.reason == "computed_value_not_in_source"


def test_fail_lists_only_the_missing_token():
    # 5 000 IS in source, 10 000 is NOT → fail lists only 10 000.
    claim = _claim("Od 5 000 Kč do 10 000 Kč.", chips=[_chip("p1")], type_="amount")
    evi = _evi({"p1": "Pokuta od 5 000 Kč výše."})
    v = _one(verify(claim, evi, _OFF))
    assert v.status == "fail"
    assert "10 000" in v.evidence["computed_values"]
    assert "5 000" not in v.evidence["computed_values"]


# ── SKIP: nothing to guard / no evidence ───────────────────────────────────────────────────────────────────


def test_skip_no_number_or_date():
    claim = _claim("Podnikatel musí ohlásit živnost živnostenskému úřadu.", type_="legal_rule")
    evi = _evi({"p1": "Ohlášení se podává živnostenskému úřadu."})
    v = _one(verify(claim, evi, _OFF))
    assert v.status == "skip"
    assert v.reason == "no_computed_value"


def test_skip_no_source_available():
    # a number is present but there is no cited text to check it against → conservative SKIP (not a fail).
    claim = _claim("Lhůta je 15 dnů.", chips=[_chip("p1")])
    evi = _evi({})  # empty bundle
    v = _one(verify(claim, evi, _OFF))
    assert v.status == "skip"
    assert v.reason == "no_source_available"


def test_skip_chip_pid_not_in_bundle_and_no_other_source():
    claim = _claim("Lhůta je 15 dnů.", chips=[_chip("pX")])
    evi = _evi({})  # pX resolves to nothing, no fallback text
    v = _one(verify(claim, evi, _OFF))
    assert v.status == "skip"
    assert v.reason == "no_source_available"


# ── VL-D-07 4-LEG WORKED-EXAMPLE GATE (flag ON, default OFF, ships DARK) ────────────────────────────────────

# A worked-example fixture: two grounded inputs (15 + 15) whose source quotes both "15 dnů", the claim shows
# its working `15 + 15 = 30`, and the result 30 is the computed value that is NOT verbatim in the source.
_WORKED_CLAIM = "Lhůta se skládá ze dvou částí: 15 + 15 = 30 dnů."
_WORKED_SRC = {"p1": "První oznámení do 15 dnů a druhé do 15 dnů."}


def test_on_verbatim_value_still_passes():
    # ON path: a directly-quoted value verifies (no missing token → the standard PASS, gate never engaged).
    claim = _claim("Lhůta je 15 dnů.", chips=[_chip("p1")])
    evi = _evi({"p1": "Oznámení do 15 dnů."})
    v = _one(verify(claim, evi, _ON))
    assert v.status == "pass"
    assert v.reason == "all_values_in_source"


def test_on_four_legs_pass_verifies():
    # ON path: all four legs hold (rule present via own chip · both inputs quoted · 15+15==30 · 30 == claim
    # result) → the worked example VERIFIES.
    claim = _claim(_WORKED_CLAIM, chips=[_chip("p1")], type_="deadline")
    v = _one(verify(claim, _evi(_WORKED_SRC), _ON))
    assert v.status == "pass"
    assert v.reason == "calc_worked_example_verified"
    assert v.evidence["worked_example"] is True
    assert v.evidence["legs"] == {
        "rule_present": True,
        "inputs_grounded": True,
        "arithmetic_correct": True,
        "result_matches_claim": True,
    }
    assert v.evidence["recomputed"] == 30
    assert "DRAFT" in v.evidence["caveat"]


def test_on_computed_wrong_arithmetic_does_not_pass():
    # ON path: the working is arithmetically WRONG (15 + 15 = 40) → leg 3 (arithmetic) fails → NOT passed;
    # it falls through to the default computed-value FAIL (never blanket-downgraded).
    claim = _claim("Součet: 15 + 15 = 40 dnů.", chips=[_chip("p1")], type_="deadline")
    v = _one(verify(claim, _evi(_WORKED_SRC), _ON))
    assert v.status == "fail"
    assert v.reason == "computed_value_not_in_source"
    assert v.evidence["worked_example_legs"]["arithmetic_correct"] is False


def test_on_drop_leg1_rule_absent_fails():
    # drop leg 1: the claim carries NO chip citing a governing provision (only whole-bundle fallback) → NOT
    # passed. (Both "15 dnů" still resolve via the bundle so legs 2/3/4 hold; only rule_present is false.)
    claim = _claim(_WORKED_CLAIM, chips=[], type_="deadline")
    v = _one(verify(claim, _evi(_WORKED_SRC), _ON))
    assert v.status == "fail"
    assert v.reason == "computed_value_not_in_source"
    assert v.evidence["worked_example_legs"]["rule_present"] is False
    assert v.evidence["worked_example_legs"]["inputs_grounded"] is True


def test_on_drop_leg2_inputs_ungrounded_fails():
    # drop leg 2: an input operand (99) is NOT in any cited source → inputs_grounded false → NOT passed.
    claim = _claim("Součet: 99 + 1 = 100 dnů.", chips=[_chip("p1")], type_="deadline")
    v = _one(verify(claim, _evi(_WORKED_SRC), _ON))
    assert v.status == "fail"
    assert v.reason == "computed_value_not_in_source"
    assert v.evidence["worked_example_legs"]["inputs_grounded"] is False


def test_on_drop_leg4_result_mismatch_fails():
    # drop leg 4: the claim asserts a DIFFERENT computed figure (35) than the expression it shows (=30), so
    # the recomputed result does not match the value being guarded → NOT passed.
    claim = _claim("Lhůta 15 + 15 = 30 dnů, tedy celkem 35 dnů.", chips=[_chip("p1")], type_="deadline")
    v = _one(verify(claim, _evi(_WORKED_SRC), _ON))
    assert v.status == "fail"
    assert v.reason == "computed_value_not_in_source"
    assert v.evidence["worked_example_legs"]["result_matches_claim"] is False


def test_on_no_working_shown_falls_through_to_fail():
    # ON path but the claim shows NO `a op b = c` working (a bare computed figure) → the gate does not engage
    # → default computed-value FAIL (identical to OFF for this claim).
    claim = _claim("Celková lhůta je 30 dnů.", chips=[_chip("p1")])
    v = _one(verify(claim, _evi(_WORKED_SRC), _ON))
    assert v.status == "fail"
    assert v.reason == "computed_value_not_in_source"
    assert "30" in v.evidence["computed_values"]


def test_on_flag_does_not_affect_skip():
    claim = _claim("Bez číselné hodnoty.", type_="legal_rule")
    evi = _evi({"p1": "Text bez čísel."})
    v = _one(verify(claim, evi, _ON))
    assert v.status == "skip"
    assert v.reason == "no_computed_value"


# ── flag OFF: BYTE-IDENTICAL to the pre-gate verbatim-only rule (pinned) ────────────────────────────────────


def test_off_worked_example_still_fails_byte_identical():
    # With the flag OFF, the very claim that would 4-leg-verify ON must stay a plain FAIL (no worked-example
    # path, no advisory, no leg annotation) — the pre-gate behaviour is preserved exactly.
    claim = _claim(_WORKED_CLAIM, chips=[_chip("p1")], type_="deadline")
    v = _one(verify(claim, _evi(_WORKED_SRC), _OFF))
    assert v.status == "fail"
    assert v.reason == "computed_value_not_in_source"
    assert "30" in v.evidence["computed_values"]
    assert "worked_example_legs" not in v.evidence  # OFF never annotates legs


def test_off_and_on_no_working_are_identical():
    # a bare computed figure (no shown working): the OFF and ON verdicts are the same FAIL evidence — proving
    # the ON gate is inert when there is no worked expression to engage it.
    claim_off = _claim("Celková lhůta je 30 dnů.", chips=[_chip("p1")])
    claim_on = _claim("Celková lhůta je 30 dnů.", chips=[_chip("p1")])
    off = _one(verify(claim_off, _evi(_WORKED_SRC), _OFF))
    on = _one(verify(claim_on, _evi(_WORKED_SRC), _ON))
    assert off.status == on.status == "fail"
    assert off.reason == on.reason == "computed_value_not_in_source"
    assert off.evidence == on.evidence


# ── verbatim-only discipline / determinism ─────────────────────────────────────────────────────────────────


def test_percent_token_verbatim():
    claim = _claim("Sazba je 21% ze základu.", type_="amount")
    evi = _evi({"p1": "Daň se stanoví ve výši 21% ."})
    claim.chips = [_chip("p1")]
    v = _one(verify(claim, evi, _OFF))
    assert v.status == "pass"


def test_percent_computed_fails():
    claim = _claim("Sazba je 15% ze základu.", type_="amount")
    evi = _evi({"p1": "Daň se stanoví ve výši 21%."})
    claim.chips = [_chip("p1")]
    v = _one(verify(claim, evi, _OFF))
    assert v.status == "fail"
    assert "15%" in v.evidence["computed_values"]


def test_deterministic_same_input_same_output():
    claim = _claim("Lhůta je 15 dnů, pokuta 5 000 Kč.", chips=[_chip("p1")])
    evi = _evi({"p1": "Do 15 dnů, pokuta 5 000 Kč."})
    a = verify(claim, evi, _OFF)
    b = verify(claim, evi, _OFF)
    assert a[0].status == b[0].status == "pass"
    assert a[0].reason == b[0].reason


def test_empty_claim_text_skips():
    claim = _claim("", type_="legal_rule")
    v = _one(verify(claim, _evi({"p1": "cokoli"}), _OFF))
    assert v.status == "skip"
    assert v.reason == "no_computed_value"


def test_int_keyed_cited_texts_match():
    # cited_texts keyed by ints (as answer.py builds them) resolve when the chip pid string-matches.
    claim = _claim("Lhůta je 15 dnů.", chips=[_chip("3")])
    evi = {"cited_texts": {3: "Oznámení do 15 dnů."}, "chips": []}
    v = _one(verify(claim, evi, _OFF))
    assert v.status == "pass"
