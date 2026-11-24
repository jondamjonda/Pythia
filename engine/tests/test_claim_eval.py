"""MP-G (T5) claim-loop eval harness tests — PURE (no DB, no engine, no LLM, no network).

Asserts:
  • the claim gold JSONL parses + validates (unique ids, claim types ∈ CLAIM_TYPE, provisional flag);
  • the pure metric scorers behave on hand-built edge cases (verified/fabricated/demoted/empty);
  • the DRY self-test (stub oracle over the embedded fixture) computes sane metrics;
  • the ledger→outcome mapper collapses the C3 FINAL_STATUS vocab to the harness verdicts correctly;
  • the OFF-loop invariant is respected elsewhere (this file only exercises the harness plumbing).

Gold labels are PROVISIONAL; the harness MEASURES, it does not certify.
"""

from __future__ import annotations

import pytest

from pythia.reasoning import claim_eval as ce


# ─────────────────────────────────────────────── gold + validation ────────────────────────────────────────
def test_gold_parses_and_validates():
    header, items = ce.load_gold()
    assert header.get("schema") == "claim_gold_v1"
    assert header.get("provisional") is True
    assert len(items) >= 4
    ids = [it["id"] for it in items]
    assert len(ids) == len(set(ids))
    # every claim type is from the C3 vocab and every fabricated flag is a bool (validate_gold ran already)
    for it in items:
        for c in it["claims"]:
            assert c["type"] in ce._CLAIM_TYPES
            assert isinstance(c["fabricated"], bool)


def test_validate_rejects_unknown_type():
    bad = [
        {
            "id": "x",
            "verified": False,
            "source": "draft",
            "draft": "d",
            "claims": [{"id": "c1", "type": "not_a_type", "fabricated": False}],
        }
    ]
    with pytest.raises(ValueError):
        ce.validate_gold(bad)


def test_validate_rejects_nonprovisional():
    bad = [
        {
            "id": "x",
            "verified": True,
            "source": "draft",
            "draft": "d",
            "claims": [{"id": "c1", "type": "deadline", "fabricated": False}],
        }
    ]
    with pytest.raises(ValueError):
        ce.validate_gold(bad)


def test_validate_rejects_duplicate_claim_ids():
    bad = [
        {
            "id": "x",
            "verified": False,
            "source": "draft",
            "draft": "d",
            "claims": [
                {"id": "c1", "type": "deadline", "fabricated": False},
                {"id": "c1", "type": "amount", "fabricated": False},
            ],
        }
    ]
    with pytest.raises(ValueError):
        ce.validate_gold(bad)


# ─────────────────────────────────────────────── pure metric scorers ──────────────────────────────────────
_ITEM = {
    "id": "t",
    "verified": False,
    "source": "draft",
    "draft": "d",
    "claims": [
        {"id": "c1", "type": "deadline", "fabricated": False},
        {"id": "c2", "type": "deadline", "fabricated": True},
        {"id": "c3", "type": "amount", "fabricated": False},
    ],
}


def test_precision_recall_perfect_oracle():
    oc = ce.ClaimOutcome(verdicts={"c1": "verified", "c2": "unverified", "c3": "verified"})
    assert ce.claim_precision(_ITEM, oc) == 1.0  # both verified (c1,c3) are truly true
    assert ce.claim_recall(_ITEM, oc) == 1.0  # both true claims (c1,c3) verified
    assert ce.fabrication_catch_rate(_ITEM, oc) == 1.0  # c2 (fabricated) not verified → caught


def test_precision_drops_when_fabrication_verified():
    # the loop wrongly VERIFIED the fabricated c2 → precision 2/3, catch-rate 0/1.
    oc = ce.ClaimOutcome(verdicts={"c1": "verified", "c2": "verified", "c3": "verified"})
    assert ce.claim_precision(_ITEM, oc) == pytest.approx(2 / 3)
    assert ce.fabrication_catch_rate(_ITEM, oc) == 0.0


def test_recall_drops_when_true_left_unverified():
    oc = ce.ClaimOutcome(verdicts={"c1": "verified", "c2": "unverified", "c3": "unverified"})
    assert ce.claim_recall(_ITEM, oc) == 0.5  # only c1 of the two true claims verified


def test_precision_none_when_nothing_verified():
    oc = ce.ClaimOutcome(verdicts={"c1": "unverified", "c2": "unverified", "c3": "unverified"})
    assert ce.claim_precision(_ITEM, oc) is None  # undefined over an empty prediction


def test_demote_and_repair_yield():
    oc = ce.ClaimOutcome(verdicts={"c1": "demoted", "c2": "unverified", "c3": "verified"}, repaired=["c1"])
    assert ce.demote_rate(_ITEM, oc) == pytest.approx(1 / 3)
    assert ce.repair_yield(_ITEM, oc) == 1.0  # the one demoted claim (c1) was repaired
    # with no repaired list the yield is honestly N/A (V1 has no repair lane)
    oc2 = ce.ClaimOutcome(verdicts={"c1": "demoted"})
    assert ce.repair_yield(_ITEM, oc2) is None


def test_score_item_rejects_bad_verdict():
    with pytest.raises(ValueError):
        ce.score_item(_ITEM, ce.ClaimOutcome(verdicts={"c1": "bogus"}))


# ─────────────────────────────────────────────── ledger → outcome mapping ─────────────────────────────────
def test_ledger_to_outcome_collapses_final_status():
    rows = [
        {"claim_id": "e1", "final": "verified"},
        {"claim_id": "e2", "final": "repaired_verified"},
        {"claim_id": "e3", "final": "demoted_gap"},
        {"claim_id": "e4", "final": "dropped_safety"},
        {"claim_id": "e5", "final": "dropped_unverifiable"},
        {"claim_id": "e6", "final": "shadow"},
        {"claim_id": "e7", "final": "shadow"},  # not in id_map → skipped
    ]
    id_map = {"e1": "g1", "e2": "g2", "e3": "g3", "e4": "g4", "e5": "g5", "e6": "g6"}
    oc = ce.ledger_to_outcome(rows, id_map)
    assert oc.verdicts == {
        "g1": "verified",
        "g2": "verified",
        "g3": "demoted",
        "g4": "dropped",
        "g5": "dropped",
        "g6": "unverified",
    }


# ─────────────────────────────────────────────── self-test plumbing ───────────────────────────────────────
def test_self_test_passes():
    report = ce.self_test()
    assert report["provisional"] is True
    assert report["overall"]["claim_precision"] == 1.0
    assert report["overall"]["repair_yield"] is None


def test_score_set_over_gold_runs():
    _, items = ce.load_gold()
    outcomes = [ce.stub_run_fn(it) for it in items]
    report = ce.score_set(items, outcomes)
    assert report["n"] == len(items)
    # the stub oracle verifies all true + catches all fabricated → precision/recall 1.0 where defined
    assert report["overall"]["claim_precision"] == 1.0
    assert report["overall"]["claim_recall"] == 1.0
    cr = report["overall"]["fabrication_catch_rate"]
    assert cr == 1.0
