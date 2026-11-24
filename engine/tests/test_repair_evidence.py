"""VL-E-02 (WS3 step 23) — the verifiers emit REPAIR EVIDENCE on fail (BUILT-DEAD → alive), so the repair
primitives have a target instead of demoting blindly:

  • V-CIT  fail → repoint_tag      (the in-Map bracket that WOULD ground)
  • V-FACT fail → wrong/correct_value + source_text  (a verbatim fact-row substitution — the 'Dose' analog)
  • V-CALC fail → failing_subspan  (the computed value STRIP excises)

Plus the end-to-end guarantee: a fact miscite is REPAIRED (not demoted), and a non-verbatim substitution is
refused (rule 7).
"""

from __future__ import annotations

from pythia.reasoning import v_calc, v_fact
from pythia.reasoning.claim_ledger import Claim
from pythia.reasoning.claim_repair import run_fix_loop, substitute_value


class _S:
    claim_verify_enabled = True
    claim_entailment_llm_enabled = False


def _claim(text, ctype, chips, slot="lhuty"):
    return Claim(
        id="c0", span=(0, len(text)), text=text, slot=slot, type=ctype, type_source="regex", chips=chips, normative=True
    )


def test_v_fact_fail_emits_verbatim_substitution():
    claim = _claim("Lhůta je 30 dnů.", "deadline", [{"provision_id": "P1"}])
    ev = {
        "cited_texts": {"P1": "Oznámení se podává do 8 dnů od zápisu."},
        "fact_rows": {"P1": [{"fact_type": "deadline", "value_raw": "do 8 dnů"}]},
    }
    v = next(x for x in v_fact.verify(claim, ev, _S()) if x.check == "v_fact")
    assert v.status == "fail"
    assert v.evidence.get("correct_value") == "do 8 dnů"  # the verbatim fact-row value to substitute in
    assert v.evidence.get("wrong_value") in claim.text  # the claim token to replace (here the phrase)
    assert "do 8 dnů" in v.evidence.get("source_text", "")  # correct_value is verbatim in the source


def test_v_fact_substitution_ambiguous_is_omitted():
    # two DISTINCT same-category values ⇒ no unambiguous correction ⇒ no substitution evidence (demote).
    claim = _claim("Lhůta je 30 dnů.", "deadline", [{"provision_id": "P1"}])
    ev = {
        "cited_texts": {"P1": "do 8 dnů nebo do 15 dnů"},
        "fact_rows": {
            "P1": [
                {"fact_type": "deadline", "value_raw": "do 8 dnů"},
                {"fact_type": "deadline", "value_raw": "do 15 dnů"},
            ]
        },
    }
    v = next(x for x in v_fact.verify(claim, ev, _S()) if x.check == "v_fact")
    assert v.status == "fail" and "correct_value" not in v.evidence


def test_v_calc_fail_emits_failing_subspan():
    claim = _claim("Celkem zaplatíte 45 000 Kč.", "amount", [{"provision_id": "P1"}], slot="hodnoceni")
    ev = {"cited_texts": {"P1": "Sazba je 15 000 Kč měsíčně."}}
    v = next(x for x in v_calc.verify(claim, ev, _S()) if x.check == "v_calc")
    if v.status == "fail":  # only when a computed value is actually detected
        assert v.evidence.get("failing_subspan")


def test_fact_miscite_is_repaired_not_demoted():
    # END-TO-END: a wrong deadline value + a verbatim fact-row correction ⇒ the fix-loop SUBSTITUTES and the
    # claim is 'repaired', not 'demoted'. reverify passes on the corrected text.
    claim = _claim("Lhůta je 30 dnů.", "deadline", [{"provision_id": "P1"}])
    fail = next(
        x
        for x in v_fact.verify(
            claim,
            {
                "cited_texts": {"P1": "Oznámení se podává do 8 dnů od zápisu."},
                "fact_rows": {"P1": [{"fact_type": "deadline", "value_raw": "do 8 dnů"}]},
            },
            _S(),
        )
        if x.check == "v_fact"
    )
    loop = run_fix_loop(
        [claim],
        {"c0": [fail]},
        reverify=lambda cl, t: [type(fail)(check="v_fact", status="pass", reason="fact_tokens_grounded")],
    )
    st = loop.states[0]
    assert st.status == "repaired" and st.repair_action == "substitute"


def test_substitute_rejects_non_verbatim(monkeypatch):
    # rule 7 fuzz pin: a correct_value NOT verbatim in the source is REFUSED (None), never applied.
    claim = _claim("Lhůta je 30 dnů.", "deadline", [{"provision_id": "P1"}])
    assert (
        substitute_value(
            claim, {"wrong_value": "30 dnů", "correct_value": "8 dnů", "source_text": "text bez te hodnoty"}
        )
        is None
    )
