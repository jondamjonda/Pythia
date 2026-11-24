"""WS11 / B4 — aq_compare scored memo tests (PURE: no DB, no engine, no network).

Drives the memo generator with FIXTURE OFF/ON artifacts (the aq_answer_eval_v2 shape) and asserts:
  • the paired bootstrap Answer-Recall CI is deterministic (fixed seed) and brackets the point delta;
  • grounded no-regression flags an OFF-grounded item that goes un-grounded under ON (a promotion blocker);
  • the fabrication block surfaces the ON-arm fabrication_by_type when ledgers were folded in;
  • the memo markdown carries every required section (provenance, headline CI, no-regression, fabrication,
    per-class splits incl. zero_lexical, the DIRECTIONAL caveat).

RULES: run ONLY this file. NEVER bare `pytest tests/` (live-DB hang).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

import aq_compare as ac  # noqa: E402


def _agg(overall, by_class):
    return {"overall": overall, "by_class": by_class}


def _off_artifact():
    return {
        "schema": "aq_answer_eval_v2",
        "posture_tag": "off",
        "manifest": {
            "posture_tag": "off",
            "n_items": 3,
            "limit": 3,
            "corpus_snapshot": "pythia 658",
            "engine_git_sha": "aaa111",
            "app_git_sha": "bbb222",
            "endpoint": "http://127.0.0.1:8099/v1/answer",
            "rids": ["r1", "r2"],
        },
        "aggregate": _agg(
            {
                "n": 3,
                "recall@k": 0.30,
                "seed_recall@8": 0.25,
                "citation_precision": 0.90,
                "mode_match_rate": 0.60,
                "grounded_rate": 0.66,
                "practical_present_rate": 0.0,
                "refusal_correct_rate": 1.0,
                "errors": 0,
            },
            {
                "multi_obligation": {
                    "recall@k": 0.30,
                    "seed_recall@8": 0.25,
                    "citation_precision": 0.9,
                    "mode_match_rate": 0.5,
                    "grounded_rate": 0.5,
                    "practical_present_rate": 0.0,
                    "refusal_correct_rate": None,
                    "n": 2,
                },
                "zero_lexical": {
                    "recall@k": 0.0,
                    "seed_recall@8": 0.0,
                    "citation_precision": None,
                    "mode_match_rate": 0.0,
                    "grounded_rate": 0.0,
                    "practical_present_rate": 0.0,
                    "refusal_correct_rate": None,
                    "n": 1,
                },
            },
        ),
        "items": [
            {"id": "m1", "item_class": "multi_obligation", "recall": 0.5, "grounded": True},
            {"id": "m2", "item_class": "multi_obligation", "recall": 0.1, "grounded": True},
            {"id": "z1", "item_class": "zero_lexical", "recall": 0.0, "grounded": False},
        ],
    }


def _on_artifact(*, regress: bool = False, with_fab: bool = False):
    art = {
        "schema": "aq_answer_eval_v2",
        "posture_tag": "on",
        "manifest": {
            "posture_tag": "on",
            "n_items": 3,
            "limit": 3,
            "corpus_snapshot": "pythia 658",
            "engine_git_sha": "aaa111",
            "app_git_sha": "ccc333",
            "endpoint": "http://127.0.0.1:8099/v1/answer",
            "rids": ["r1", "r2", "r3"],
        },
        "aggregate": _agg(
            {
                "n": 3,
                "recall@k": 0.55,
                "seed_recall@8": 0.40,
                "citation_precision": 0.95,
                "mode_match_rate": 0.80,
                "grounded_rate": 0.66,
                "practical_present_rate": 0.66,
                "refusal_correct_rate": 1.0,
                "errors": 0,
            },
            {
                "multi_obligation": {
                    "recall@k": 0.55,
                    "seed_recall@8": 0.40,
                    "citation_precision": 0.95,
                    "mode_match_rate": 0.75,
                    "grounded_rate": 0.5,
                    "practical_present_rate": 0.5,
                    "refusal_correct_rate": None,
                    "n": 2,
                },
                "zero_lexical": {
                    "recall@k": 0.30,
                    "seed_recall@8": 0.30,
                    "citation_precision": 1.0,
                    "mode_match_rate": 1.0,
                    "grounded_rate": 1.0,
                    "practical_present_rate": 0.0,
                    "refusal_correct_rate": None,
                    "n": 1,
                },
            },
        ),
        "items": [
            {"id": "m1", "item_class": "multi_obligation", "recall": 0.8, "grounded": True},
            {"id": "m2", "item_class": "multi_obligation", "recall": 0.5, "grounded": (not regress)},
            {"id": "z1", "item_class": "zero_lexical", "recall": 0.3, "grounded": True},
        ],
    }
    if with_fab:
        art["fabrication"] = {
            "n_bundles": 55,
            "oracle": "live",
            "fabrication_by_type": {"amount": 1.0, "deadline": 1.0, "authority": None},
            "recheck_verdicts": {"grounded": 40, "fabricated": 0},
        }
    return art


# ── bootstrap CI ─────────────────────────────────────────────────────────────────────────────────────
def test_bootstrap_ci_deterministic_and_brackets_point():
    ci = ac.bootstrap_recall_delta_ci(_off_artifact(), _on_artifact())
    assert ci is not None
    assert ci["n_pairs"] == 3
    # point delta = mean of (0.8-0.5)+(0.5-0.1)+(0.3-0.0) = mean(0.3,0.4,0.3) = 0.3333
    assert ci["point_delta"] == round((0.3 + 0.4 + 0.3) / 3, 4)
    assert ci["ci95_low"] <= ci["point_delta"] <= ci["ci95_high"]
    # deterministic across two calls (fixed seed)
    ci2 = ac.bootstrap_recall_delta_ci(_off_artifact(), _on_artifact())
    assert (ci["ci95_low"], ci["ci95_high"]) == (ci2["ci95_low"], ci2["ci95_high"])


def test_bootstrap_ci_none_when_too_few_pairs():
    off = _off_artifact()
    off["items"] = [off["items"][0]]
    on = _on_artifact()
    on["items"] = [on["items"][0]]
    assert ac.bootstrap_recall_delta_ci(off, on) is None


# ── grounded no-regression ───────────────────────────────────────────────────────────────────────────
def test_no_regression_passes_when_no_grounding_lost():
    r = ac.grounded_no_regression(_off_artifact(), _on_artifact())
    assert r["pass"] is True
    assert r["n_regressed"] == 0
    # z1 went un-grounded → grounded under ON: a GAIN, not a regression
    assert "z1" in r["gained_ids"]


def test_no_regression_flags_lost_grounding():
    r = ac.grounded_no_regression(_off_artifact(), _on_artifact(regress=True))
    assert r["pass"] is False
    assert r["n_regressed"] == 1
    assert r["regressed_ids"] == ["m2"]  # m2 was grounded OFF, un-grounded ON


# ── fabrication summary ──────────────────────────────────────────────────────────────────────────────
def test_fabrication_summary_reads_on_arm_block():
    fab = ac._fabrication_summary(_off_artifact(), _on_artifact(with_fab=True))
    assert fab["on"]["n_bundles"] == 55
    assert fab["on"]["fabrication_by_type"]["amount"] == 1.0
    assert fab["off"]["fabrication_by_type"] is None  # OFF run folded no ledgers


# ── the full memo markdown ───────────────────────────────────────────────────────────────────────────
def test_memo_contains_all_sections():
    memo = ac.build_memo(_off_artifact(), _on_artifact(with_fab=True), generated_note="slice n=3")
    for needle in (
        "# AQ OFF-vs-ON scored comparison memo",
        "DIRECTIONAL",
        "## Run provenance",
        "## Headline — Answer-Recall Δ (bootstrap 95% CI)",
        "## Grounded no-regression",
        "## Fabrication-per-type",
        "## Metrics — overall",
        "## Metrics — per class split",
        "zero_lexical",
        "slice n=3",
    ):
        assert needle in memo, needle
    # the ON fabrication table row is rendered
    assert "| amount |" in memo
    # the no-regression verdict line is a PASS for the non-regressing fixture
    assert "**PASS**" in memo


def test_memo_reports_regression_fail():
    memo = ac.build_memo(_off_artifact(), _on_artifact(regress=True))
    assert "**FAIL**" in memo
    assert "m2" in memo


def test_memo_handles_missing_fabrication_and_small_ci():
    # no fabrication block + a 1-item slice → memo still renders (CI section says not-meaningful)
    off, on = _off_artifact(), _on_artifact()
    off["items"], on["items"] = [off["items"][0]], [on["items"][0]]
    memo = ac.build_memo(off, on)
    assert "No ledger bundles were folded" in memo
    assert "bootstrap CI is not meaningful" in memo
