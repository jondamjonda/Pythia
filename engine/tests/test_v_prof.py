"""Fast, PURE unit tests for V-PROF (contract C13 — intake-fact consistency). No LLM / DB / fetch.

Covers: pass (asserted asker fact matches the record), fail (contradicts / absent — asker_fact_unverified +
gap_source=vf_profile), skip (no intake/profile facts → cannot verify; no profile-axis assertion; wrong
claim type). Fixtures are hand-crafted Claim + evidence dicts."""

from __future__ import annotations

from pythia.reasoning.claim_ledger import Claim
from pythia.reasoning.v_prof import verify


def _claim(text: str, ctype: str = "situation_application") -> Claim:
    return Claim(id="c0", span=(0, len(text)), text=text, slot="assessment", type=ctype, type_source="regex")


class _S:  # a stand-in settings object (V-PROF ignores it — it is pure/deterministic)
    claim_verify_enabled = True


_S_ = _S()


# ── SKIP cases ───────────────────────────────────────────────────────────────────────────────────────────
def test_skip_when_no_facts_present():
    """Empty evidence ⇒ cannot verify ⇒ skip (never a fail)."""
    v = verify(_claim("Ve vašem případě jako s.r.o. platí povinnost."), {}, _S_)
    assert len(v) == 1
    assert v[0].status == "skip"
    assert v[0].reason == "no_asker_facts"


def test_skip_when_evidence_has_only_law_keys():
    """The V1 evidence shape {cited_texts, chips} carries NO asker facts ⇒ skip (SHADOW byte-identical)."""
    ev = {"cited_texts": {"p1": "text"}, "chips": []}
    v = verify(_claim("Jako s.r.o. musíte."), ev, _S_)
    assert v[0].status == "skip" and v[0].reason == "no_asker_facts"


def test_skip_when_claim_asserts_no_profile_axis():
    """A situation claim that ascribes no legal_form/gate/municipality ⇒ nothing to check ⇒ skip."""
    ev = {"profile_context": {"legal_form": "s.r.o."}}
    v = verify(_claim("Ve vašem případě je vhodné poradit se s odborníkem."), ev, _S_)
    assert v[0].status == "skip" and v[0].reason == "no_asker_assertion"


def test_skip_when_wrong_claim_type_routed():
    """Defensive: a non-situation claim routed here is skipped, never fabricated into a profile verdict."""
    ev = {"profile_context": {"legal_form": "s.r.o."}}
    v = verify(_claim("Do 15 dnů podejte oznámení.", ctype="deadline"), ev, _S_)
    assert v[0].status == "skip" and v[0].reason == "not_situation_claim"


# ── PASS cases ───────────────────────────────────────────────────────────────────────────────────────────
def test_pass_legal_form_matches_profile():
    ev = {"profile_context": {"legal_form": "s.r.o."}}
    v = verify(_claim("Ve vašem případě jako s.r.o. platí registrační povinnost."), ev, _S_)
    assert v[0].status == "pass" and v[0].reason == "asker_fact_consistent"
    assert "legal_form" in v[0].evidence["matched_axes"]


def test_pass_legal_form_variant_spelling_matches():
    """'s. r. o.' in the claim vs 'sro' in the profile normalise equal (no transform, token equality)."""
    ev = {"profile_context": {"legal_form": "sro"}}
    v = verify(_claim("Jako s. r. o. máte tuto povinnost."), ev, _S_)
    assert v[0].status == "pass"


def test_pass_employer_gate_matches():
    ev = {"profile_context": {"is_employer": True}}
    v = verify(_claim("Protože zaměstnáváte lidi, vztahuje se na vás zákoník práce."), ev, _S_)
    assert v[0].status == "pass"
    assert "is_employer" in v[0].evidence["matched_axes"]


def test_pass_from_intake_facts_legal_form():
    """The record can come from IntakeRecord facts (dicts) instead of raw profile_context."""
    ev = {"intake_facts": [{"field": "legal_form", "value": "a.s.", "source": "query"}]}
    v = verify(_claim("Vaše a.s. musí vést účetnictví."), ev, _S_)
    assert v[0].status == "pass"


# ── FAIL cases ───────────────────────────────────────────────────────────────────────────────────────────
def test_fail_legal_form_contradicts_profile():
    """Profile says s.r.o.; the draft calls the asker an a.s. ⇒ fail asker_fact_unverified / vf_profile."""
    ev = {"profile_context": {"legal_form": "s.r.o."}}
    v = verify(_claim("Ve vašem případě jako a.s. platí přísnější pravidla."), ev, _S_)
    assert v[0].status == "fail"
    assert v[0].reason == "asker_fact_unverified"
    assert v[0].evidence["gap_source"] == "vf_profile"
    mm = v[0].evidence["mismatches"]
    assert mm[0]["axis"] == "legal_form"
    assert mm[0]["kind"] == "contradict"
    assert mm[0]["asserted"] == "as" and mm[0]["record"] == "sro"


def test_fail_asserted_fact_absent_from_record():
    """The record has legal_form only; the claim asserts an employer gate the record does not carry ⇒ fail."""
    ev = {"profile_context": {"legal_form": "s.r.o."}}
    v = verify(_claim("Jako s.r.o., a protože zaměstnáváte lidi, máte povinnost."), ev, _S_)
    assert v[0].status == "fail"
    mm = {m["axis"]: m for m in v[0].evidence["mismatches"]}
    assert "is_employer" in mm and mm["is_employer"]["kind"] == "absent"
    # the legal_form part matched, so it is recorded as a matched axis on the fail verdict
    assert "legal_form" in v[0].evidence["matched_axes"]


def test_fail_gate_negation_contradicts_true_profile():
    """Profile: is_employer=True; the draft asserts the asker does NOT employ ⇒ contradiction ⇒ fail."""
    ev = {"profile_context": {"is_employer": True}}
    v = verify(_claim("Protože nezaměstnáváte žádné lidi, tato povinnost odpadá."), ev, _S_)
    assert v[0].status == "fail"
    mm = v[0].evidence["mismatches"][0]
    assert mm["axis"] == "is_employer" and mm["asserted"] is False and mm["record"] is True


def test_fail_personal_data_gate_contradiction():
    ev = {"profile_context": {"processes_personal_data": False}}
    v = verify(_claim("Jelikož zpracováváte osobní údaje, musíte plnit GDPR."), ev, _S_)
    assert v[0].status == "fail"
    assert v[0].evidence["gap_source"] == "vf_profile"


# ── determinism / purity ─────────────────────────────────────────────────────────────────────────────────
def test_deterministic_repeat():
    ev = {"profile_context": {"legal_form": "s.r.o."}}
    c = _claim("Jako a.s. máte povinnost.")
    a = verify(c, ev, _S_)
    b = verify(c, ev, _S_)
    assert a[0].status == b[0].status == "fail"
    assert a[0].evidence["mismatches"] == b[0].evidence["mismatches"]


def test_always_single_verdict():
    """V-PROF always returns exactly one verdict on every path (orchestrator-friendly)."""
    cases = [
        ({}, "Jako s.r.o. platí."),
        ({"profile_context": {"legal_form": "s.r.o."}}, "Jako s.r.o. platí."),
        ({"profile_context": {"legal_form": "s.r.o."}}, "Jako a.s. platí."),
        ({"profile_context": {"legal_form": "s.r.o."}}, "Poraďte se s odborníkem."),
    ]
    for ev, txt in cases:
        v = verify(_claim(txt), ev, _S_)
        assert len(v) == 1 and v[0].check == "v_prof"
