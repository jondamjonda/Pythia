"""Generate the scored off-versus-on comparison memo from two answer-evaluation artifacts.

Reads a `deploy_off_*.json` and a `deploy_on_*.json` artifact (both written by aq_answer_eval.py)
and produces a markdown memo for deciding whether the answer-quality flags can be promoted. It
contains:

- the off-to-on delta of every metric;
- a paired bootstrap confidence interval on the headline answer-recall delta;
- the grounded no-regression check: items grounded with the flags off that lose grounding with them
  on (any such item blocks promotion);
- fabrication per claim type (target: zero), citation precision and refusal correctness;
- per-class splits, including zero_lexical, the hard retrieval test where the question shares no
  word with the text of the law.

The module is pure: apart from an optional generation note passed in, it only reads two JSON files
and returns a markdown string, so tests can check the whole memo offline with fixture artifacts.

Run (from engine/):
    python eval/aq_compare.py eval/artifacts/deploy_off_slice.json eval/artifacts/deploy_on_slice.json \
        --out eval/artifacts/deploy_compare_memo.md

The memo repeats the caveat that no gold label is lawyer-verified yet, so every delta is
directional, not certified.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

# Metric families in memo order. Answer recall is the headline metric and gets a bootstrap CI.
_METRICS = (
    ("recall@k", "Answer-Recall@k"),
    ("seed_recall@8", "Seed-Recall@8"),
    ("citation_precision", "Citation precision"),
    ("grounded_rate", "Grounded rate"),
    ("mode_match_rate", "Mode-match rate"),
    ("refusal_correct_rate", "Refusal correctness"),
    ("practical_present_rate", "Practical-present rate"),
)
_BOOTSTRAP_N = 2000
_BOOTSTRAP_SEED = 4  # fixed, so the same artifacts always give the same CI


def _load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _items_by_id(artifact: dict) -> dict[str, dict]:
    return {str(it.get("id")): it for it in (artifact.get("items") or []) if it.get("id")}


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def _delta(o: Any, n: Any) -> float | None:
    return round(n - o, 4) if (isinstance(o, (int, float)) and isinstance(n, (int, float))) else None


def _paired_recall_pairs(off: dict, on: dict) -> list[tuple[float, float]]:
    """Return (off recall, on recall) pairs for items present in both artifacts.

    Items without a defined recall (must_refuse items with no expected ids have recall None) are
    excluded. The bootstrap resamples these pairs, which respects the same-item design.
    """
    off_items, on_items = _items_by_id(off), _items_by_id(on)
    pairs: list[tuple[float, float]] = []
    for iid in sorted(set(off_items) & set(on_items)):
        o, n = off_items[iid].get("recall"), on_items[iid].get("recall")
        if isinstance(o, (int, float)) and isinstance(n, (int, float)):
            pairs.append((float(o), float(n)))
    return pairs


def bootstrap_recall_delta_ci(
    off: dict, on: dict, *, n_resamples: int = _BOOTSTRAP_N, seed: int = _BOOTSTRAP_SEED
) -> dict | None:
    """Return a paired bootstrap 95% CI on the mean answer-recall delta (on minus off).

    Resamples the per-item deltas with replacement, recomputes the mean each time and returns the
    2.5th and 97.5th percentiles. Uses a seeded stdlib RNG, so the result is deterministic. Returns
    None with fewer than 2 paired items, where a CI is meaningless.
    """
    pairs = _paired_recall_pairs(off, on)
    if len(pairs) < 2:
        return None
    deltas = [n - o for o, n in pairs]
    point = round(sum(deltas) / len(deltas), 4)
    rng = random.Random(seed)
    means: list[float] = []
    k = len(deltas)
    for _ in range(n_resamples):
        sample = [deltas[rng.randrange(k)] for _ in range(k)]
        means.append(sum(sample) / k)
    means.sort()
    lo = means[int(0.025 * n_resamples)]
    hi = means[min(int(0.975 * n_resamples), n_resamples - 1)]
    return {
        "n_pairs": len(pairs),
        "point_delta": point,
        "ci95_low": round(lo, 4),
        "ci95_high": round(hi, 4),
        "n_resamples": n_resamples,
        "seed": seed,
    }


def grounded_no_regression(off: dict, on: dict) -> dict:
    """Find items grounded with the flags off that are no longer grounded with them on.

    Any such item blocks promotion: enabling the answer-quality features must not take grounding
    away from an answer. Returns the count and ids of regressions, plus, for information, the items
    grounded only with the flags on.
    """
    off_items, on_items = _items_by_id(off), _items_by_id(on)
    shared = sorted(set(off_items) & set(on_items))
    regressed = [i for i in shared if off_items[i].get("grounded") and not on_items[i].get("grounded")]
    gained = [i for i in shared if not off_items[i].get("grounded") and on_items[i].get("grounded")]
    return {
        "n_shared": len(shared),
        "n_regressed": len(regressed),
        "regressed_ids": regressed,
        "n_gained": len(gained),
        "gained_ids": gained,
        "pass": len(regressed) == 0,
    }


def _fabrication_summary(off: dict, on: dict) -> dict:
    """Return the fabrication-per-type block of each artifact, if its run included claim ledgers.

    The target is zero fabrications for every claim type. Both arms are reported; the "on" arm is
    the one that decides whether claim verification can be enforced.
    """

    def _blk(a: dict) -> dict:
        fb = a.get("fabrication") or {}
        return {
            "n_bundles": fb.get("n_bundles"),
            "oracle": fb.get("oracle"),
            "fabrication_by_type": fb.get("fabrication_by_type"),
            "recheck_verdicts": fb.get("recheck_verdicts"),
        }

    return {"off": _blk(off), "on": _blk(on)}


def _metric_table(off: dict, on: dict, scope: str) -> list[str]:
    """Return a markdown table of off, on and delta values for one scope ('overall' or a class)."""
    o_agg = off["aggregate"]["overall"] if scope == "overall" else (off["aggregate"]["by_class"].get(scope) or {})
    n_agg = on["aggregate"]["overall"] if scope == "overall" else (on["aggregate"]["by_class"].get(scope) or {})
    rows = ["| Metric | OFF | ON | Δ |", "|---|---:|---:|---:|"]
    for key, label in _METRICS:
        o, n = o_agg.get(key), n_agg.get(key)
        rows.append(f"| {label} | {_fmt(o)} | {_fmt(n)} | {_fmt(_delta(o, n))} |")
    return rows


def build_memo(off: dict, on: dict, *, generated_note: str | None = None) -> str:
    """Assemble the scored markdown memo from the two artifacts (pure function)."""
    off_m, on_m = off.get("manifest") or {}, on.get("manifest") or {}
    ci = bootstrap_recall_delta_ci(off, on)
    noreg = grounded_no_regression(off, on)
    fab = _fabrication_summary(off, on)

    lines: list[str] = []
    lines.append("# AQ OFF-vs-ON scored comparison memo (WS11 / B4)")
    lines.append("")
    lines.append(
        "> **DIRECTIONAL — the gold labels are 0/100 lawyer-verified.** These deltas make the "
        "OFF-vs-ON comparison reproducible; a lawyer certifying the gold is what makes a promotion "
        "number admissible for the WS13 gate."
    )
    lines.append("")
    if generated_note:
        lines.append(f"_{generated_note}_")
        lines.append("")

    # Provenance
    lines.append("## Run provenance")
    lines.append("")
    lines.append("| | OFF arm | ON arm |")
    lines.append("|---|---|---|")
    for label, key in (
        ("posture_tag", "posture_tag"),
        ("n_items", "n_items"),
        ("limit", "limit"),
        ("corpus_snapshot", "corpus_snapshot"),
        ("engine_git_sha", "engine_git_sha"),
        ("app_git_sha", "app_git_sha"),
        ("endpoint", "endpoint"),
    ):
        lines.append(f"| {label} | {_fmt(off_m.get(key))} | {_fmt(on_m.get(key))} |")
    lines.append(f"| n_rids | {len(off_m.get('rids') or [])} | {len(on_m.get('rids') or [])} |")
    lines.append("")

    # Headline: answer-recall delta with bootstrap CI
    lines.append("## Headline — Answer-Recall Δ (bootstrap 95% CI)")
    lines.append("")
    if ci is None:
        lines.append(
            "_Fewer than 2 paired recall-defined items — a bootstrap CI is not meaningful for this "
            "slice. Run a larger --limit (the shadow-window protocol targets ≥100)._"
        )
    else:
        sign = "+" if ci["point_delta"] >= 0 else ""
        lines.append(f"- Paired items (recall-defined): **{ci['n_pairs']}**")
        lines.append(f"- Mean Answer-Recall Δ (ON − OFF): **{sign}{ci['point_delta']:.4f}**")
        lines.append(
            f"- 95% CI: **[{ci['ci95_low']:+.4f}, {ci['ci95_high']:+.4f}]** "
            f"({ci['n_resamples']} paired resamples, seed={ci['seed']})"
        )
        crosses = ci["ci95_low"] <= 0.0 <= ci["ci95_high"]
        lines.append(
            f"- CI {'CROSSES 0 (delta not distinguishable from noise on this slice)' if crosses else 'excludes 0'}."
        )
    lines.append("")

    # Grounded no-regression
    lines.append("## Grounded no-regression (OFF-grounded must stay grounded under ON)")
    lines.append("")
    lines.append(
        f"- Shared items: {noreg['n_shared']} · regressed (grounded→un-grounded): "
        f"**{noreg['n_regressed']}** · gained (un-grounded→grounded): {noreg['n_gained']}"
    )
    lines.append(
        f"- **{'PASS' if noreg['pass'] else 'FAIL'}** — "
        f"{'no OFF-grounded item lost grounding under ON.' if noreg['pass'] else 'regressions block promotion: ' + ', '.join(noreg['regressed_ids'])}"
    )
    lines.append("")

    # Fabrication per claim type (target: zero)
    lines.append("## Fabrication-per-type (target = 0 · independent oracle, step-72)")
    lines.append("")
    on_fab = fab["on"]
    if on_fab.get("fabrication_by_type") is None:
        lines.append(
            "_No ledger bundles were folded into the ON run (pass --ledger-dir to aq_answer_eval.py). "
            "Fabrication-per-type is the enforce-gate blocker — it must be computed from ≥50 exported "
            "ledgers before the WS13 gate._"
        )
    else:
        oracle = on_fab.get("oracle")
        lines.append(
            f"- ON arm: **{on_fab.get('n_bundles')}** bundles · oracle=`{oracle}` "
            f"{'(SKELETON — re-run with --ledger-live)' if oracle == 'fixture' else '(fresh full-text)'}"
        )
        lines.append("")
        lines.append("| Claim type | fabrication_catch_rate |")
        lines.append("|---|---:|")
        for t, v in (on_fab["fabrication_by_type"] or {}).items():
            lines.append(f"| {t} | {_fmt(v)} |")
    lines.append("")

    # Overall metric table
    lines.append("## Metrics — overall")
    lines.append("")
    lines.extend(_metric_table(off, on, "overall"))
    lines.append("")

    # Per-class splits, with zero_lexical called out
    classes = sorted(set(off["aggregate"]["by_class"]) | set(on["aggregate"]["by_class"]))
    lines.append("## Metrics — per class split")
    lines.append("")
    for cls in classes:
        note = (
            " — **zero-lexical: the hard 'no shared word with the law' retrieval test**"
            if cls == "zero_lexical"
            else ""
        )
        lines.append(f"### {cls}{note}")
        lines.append("")
        lines.extend(_metric_table(off, on, cls))
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        f"OFF artifact: `{off_m.get('endpoint', '?')}` · ON artifact: `{on_m.get('endpoint', '?')}`. "
        "Metrics defined in `eval/aq_answer_eval.py`; fabrication oracle in "
        "`pythia/eval/fabrication_check.py`."
    )
    return "\n".join(lines) + "\n"


def compare_files(off_path: str, on_path: str, *, generated_note: str | None = None) -> str:
    return build_memo(_load(off_path), _load(on_path), generated_note=generated_note)


def main() -> int:
    ap = argparse.ArgumentParser(description="Scored OFF-vs-ON comparison memo (markdown) for WS11/B4")
    ap.add_argument("off", help="deploy_off_*.json produced by aq_answer_eval.py")
    ap.add_argument("on", help="deploy_on_*.json produced by aq_answer_eval.py")
    ap.add_argument("--out", default=None, help="write the memo here (default: stdout)")
    ap.add_argument("--note", default=None, help="a free-text generation note stamped into the memo")
    args = ap.parse_args()
    memo = compare_files(args.off, args.on, generated_note=args.note)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(memo, encoding="utf-8")
        print(f"wrote memo → {args.out}")
    else:
        print(memo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
