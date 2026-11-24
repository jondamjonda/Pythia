"""Track V / V1 — fast unit tests for V-FACT (pythia/reasoning/v_fact.py, VL-D-06 verbatim-only).

PURE + FAST: no LLM, no DB, no fetch. Every case is a crafted (Claim, evidence) fixture. Covers the pass /
fail / skip verdicts, both grounding sources (a) cited full_txt and (b) pre-supplied practical_fact rows, the
whitespace-collapse-only normalisation (NEVER a numeric/date transform), the authority lexicon + the P3-
registry-locator fail, and stable reason codes + evidence shape.
"""

from __future__ import annotations

import pytest

from pythia.reasoning import v_fact
from pythia.reasoning.claim_ledger import Claim, ClaimVerdict


class _S:
    """A minimal settings stand-in (V-FACT ignores it on the pure path)."""

    claim_verify_enabled = True
    claim_verify_mode = "shadow"


def _claim(text: str, ctype: str, chips=None) -> Claim:
    return Claim(
        id="c0",
        span=(0, len(text)),
        text=text,
        slot="lhuty",
        type=ctype,
        type_source="regex",
        chips=chips or [],
        normative=True,
    )


def _one(verdicts: list[ClaimVerdict]) -> ClaimVerdict:
    assert isinstance(verdicts, list) and len(verdicts) == 1
    v = verdicts[0]
    assert isinstance(v, ClaimVerdict)
    assert v.check == "v_fact"
    return v


# ─────────────────────────────── PASS cases (source a: cited full_txt) ────────────────────────────────────


def test_pass_amount_token_verbatim_in_full_txt():
    claim = _claim("Pokuta az 1 000 000 Kc lze ulozit.", "amount", chips=[{"provision_id": "P1", "start": 0, "end": 5}])
    ev = {"cited_texts": {"P1": "Za spravni delikt lze ulozit pokutu az do 1 000 000 Kc."}}
    v = _one(v_fact.verify(claim, ev, _S()))
    assert v.status == "pass"
    assert v.reason == "fact_tokens_grounded"
    assert "1 000 000 Kc" in v.evidence["tokens"]
    assert v.evidence["searched_pids"] == ["P1"]


def test_pass_deadline_token_verbatim_in_full_txt():
    # The deadline VALUE ('do 15 dnů') is verbatim in the cited provision's full text → pass.
    claim = _claim("Oznámení podejte do 15 dnů.", "deadline", chips=[{"provision_id": "P9", "start": 0, "end": 4}])
    ev = {"cited_texts": {"P9": "Podání se podává ve lhůtě do 15 dnů ode dne rozhodnutí."}}
    v = _one(v_fact.verify(claim, ev, _S()))
    assert v.status == "pass"


def test_pass_via_pid_fallback_when_no_chips():
    # No chips → the search space falls back to every cited provision in the bundle.
    claim = _claim("Sazba pokuty az 50000 Kc.", "amount", chips=[])
    ev = {"cited_texts": {"PA": "irrelevant", "PB": "pokuta az 50000 Kc podle zakona"}}
    v = _one(v_fact.verify(claim, ev, _S()))
    assert v.status == "pass"
    assert set(v.evidence["searched_pids"]) == {"PA", "PB"}


# ─────────────────────────────── PASS via source (b): pre-supplied fact rows ───────────────────────────────


def test_pass_via_practical_fact_value_raw_dict():
    claim = _claim("Pokuta az 25000 Kc.", "amount", chips=[{"provision_id": "P2", "start": 0, "end": 6}])
    # full_txt does NOT contain the amount; a practical_fact.value_raw does → source (b) grounds it.
    ev = {
        "cited_texts": {"P2": "Text bez castky."},
        "fact_rows": {"P2": [{"fact_type": "penalty", "value_raw": "az 25000 Kc"}]},
    }
    v = _one(v_fact.verify(claim, ev, _S()))
    assert v.status == "pass"


def test_pass_via_practical_fact_value_raw_factrow_object():
    class _Row:
        value_raw = "do 30 dnů"

    claim = _claim("Podejte to do 30 dnů.", "deadline", chips=[{"provision_id": "P3", "start": 0, "end": 7}])
    ev = {"cited_texts": {"P3": "neutrální text"}, "fact_rows": {"P3": [_Row()]}}
    v = _one(v_fact.verify(claim, ev, _S()))
    assert v.status == "pass"


def test_source_b_absent_is_skipped_not_failed():
    # No fact_rows map at all AND full_txt grounds the token → still a PASS (b skipped, not failed).
    claim = _claim("do 30 dnů", "deadline", chips=[{"provision_id": "P4", "start": 0, "end": 2}])
    ev = {"cited_texts": {"P4": "lhůta do 30 dnů ode dne"}}
    v = _one(v_fact.verify(claim, ev, _S()))
    assert v.status == "pass"


# ─────────────────────────────── whitespace-collapse ONLY (never numeric/date) ────────────────────────────


def test_whitespace_collapse_matches():
    # token has single spaces; source has a newline + double space run → collapse makes them equal.
    claim = _claim("Pokuta 1 000 Kc.", "amount", chips=[{"provision_id": "P5", "start": 0, "end": 6}])
    ev = {"cited_texts": {"P5": "pokuta\n1   000    Kc"}}
    v = _one(v_fact.verify(claim, ev, _S()))
    assert v.status == "pass"


def test_numeric_transform_never_applied():
    # source says 3000 Kc; claim token is 30000 Kc — NOT a substring, must NOT match (no numeric transform).
    claim = _claim("Pokuta 30000 Kc.", "amount", chips=[{"provision_id": "P6", "start": 0, "end": 6}])
    ev = {"cited_texts": {"P6": "pokuta 3000 Kc"}}
    v = _one(v_fact.verify(claim, ev, _S()))
    assert v.status == "fail"
    assert v.reason == "fact_token_not_in_source"


# ─────────────────────────────── FAIL: fact_token_not_in_source ───────────────────────────────────────────


def test_fail_token_absent_evidence_shape():
    claim = _claim("Pokuta az 999999 Kc.", "amount", chips=[{"provision_id": "P7", "start": 0, "end": 6}])
    ev = {"cited_texts": {"P7": "Text ktery castku vubec neobsahuje."}}
    v = _one(v_fact.verify(claim, ev, _S()))
    assert v.status == "fail"
    assert v.reason == "fact_token_not_in_source"
    assert v.evidence["searched_pids"] == ["P7"]
    assert "999999 Kc" in v.evidence["expected"]


# ─────────────────────────────── FAIL: authority_only_in_registry (P3 registry) ───────────────────────────


def test_fail_authority_only_in_registry():
    # A concrete branch/place locator the statute names only generically → resolvable ONLY via the P3
    # gazetteer. The extracted token ('zivnostensky urad') is NOT in the cite and NOT in the lexicon, and the
    # claim carries the locator qualifier ('pro okres Brno') → registry fail (V-FACT must not invent a branch).
    claim = _claim(
        "Obratte se na zivnostensky urad pro okres Brno.",
        "authority",
        chips=[{"provision_id": "P8", "start": 0, "end": 8}],
    )
    ev = {"cited_texts": {"P8": "Prislusny organ rozhoduje o veci."}}  # no office name quoted
    v = _one(v_fact.verify(claim, ev, _S()))
    assert v.status == "fail"
    assert v.reason == "authority_only_in_registry"
    assert v.evidence["searched_pids"] == ["P8"]
    assert "expected" in v.evidence


def test_authority_in_source_passes_even_with_locator_qualifier():
    # If the authority token IS quoted in the cited full_txt, it grounds (source wins over the registry path).
    claim = _claim(
        "Obratte se na zivnostensky urad pro okres Brno.",
        "authority",
        chips=[{"provision_id": "P8b", "start": 0, "end": 8}],
    )
    ev = {"cited_texts": {"P8b": "Ohlaseni prijima zivnostensky urad."}}
    v = _one(v_fact.verify(claim, ev, _S()))
    assert v.status == "pass"


def test_authority_lexicon_but_not_in_cite_fails_as_missing_not_registry():
    # 'Ceska narodni banka' is in the inflection lexicon but NOT quoted in the cite and NO P3 locator qualifier
    # → a plain missing token (NOT authority_only_in_registry).
    claim = _claim(
        "Dohled vykonava Ceska narodni banka.", "authority", chips=[{"provision_id": "P8c", "start": 0, "end": 6}]
    )
    ev = {"cited_texts": {"P8c": "Text bez nazvu organu."}}
    v = _one(v_fact.verify(claim, ev, _S()))
    assert v.status == "fail"
    assert v.reason == "fact_token_not_in_source"


# ─────────────────────────────── SKIP cases ───────────────────────────────────────────────────────────────


def test_skip_non_fact_claim_type():
    claim = _claim("Toto je pravni pravidlo.", "legal_rule", chips=[{"provision_id": "P", "start": 0, "end": 4}])
    v = _one(v_fact.verify(claim, {"cited_texts": {"P": "x"}}, _S()))
    assert v.status == "skip"
    assert v.reason == "not_a_fact_claim"


def test_skip_no_fact_token_extracted():
    # type=amount but the text carries NO amount token the atomize regex recognises ('21 %' has a space).
    claim = _claim("Sazba je 21 %.", "amount", chips=[{"provision_id": "P", "start": 0, "end": 5}])
    v = _one(v_fact.verify(claim, {"cited_texts": {"P": "sazba 21 %"}}, _S()))
    assert v.status == "skip"
    assert v.reason == "no_fact_token"


def test_skip_no_cited_provision():
    claim = _claim("Pokuta az 1000 Kc.", "amount", chips=[])
    v = _one(v_fact.verify(claim, {"cited_texts": {}}, _S()))
    assert v.status == "skip"
    assert v.reason == "no_cited_provision"
    assert "1000 Kc" in v.evidence["expected"]


# ─────────────────────────────── purity / determinism ─────────────────────────────────────────────────────


def test_deterministic_repeat():
    claim = _claim("Pokuta az 1 000 000 Kc.", "amount", chips=[{"provision_id": "P1", "start": 0, "end": 5}])
    ev = {"cited_texts": {"P1": "pokuta az 1 000 000 Kc"}}
    a = v_fact.verify(claim, ev, _S())
    b = v_fact.verify(claim, ev, _S())
    assert a[0].status == b[0].status == "pass"
    assert a[0].evidence == b[0].evidence


def test_evidence_is_json_safe():
    import json

    claim = _claim("Pokuta az 42 Kc neni v textu.", "amount", chips=[{"provision_id": "P", "start": 0, "end": 6}])
    v = _one(v_fact.verify(claim, {"cited_texts": {"P": "nic"}}, _S()))
    json.dumps(v.evidence)  # must not raise


def test_returns_exactly_one_verdict_list():
    claim = _claim("do 30 dnu", "deadline", chips=[{"provision_id": "P", "start": 0, "end": 2}])
    out = v_fact.verify(claim, {"cited_texts": {"P": "lhuta do 30 dnu"}}, _S())
    assert isinstance(out, list) and len(out) == 1


@pytest.mark.parametrize("ctype", ["deadline", "amount", "authority"])
def test_all_fact_types_route_to_a_verdict(ctype):
    claim = _claim("neutralni veta bez tokenu", ctype, chips=[{"provision_id": "P", "start": 0, "end": 3}])
    v = _one(v_fact.verify(claim, {"cited_texts": {"P": "x"}}, _S()))
    # no extractable token for a neutral sentence → conservative skip (never a silent pass).
    assert v.status == "skip"
    assert v.reason == "no_fact_token"
