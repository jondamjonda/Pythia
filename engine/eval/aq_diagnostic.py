"""Per-query diagnostic of the answer and refusal path.

Sends each gold query to the engine's /v1/answer endpoint and records the full causal chain, so you
can see, per item and per item class, why an answer was refused or degraded:

  * no_answer_cause   the typed cause: grounding_floor, coverage_*, premise_false, intent_* or none
  * render_mode, refused
  * coverage verdict and confidence from the question gate
  * n_retrieved       did retrieval return any candidates?
  * n_intake_subq     did intake produce sub-questions?
  * n_citations       did the draft cite anything? (0 means grounding-floor territory)
  * drafted           did generation produce any prose at all?

The question it answers: are refusals uniformly `grounding_floor` (the model drafted an answer but
cited nothing, so the limit is the answer-drafting model), or are there real `coverage_*` misses or
queries with few retrieved candidates (a retrieval problem that code changes could fix)?

Run (from engine/, with the engine service running):
    .venv/bin/python eval/aq_diagnostic.py [--endpoint URL] [--gold FILE] [--limit N] [--out FILE]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections import Counter
from pathlib import Path


def _load(gold_path: str, limit: int) -> list[dict]:
    rows = []
    for line in Path(gold_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("question_cz") and r.get("item_class"):
            rows.append(r)
    return rows[:limit] if limit else rows


def _post(endpoint: str, query: str, timeout: float) -> dict | None:
    body = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)[:160]}


def _row(item: dict, ans: dict | None) -> dict:
    if not ans or ans.get("_error"):
        return {
            "id": item.get("id"),
            "cls": item.get("item_class"),
            "error": (ans or {}).get("_error", "none"),
            "cause": "ERROR",
            "render_mode": None,
        }
    at = ans.get("audit_trace") or {}
    meta = ans.get("meta") or at.get("meta") or {}
    intake = at.get("intake") or {}
    cause = ans.get("no_answer_cause") or at.get("no_answer_cause") or "none"
    cf = (
        at.get("metagate_coverage_features")
        or (meta.get("coverage_features") if isinstance(meta, dict) else None)
        or {}
    )
    return {
        "id": item.get("id"),
        "cls": item.get("item_class"),
        "cause": cause,
        "render_mode": ans.get("render_mode"),
        "refused": bool(ans.get("refused")),
        "coverage": meta.get("coverage"),
        "coverage_conf": meta.get("coverage_confidence"),
        "top_raw": cf.get("top_raw"),
        "n_strong": cf.get("n_strong"),
        "strong_cos": cf.get("strong_cos"),
        "support": cf.get("support"),
        "support_conf": cf.get("support_conf"),
        "cov_floor": cf.get("coverage_floor"),
        "route": meta.get("route"),
        "premise": meta.get("premise"),
        "n_retrieved": (
            at.get("retrieved") if isinstance(at.get("retrieved"), int) else len(at.get("retrieved") or [])
        ),
        "n_intake_subq": len(intake.get("sub_questions") or []) if isinstance(intake, dict) else 0,
        "n_citations": len(ans.get("citations_map") or []),
        "drafted": bool(ans.get("drafted_answer")),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="http://127.0.0.1:8091/v1/answer")
    ap.add_argument("--gold", default="eval/gold_subset20.jsonl")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--out", default="eval/artifacts/diagnostic.json")
    args = ap.parse_args()

    gold = _load(args.gold, args.limit)
    print(f"{len(gold)} items → {args.endpoint}", file=sys.stderr)
    rows = []
    for i, item in enumerate(gold, 1):
        r = _row(item, _post(args.endpoint, item["question_cz"], args.timeout))
        rows.append(r)
        print(
            f"  [{i}/{len(gold)}] {r['id']:<26} cause={r['cause']:<20} "
            f"mode={r.get('render_mode')} cov={r.get('coverage')} "
            f"n_ret={r.get('n_retrieved')} cites={r.get('n_citations')} "
            f"top_raw={r.get('top_raw')} strong_cos={r.get('strong_cos')} support={r.get('support')}/{r.get('support_conf')}",
            file=sys.stderr,
        )

    cause_overall = Counter(r["cause"] for r in rows)
    by_class: dict[str, dict] = {}
    for cls in sorted({r["cls"] for r in rows}):
        grp = [r for r in rows if r["cls"] == cls]
        by_class[cls] = {
            "n": len(grp),
            "causes": dict(Counter(r["cause"] for r in grp)),
            "mean_n_retrieved": round(sum(r.get("n_retrieved", 0) for r in grp) / len(grp), 1),
            "drafted_but_0_cite": sum(1 for r in grp if r.get("drafted") and r.get("n_citations") == 0),
        }
    art = {
        "endpoint": args.endpoint,
        "n": len(rows),
        "cause_overall": dict(cause_overall),
        "by_class": by_class,
        "rows": rows,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(art, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== CAUSE DISTRIBUTION (overall) ===")
    for c, n in cause_overall.most_common():
        print(f"  {c:<24} {n}")
    print("=== drafted-but-0-citation (grounding-floor / ollama-uncited signature) ===")
    print(f"  {sum(1 for r in rows if r.get('drafted') and r.get('n_citations') == 0)}/{len(rows)}")
    print("=== retrieval-miss signature (n_retrieved==0) ===")
    print(f"  {sum(1 for r in rows if r.get('n_retrieved') == 0)}/{len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
