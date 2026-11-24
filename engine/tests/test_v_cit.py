"""Track V / V1 — fast unit tests for the V-CIT verifier (VL-D-05). Pure/deterministic; no LLM/DB/fetch.

Covers: citation-presence fail + exempt-slot skip + non-normative skip; the lexical-support pass / fail /
ambiguous bands; diacritic-folded + 6-char-stemmed token matching; verbatim number handling; the ambiguous
entailment lane (flag-gated, verbatim-quote-gated, degrade-to-advisory when unwired); JSON-safe evidence.
"""

from __future__ import annotations

from types import SimpleNamespace

from pythia.reasoning.claim_ledger import Claim, ClaimVerdict
from pythia.reasoning.v_cit import (
    CLAIM_ENTAILMENT_PROMPT_HASH,
    _content_tokens,
    _is_verbatim_quote,
    _support_score,
    verify,
)


# ── fixtures / helpers ───────────────────────────────────────────────────────────────────────────────────
def _claim(text, *, slot="legislative", ctype="legal_rule", normative=True, chips=None):
    return Claim(
        id="c0",
        span=(0, len(text)),
        text=text,
        slot=slot,
        type=ctype,
        type_source="slot_default",
        chips=chips or [],
        normative=normative,
    )


def _chip(pid, start=0, end=5):
    return {"start": start, "end": end, "provision_id": pid, "label": "§ 1", "corpus": "pythia"}


def _settings(entailment=False):
    return SimpleNamespace(claim_entailment_llm_enabled=entailment)


def _only(verdicts):
    assert isinstance(verdicts, list) and len(verdicts) == 1
    v = verdicts[0]
    assert isinstance(v, ClaimVerdict)
    assert v.check == "v_cit"
    return v


# ── (i) CITATION PRESENCE ────────────────────────────────────────────────────────────────────────────────
def test_uncited_normative_claim_in_claim_slot_fails():
    c = _claim("Podnikatel musí splnit ohlašovací povinnost.", slot="legislative", chips=[])
    v = _only(verify(c, {"cited_texts": {}}, _settings()))
    assert v.status == "fail"
    assert v.reason == "uncited_normative_claim"


def test_uncited_claim_in_assessment_slot_is_exempt_skip():
    c = _claim("Ve vašem případě to znamená registraci.", slot="hodnoceni", ctype="situation_application", chips=[])
    v = _only(verify(c, {"cited_texts": {}}, _settings()))
    assert v.status == "skip"
    assert v.reason == "citation_exempt_slot"


def test_situation_slot_exempt_even_with_diacritics_in_slot_name():
    c = _claim("Vaše situace vyžaduje ohlášení.", slot="vase_situace", ctype="situation_application", chips=[])
    v = _only(verify(c, {"cited_texts": {}}, _settings()))
    assert v.status == "skip"
    assert v.reason == "citation_exempt_slot"


def test_gap_claim_is_exempt():
    c = _claim("K tomuto nemáme v korpusu podklad.", slot="legislative", ctype="gap", chips=[])
    v = _only(verify(c, {"cited_texts": {}}, _settings()))
    assert v.status == "skip"
    assert v.reason == "citation_exempt_slot"


def test_non_normative_claim_skips():
    c = _claim("## Legislativní rámec", slot="legislative", normative=False, chips=[])
    v = _only(verify(c, {"cited_texts": {}}, _settings()))
    assert v.status == "skip"
    assert v.reason == "non_normative_claim"


# ── (ii) SUPPORT bands ───────────────────────────────────────────────────────────────────────────────────
def test_supported_claim_passes():
    text = "Podnikatel musí ohlásit živnost živnostenskému úřadu."
    prov = (
        "Fyzická osoba, která hodlá provozovat živnost, je povinna ohlásit "
        "živnost živnostenskému úřadu; podnikatel tak činí písemně."
    )
    c = _claim(text, chips=[_chip("p1")])
    v = _only(verify(c, {"cited_texts": {"p1": prov}}, _settings()))
    assert v.status == "pass"
    assert v.reason == "cited_and_supported"
    assert v.evidence["score"] >= 0.5


def test_unsupported_claim_fails():
    text = "Sazba daně z příjmů právnických osob činí devatenáct procent."
    prov = "Zaměstnavatel zajistí školení bezpečnosti práce pro každého nového zaměstnance."
    c = _claim(text, chips=[_chip("p1")])
    v = _only(verify(c, {"cited_texts": {"p1": prov}}, _settings()))
    assert v.status == "fail"
    assert v.reason == "cited_but_unsupported"
    assert v.evidence["score"] < 0.2


def test_ambiguous_without_entailment_is_advisory():
    # ~1/3 of the content tokens overlap → lands in [0.2, 0.5)
    text = "Podnikatel ohlásí živnost a zaplatí správní poplatek tisíc korun."
    prov = "Fyzická osoba ohlásí živnost úřadu; jiné povinnosti se řídí zvláštním předpisem."
    c = _claim(text, chips=[_chip("p1")])
    score, _, _ = _support_score(text, [prov])
    assert 0.2 <= score < 0.5, f"fixture must be ambiguous, got {score}"
    v = _only(verify(c, {"cited_texts": {"p1": prov}}, _settings(entailment=False)))
    assert v.status == "advisory"
    assert v.reason == "unverified_support"


def test_diacritic_folding_matches_support():
    # claim uses ASCII-ish forms, provision uses full diacritics — folding must make them match
    text = "lhuta ohlaseni zivnosti"
    prov = "Lhůta ohlášení živnosti činí patnáct dní."
    score, _, matched = _support_score(text, [prov])
    assert score == 1.0
    assert matched == 3


def test_number_is_kept_verbatim_as_token():
    toks = _content_tokens("pokuta 50000 korun")
    assert "50000" in toks


# ── cited-but-text-unavailable → advisory (not a fail) ───────────────────────────────────────────────────
def test_cited_text_unavailable_is_advisory():
    c = _claim("Podnikatel musí ohlásit živnost.", chips=[_chip("pX")])
    v = _only(verify(c, {"cited_texts": {}}, _settings()))
    assert v.status == "advisory"
    assert v.reason == "cited_text_unavailable"
    assert v.evidence["missing"] == ["pX"]


def test_no_scorable_tokens_is_advisory():
    # a claim of only stopwords / single chars → no scorable content tokens
    c = _claim("a i o u v k.", chips=[_chip("p1")])
    v = _only(verify(c, {"cited_texts": {"p1": "Podnikatel ohlásí živnost."}}, _settings()))
    assert v.status == "advisory"
    assert v.reason == "no_scorable_tokens"


# ── ENTAILMENT lane (ambiguous band, flag-gated, verbatim-quote-gated) ───────────────────────────────────
_AMBIG_TEXT = "Podnikatel ohlásí živnost a zaplatí správní poplatek tisíc korun."
_AMBIG_PROV = "Fyzická osoba ohlásí živnost úřadu; jiné povinnosti se řídí zvláštním předpisem."


def test_entailment_supported_when_quote_is_verbatim():
    def fake_llm(prompt, batch):
        return ["ohlásí živnost úřadu"]  # a verbatim substring of the provision

    c = _claim(_AMBIG_TEXT, chips=[_chip("p1")])
    ev = {"cited_texts": {"p1": _AMBIG_PROV}, "entailment_llm": fake_llm}
    v = _only(verify(c, ev, _settings(entailment=True)))
    assert v.status == "pass"
    assert v.reason == "entailment_supported"
    assert v.evidence["prompt_hash"] == CLAIM_ENTAILMENT_PROMPT_HASH
    # lexical score folded in
    assert "score" in v.evidence


def test_entailment_rejects_hallucinated_quote():
    def fake_llm(prompt, batch):
        return ["povinnost zaplatit deset tisíc korun"]  # NOT in the provision text

    c = _claim(_AMBIG_TEXT, chips=[_chip("p1")])
    ev = {"cited_texts": {"p1": _AMBIG_PROV}, "entailment_llm": fake_llm}
    v = _only(verify(c, ev, _settings(entailment=True)))
    assert v.status == "fail"
    assert v.reason == "entailment_unsupported"


def test_entailment_empty_quote_is_unsupported():
    def fake_llm(prompt, batch):
        return [""]

    c = _claim(_AMBIG_TEXT, chips=[_chip("p1")])
    ev = {"cited_texts": {"p1": _AMBIG_PROV}, "entailment_llm": fake_llm}
    v = _only(verify(c, ev, _settings(entailment=True)))
    assert v.status == "fail"
    assert v.reason == "entailment_unsupported"


def test_entailment_flag_on_but_unwired_degrades_to_advisory():
    # flag ON but no 'entailment_llm' in evidence → the lane degrades to advisory (never a crash)
    c = _claim(_AMBIG_TEXT, chips=[_chip("p1")])
    v = _only(verify(c, {"cited_texts": {"p1": _AMBIG_PROV}}, _settings(entailment=True)))
    assert v.status == "advisory"
    assert v.reason == "unverified_support"


def test_entailment_callable_exception_degrades_to_advisory():
    def boom(prompt, batch):
        raise RuntimeError("llm down")

    c = _claim(_AMBIG_TEXT, chips=[_chip("p1")])
    ev = {"cited_texts": {"p1": _AMBIG_PROV}, "entailment_llm": boom}
    v = _only(verify(c, ev, _settings(entailment=True)))
    assert v.status == "advisory"
    assert v.reason == "entailment_error"


def test_entailment_not_run_outside_ambiguous_band():
    # a clearly-supported claim must PASS via the lexical lane, never invoking the (would-crash) llm
    def boom(prompt, batch):
        raise AssertionError("entailment must not run when lexical score already passes")

    text = "Podnikatel musí ohlásit živnost živnostenskému úřadu."
    prov = "Podnikatel je povinen ohlásit živnost živnostenskému úřadu písemně."
    c = _claim(text, chips=[_chip("p1")])
    ev = {"cited_texts": {"p1": prov}, "entailment_llm": boom}
    v = _only(verify(c, ev, _settings(entailment=True)))
    assert v.status == "pass"


# ── helper-level ─────────────────────────────────────────────────────────────────────────────────────────
def test_verbatim_quote_gate_folds_whitespace_and_diacritics():
    prov = "Lhůta   ohlášení\nživnosti činí patnáct dní."
    assert _is_verbatim_quote("lhuta ohlaseni zivnosti", prov)
    assert not _is_verbatim_quote("lhuta placeni dane", prov)
    assert not _is_verbatim_quote("", prov)


def test_evidence_is_json_safe():
    import json

    text = "Sazba daně z příjmů právnických osob činí devatenáct procent."
    prov = "Zaměstnavatel zajistí školení bezpečnosti práce."
    c = _claim(text, chips=[_chip("p1")])
    v = _only(verify(c, {"cited_texts": {"p1": prov}}, _settings()))
    json.dumps(v.evidence)  # must not raise


def test_multiple_provisions_union_scored():
    text = "Podnikatel ohlásí živnost a zaplatí poplatek."
    c = _claim(text, chips=[_chip("p1"), _chip("p2")])
    ev = {"cited_texts": {"p1": "Podnikatel ohlásí živnost.", "p2": "Zaplatí správní poplatek."}}
    v = _only(verify(c, ev, _settings()))
    assert v.status == "pass"
    assert set(v.evidence["provision_ids"]) == {"p1", "p2"}
