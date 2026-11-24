"""WS11 / B4 — aq_answer_eval harness scoring/parse pins (PURE: no DB, no engine, no network).

Drives the pure scoring/parse bits with FIXTURE served-answer dicts (the /v1/answer response shape) so the
metric families are proven offline:
  • answer_recall@k over citations_map ∪ chips ∪ cited_provisions (cross-representation);
  • seed_recall@8 over the seed-marked returned provisions;
  • citation precision over the grounded map;
  • refusal correctness (must_refuse ⇒ correct iff refused; answer ⇒ correct iff not refused);
  • grounded no-regression proxy + mode_match + practical_present;
  • the manifest block (posture tag, gold pin, git SHAs, rid list) + build_artifact assembly with NO network.

RULES: run ONLY this file. NEVER bare `pytest tests/` (live-DB hang).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

import aq_answer_eval as ae  # noqa: E402


# ── fixture answers (the /v1/answer response shape) ──────────────────────────────────────────────────
def _answer_full():
    """A grounded FULL answer: cites 262/2006 §34 (grounded map + seed) and §35 (cited only), plus a practical
    block. One malformed map entry (no corpus) to exercise the precision denominator."""
    return {
        "refused": False,
        "verdict": "ok",
        "render_mode": "full",
        "citations_map": [
            {"corpus": "262/2006", "label": "§ 34"},
            {"corpus": "262/2006", "label": "§ 35"},
            {"corpus": "", "label": "no-act"},  # malformed → not counted as a parsed id
        ],
        "cited_provisions": [
            {"source_title": "262/2006", "label": "§ 34", "relation_path": ["seed"]},
            {"source_title": "262/2006", "label": "§ 35", "relation_path": ["seed", "cites"]},
            {"source_title": "187/2006", "label": "§ 92", "relation_path": ["cites"]},  # retrieved, not seed
        ],
        "practical": {"items": [{"fact_type": "deadline", "value": "do 8 dnů"}]},
        "audit_trace": {"request_id": "rid-abc-123"},
    }


def _answer_refuse():
    return {
        "refused": True,
        "verdict": "refuse",
        "render_mode": "refuse",
        "citations_map": [],
        "cited_provisions": [],
        "audit_trace": {"request_id": "rid-ref-9"},
    }


# ── the pure cite extractors ─────────────────────────────────────────────────────────────────────────
def test_map_and_cited_and_seed_extraction():
    a = _answer_full()
    assert ae._map_cites(a) == {"262/2006 § 34", "262/2006 § 35"}
    assert ae._cited_provision_cites(a) == {"262/2006 § 34", "262/2006 § 35", "187/2006 § 92"}
    # seed set = seed-marked returned provisions only
    assert ae._seed_cites(a) == {"262/2006 § 34", "262/2006 § 35"}
    # returned = union
    assert ae._returned_cites(a) == {"262/2006 § 34", "262/2006 § 35", "187/2006 § 92"}


def test_chip_cites_from_app_shape():
    app_ans = {"sections": [{"chips": [{"label": "[187/2006 § 93]"}]}], "chips": [{"label": "235/2004 § 6"}]}
    assert ae._chip_cites(app_ans) == {"187/2006 § 93", "235/2004 § 6"}


def test_eu_celex_and_article_normalization():
    a = {"citations_map": [{"corpus": "32014L0065", "label": "Article 27"}]}
    assert ae._map_cites(a) == {"32014L0065 Article 27"}


# ── scoring: answer item ─────────────────────────────────────────────────────────────────────────────
def test_score_answer_item_recall_seed_precision():
    item = {
        "id": "x1",
        "item_class": "multi_obligation",
        "expected_mode": "answer",
        "expected_provision_ids": ["262/2006 § 34", "262/2006 § 35", "999/9999 § 1"],
    }
    s = ae._score_item(item, _answer_full())
    # 2 of 3 expected are returned → recall 2/3
    assert s["recall"] == round(2 / 3, 4)
    # both hit provisions are seed-marked → seed_recall also 2/3
    assert s["seed_recall"] == round(2 / 3, 4)
    # 2 of 3 map entries parse to a legit id → precision 2/3
    assert s["citation_precision"] == round(2 / 3, 4)
    assert s["grounded"] is True
    assert s["mode_match"] is True  # full → answer == expected answer
    assert s["practical_present"] is True
    assert s["refused"] is False
    assert s["refusal_correct"] is True  # answer item, not refused → correct
    assert s["rid"] == "rid-abc-123"


def test_score_must_refuse_item_correct_when_refused():
    item = {"id": "r1", "item_class": "must_refuse", "expected_mode": "refuse", "expected_provision_ids": []}
    s = ae._score_item(item, _answer_refuse())
    assert s["must_refuse"] is True
    assert s["refused"] is True
    assert s["refusal_correct"] is True  # must_refuse & refused → correct
    assert s["recall"] is None  # no expected ids → recall undefined
    assert s["citation_precision"] is None  # nothing grounded → precision undefined


def test_score_must_refuse_item_wrong_when_answered():
    item = {"id": "r2", "item_class": "must_refuse", "expected_mode": "refuse", "expected_provision_ids": []}
    s = ae._score_item(item, _answer_full())  # served a full answer to a must-refuse
    assert s["refusal_correct"] is False


def test_score_answer_item_wrong_when_refused():
    item = {"id": "a2", "item_class": "amount", "expected_mode": "answer", "expected_provision_ids": ["285/2024 § 1"]}
    s = ae._score_item(item, _answer_refuse())
    assert s["refusal_correct"] is False  # answer item but refused → wrong
    assert s["grounded"] is False


def test_score_error_item_when_answer_none():
    item = {"id": "e1", "item_class": "amount", "expected_mode": "answer", "expected_provision_ids": ["285/2024 § 1"]}
    s = ae._score_item(item, None)
    assert s["error"] is True
    assert s["recall"] == 0.0 and s["grounded"] is False and s["served_mode"] == "ERROR"


def test_refused_detection_covers_band_and_verdict():
    # refuse-family band (no refused flag) still counts as refused
    assert ae._refused({"band": "closest"}) is True
    assert ae._refused({"verdict": "abstain"}) is True
    assert ae._refused({"render_mode": "full", "verdict": "ok"}) is False


# ── aggregate + manifest + build_artifact (no network) ───────────────────────────────────────────────
class _Args:
    posture_tag = "off"
    arm = "off"
    endpoint = "http://127.0.0.1:8099/v1/answer"
    gold = None
    limit = 6
    env_manifest = None
    ledger_dir = None
    ledger_live = False
    ledger_limit = 200


def test_build_artifact_shape_and_manifest():
    gold = [
        {
            "id": "x1",
            "item_class": "multi_obligation",
            "expected_mode": "answer",
            "expected_provision_ids": ["262/2006 § 34", "262/2006 § 35"],
        },
        {"id": "r1", "item_class": "must_refuse", "expected_mode": "refuse", "expected_provision_ids": []},
    ]
    scored = [ae._score_item(gold[0], _answer_full()), ae._score_item(gold[1], _answer_refuse())]
    art = ae.build_artifact(_Args(), gold, scored, {"multi_obligation", "must_refuse"})
    assert art["schema"] == "aq_answer_eval_v2"
    assert art["posture_tag"] == "off"
    agg = art["aggregate"]
    assert set(agg) == {"by_class", "overall"}
    ov = agg["overall"]
    # both refusal-correct items → refusal_correct_rate 1.0
    assert ov["refusal_correct_rate"] == 1.0
    for key in ("recall@k", "seed_recall@8", "citation_precision", "grounded_rate", "refusal_correct_rate"):
        assert key in ov
    man = art["manifest"]
    assert man["posture_tag"] == "off"
    assert man["n_items"] == 2
    assert man["limit"] == 6
    # rid list is harvested from the scored rows (only rid-carrying answers)
    assert "rid-abc-123" in man["rids"]
    # no ledger dir → no fabrication block
    assert "fabrication" not in art
    # artifact serializes cleanly (the on-disk write)
    json.dumps(art, ensure_ascii=False)


def test_load_gold_filters_header_and_classes():
    items = ae._load_gold({"amount"})
    assert len(items) >= 10
    assert all(it["item_class"] == "amount" for it in items)
    # header record is never yielded as an item
    assert all(it.get("question_cz") for it in items)


def test_gold_header_pins_corpus_snapshot():
    hdr = ae._gold_header()
    assert hdr.get("schema") == "gold_layman_v1"
    assert "658" in (hdr.get("corpus_snapshot") or "")
