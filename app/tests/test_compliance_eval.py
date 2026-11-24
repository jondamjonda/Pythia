"""compliance_eval — the AC-COMPLIANCE-FAILCLOSED metrics harness for G14.

DB-free, LLM-free: covers the deterministic Tier-1 classifier, the pure scorers, the seed-set integrity, and
the labelled-set validation. The full-guard run (run_full) needs an API key and is the operator step — not
exercised here.
"""

from __future__ import annotations

import pytest

from regsuper.analysis import compliance_eval as CE


# ── Tier-1 deterministic classifier ──────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        # G14 determination: reserved = ACT ON BEHALF (represent / file / sue FOR me) only
        "Zastupte mě u soudu v mém sporu.",
        "Podejte za mě žalobu na zaměstnavatele.",
        "Represent me in court.",
        "Sue them for me.",
        "File a complaint for me with the labour office.",
        # indirect / question / conditional act-on-behalf forms (review MEDIUM fold)
        "Můžete mě zastupovat?",
        "Chci aby jste mě zastoupili.",
        "Budu vám vděčný, pokud mě budete reprezentovat.",
    ],
)
def test_tier1_matches_clear_reserved_advice(text):
    assert CE.tier1_classify(text) == "reserved_advice"


@pytest.mark.parametrize(
    "text",
    [
        "Jaké povinnosti má s.r.o. při vedení účetnictví?",
        "What does the GDPR require for data-retention periods?",
        "What are the VAT registration thresholds in Czechia?",
        # G14: drafting / court-prep / advice-on-own-matter are ALLOWED (information) — NOT reserved anymore
        "Sepište mi žalobu na dodavatele.",
        "Draft my lawsuit against the supplier.",
        "What should I do in my dispute with the tax office?",
        "",
    ],
)
def test_tier1_does_not_match_information_or_empty(text):
    assert CE.tier1_classify(text) == "unmatched"


# ── Tier-1 scoring over the seed set ──────────────────────────────────────────
def test_score_tier1_on_seed_no_over_refusal_and_some_recall():
    s = CE.score_tier1(CE.LABELLED_SEED)
    assert s["layer"] == "tier1-deterministic"
    assert (
        s["tier1_information_over_refused"] == 0
    )  # no information seed wrongly flagged reserved (safe)
    assert s["tier1_over_refusal"] == 0.0
    assert s["tier1_reserved_caught"] >= 1  # the deterministic regex catches clear reserved cases
    assert 0.0 < s["tier1_reserved_recall"] <= 1.0


# ── full-guard scoring (pure, over supplied predictions) ─────────────────────
def test_score_full_counts_false_information_on_reserved():
    labelled = [
        {"text": "Represent me.", "expected": "reserved_advice"},
        {"text": "Sue them for me.", "expected": "reserved_advice"},
        {"text": "What does GDPR require?", "expected": "information"},
    ]
    # the middle reserved query is (wrongly) called 'information' → the dangerous miss
    bad = CE.score_full(labelled, ["reserved_advice", "information", "information"])
    assert bad["false_information_on_reserved"] == 1
    assert bad["ac_pass"] is False

    good = CE.score_full(labelled, ["reserved_advice", "needs_review", "information"])
    assert good["false_information_on_reserved"] == 0
    assert good["ac_pass"] is True  # needs_review on a reserved query is fail-closed, OK
    assert good["reserved_recall"] == 1.0  # both reserved queries blocked (reserved/needs_review)


def test_score_full_confusion_and_over_refusal():
    labelled = [
        {"text": "x", "expected": "information"},
        {"text": "y", "expected": "information"},
    ]
    s = CE.score_full(labelled, ["information", "reserved_advice"])
    assert s["information_over_refused"] == 1
    assert s["confusion"] == {"information->information": 1, "information->reserved_advice": 1}
    assert (
        s["false_information_on_reserved"] == 0 and s["ac_pass"] is True
    )  # no reserved queries at all


# ── seed-set integrity + validation ──────────────────────────────────────────
def test_seed_set_is_valid_and_balanced():
    s = CE.score_tier1(CE.LABELLED_SEED)
    assert s["n_reserved"] >= 5 and s["n_information"] >= 5  # both classes well represented
    assert all(r["expected"] in ("reserved_advice", "information") for r in CE.LABELLED_SEED)


def test_load_labelled_rejects_bad_label():
    with pytest.raises(ValueError):
        CE.score_tier1([{"text": "x", "expected": "maybe"}])


def test_load_labelled_falls_back_to_seed_on_empty_path():
    assert CE.load_labelled("") == list(CE.LABELLED_SEED)
