"""AQ-03 layman-gold harness tests — PURE (no DB, no engine, no network).

Asserts:
  • the gold JSONL parses + validates and every expected_provision_id is well-formed;
  • the AQ-03 v1.1 shape holds: exactly 100 draft items with every mandatory class >=10 (multi_obligation,
    zero_lexical, amount, must_refuse), and the header class_counts match the file;
  • the metric functions return SANE values on the stub fixture (self-test), and the pure scorers behave
    on hand-built edge cases (partial recall, cluster gaps, off-gold citations, wrong refusal).

These do NOT touch the live DB — grounding of the provision ids against the live pythia corpus was done at
authoring time; the harness/tests exercise the plumbing so CI stays offline. Gold labels are PROVISIONAL.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

import intake_eval as ie  # noqa: E402


def test_gold_parses_and_validates():
    header, items = ie.load_gold()
    assert header.get("schema") == "gold_layman_v1"
    assert header.get("provisional") is True
    assert "658" in header.get("corpus_snapshot", ""), "corpus snapshot must pin the 658-doc pin"
    # AQ-03 v1.1: extended to 100 draft items
    assert len(items) == 100, f"gold v1.1 must have exactly 100 items, got {len(items)}"
    assert header.get("n_items") == 100, "header must pin n_items=100"
    # validate_gold ran inside load_gold; ids are unique
    ids = [it["id"] for it in items]
    assert len(ids) == len(set(ids))


def test_every_provision_id_wellformed():
    _, items = ie.load_gold()
    for it in items:
        for pid in it.get("expected_provision_ids", []):
            assert ie._PROVISION_RE.match(pid), f"{it['id']}: malformed {pid!r}"


def test_expected_facts_reference_gold_provisions():
    _, items = ie.load_gold()
    for it in items:
        pids = set(it.get("expected_provision_ids", []))
        for f in it.get("expected_facts", []):
            assert f["provision"] in pids, f"{it['id']}: fact provision {f['provision']} not in gold ids"


def test_all_labels_provisional():
    _, items = ie.load_gold()
    assert all(it.get("verified") is False for it in items), "every draft label must be verified:false"
    assert all(it.get("source") == "draft" for it in items)


def test_mandatory_class_minima_met():
    _, items = ie.load_gold()
    counts = ie.assert_class_minima(items)  # raises if any minimum unmet
    # AQ-03 v1.1 contract: all FOUR mandatory classes carry >= 10 items each
    for cls in ("multi_obligation", "zero_lexical", "amount", "must_refuse"):
        assert counts[cls] >= 10, f"class {cls} below floor: {counts[cls]}"
    # and the class counts pinned in the header match the actual file
    header, _ = ie.load_gold()
    assert header.get("class_counts") == counts, "header class_counts must match the actual per-class counts"


def test_must_refuse_items_have_no_gold_provisions():
    _, items = ie.load_gold()
    for it in items:
        if it["item_class"] == "must_refuse":
            assert not it.get("expected_provision_ids"), it["id"]
            assert it["expected_mode"] == "refuse"


def test_multi_obligation_items_carry_clusters():
    _, items = ie.load_gold()
    multi = [it for it in items if it["item_class"] == "multi_obligation"]
    assert len(multi) >= 15
    # at least the bulk carry clusters so per-cluster coverage can fire (clarify items may be sparse)
    with_clusters = [it for it in multi if it.get("expected_clusters")]
    assert len(with_clusters) >= 15


def test_selftest_returns_sane_metrics():
    report = ie.self_test()
    assert report["provisional"] is True
    assert report["n"] == 3
    overall = report["overall"]
    # the stub is a perfect predictor -> defined metrics are 1.0
    assert overall["seed_recall@20"] == 1.0
    assert overall["answer_recall@20"] == 1.0
    assert overall["cluster_coverage"] == 1.0
    assert overall["citation_precision"] == 1.0
    assert overall["refusal_correct"] == 1.0


def test_validate_rejects_non_provisional():
    bad = [
        {
            "id": "x",
            "item_class": "amount",
            "question_cz": "q",
            "expected_mode": "answer",
            "expected_provision_ids": ["1/2020 § 1"],
            "verified": True,
        }
    ]
    with pytest.raises(ValueError, match="PROVISIONAL"):
        ie.validate_gold(bad)


def test_validate_rejects_malformed_provision_id():
    bad = [
        {
            "id": "x",
            "item_class": "amount",
            "question_cz": "q",
            "expected_mode": "answer",
            "expected_provision_ids": ["not-an-id"],
            "verified": False,
        }
    ]
    with pytest.raises(ValueError, match="malformed"):
        ie.validate_gold(bad)


def test_validate_rejects_must_refuse_with_gold():
    bad = [
        {
            "id": "x",
            "item_class": "must_refuse",
            "question_cz": "q",
            "expected_mode": "refuse",
            "expected_provision_ids": ["1/2020 § 1"],
            "verified": False,
        }
    ]
    with pytest.raises(ValueError, match="must_refuse"):
        ie.validate_gold(bad)


def test_seed_recall_partial():
    item = {
        "id": "p",
        "item_class": "amount",
        "question_cz": "q",
        "expected_mode": "answer",
        "expected_provision_ids": ["1/2020 § 1", "1/2020 § 2"],
        "verified": False,
    }
    pred = ie.Prediction(mode="answer", seed_ids=["1/2020 § 1", "9/9 § 9"])
    assert ie.seed_recall_at_k(item, pred, 20) == 0.5


def test_seed_recall_none_when_no_gold():
    item = {"item_class": "must_refuse", "expected_provision_ids": []}
    assert ie.seed_recall_at_k(item, ie.Prediction(mode="refuse")) is None


def test_seed_recall_respects_k():
    item = {"expected_provision_ids": ["a § 1"]}
    pred = ie.Prediction(mode="answer", seed_ids=["z § 9"] * 15 + ["a § 1"])  # gold at rank 16
    assert ie.seed_recall_at_k(item, pred, 10) == 0.0
    assert ie.seed_recall_at_k(item, pred, 20) == 1.0


def test_cluster_coverage_partial_and_na():
    item = {"item_class": "multi_obligation", "expected_clusters": ["a", "b"]}
    pred = ie.Prediction(mode="answer", cited_ids=["1/1 § 1"], cited_clusters={"1/1 § 1": "a"})
    assert ie.cluster_coverage(item, pred) == 0.5
    # no clusters -> N/A
    assert ie.cluster_coverage({"expected_clusters": []}, pred) is None
    # unmapped citations -> 0 coverage (measured honestly)
    unmapped = ie.Prediction(mode="answer", cited_ids=["1/1 § 1"])
    assert ie.cluster_coverage(item, unmapped) == 0.0


def test_citation_precision():
    item = {"expected_provision_ids": ["a § 1", "a § 2"]}
    pred = ie.Prediction(mode="answer", cited_ids=["a § 1", "off § 1"])  # 1 of 2 cited is gold
    assert ie.citation_precision(item, pred) == 0.5
    assert ie.citation_precision(item, ie.Prediction(mode="answer")) is None  # nothing cited -> N/A


def test_refusal_correct_matrix():
    refuse_item = {"expected_mode": "refuse"}
    assert ie.refusal_correct(refuse_item, ie.Prediction(mode="refuse")) == 1.0
    assert ie.refusal_correct(refuse_item, ie.Prediction(mode="answer")) == 0.0
    clarify_item = {"expected_mode": "clarify"}
    assert ie.refusal_correct(clarify_item, ie.Prediction(mode="clarify")) == 1.0
    # answer items are not scored by refusal
    assert ie.refusal_correct({"expected_mode": "answer"}, ie.Prediction(mode="answer")) is None


def test_score_set_parallel_guard():
    _, items = ie.load_gold()
    with pytest.raises(ValueError, match="parallel"):
        ie.score_set(items, [ie.Prediction(mode="answer")])  # wrong length


def test_score_set_over_full_gold_with_stub():
    """End-to-end: the harness imports + parses the real gold file and computes metrics on a stub prediction,
    proving the plumbing works on the ACTUAL delivered gold set (not just the tiny fixture)."""
    _, items = ie.load_gold()
    preds = [ie.stub_run_fn(it) for it in items]
    report = ie.score_set(items, preds)
    assert report["n"] == len(items)
    # perfect stub -> where defined, recall/precision/refusal are 1.0 overall
    assert report["overall"]["seed_recall@20"] == 1.0
    assert report["overall"]["citation_precision"] == 1.0
    assert report["overall"]["refusal_correct"] == 1.0
    # cluster_coverage defined only via multi_obligation class bucket
    assert report["by_class"]["multi_obligation"]["cluster_coverage"] == 1.0
    assert report["by_class"]["must_refuse"]["seed_recall@20"] is None  # no gold -> N/A
