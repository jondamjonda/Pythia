"""Evaluation harness for the intake stage against the layman gold set.

Scores the intake answer path (question, retrieved provisions, then answer, clarify or refuse)
against the layman gold set ``eval/gold_layman_v1.jsonl``. It follows the structure of
``regsuper.analysis.compliance_eval``:

- the metric functions are pure: they take gold items and a prediction and return numbers, so they
  can be unit-tested with a stub prediction and no running engine;
- the engine is reached only through a pluggable ``run_fn`` (an async callable from item to
  ``Prediction``), so the same scorers run over a stub fixture (self-test) or the live engine;
- the gold labels are provisional (``verified: false``, ``source: draft``) until a lawyer reviews
  them, so the harness measures but does not certify. It is read-only and never changes engine
  behaviour.

Metric functions:

- ``seed_recall_at_k``: share of gold provision ids found in the retriever's seed list (before the
  answer is drafted).
- ``answer_recall_at_k``: share of gold provision ids found among the answer's cited provisions.
- ``cluster_coverage``: for multi-obligation items, share of expected obligation clusters with at
  least one cited provision mapped to them (needs a cited-to-cluster map; 0 when unmapped).
- ``citation_precision``: share of the answer's cited provisions that are in the gold set.
- ``refusal_correct``: for items expected to refuse or clarify, whether the predicted mode matched.

``score_set`` aggregates per item_class and overall, and keeps track of which metrics apply to
which class (for example cluster_coverage only to multi_obligation, refusal only to refuse and
clarify items). ``self_test`` runs the whole pipeline over a 3-item fixture with a stub ``run_fn``,
checking that the gold is loaded and parsed and that the metrics are sane, without an engine.

Run (from engine/):
    python eval/intake_eval.py [--selftest | --live | --multileg] [--set eval/gold_layman_v1.jsonl]
"""

from __future__ import annotations

import json
import pathlib
import re
import statistics as st
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

_SET = pathlib.Path(__file__).parent / "gold_layman_v1.jsonl"

_MODES = frozenset({"answer", "clarify", "refuse"})
_ITEM_CLASSES = frozenset({"multi_obligation", "zero_lexical", "amount", "must_refuse"})
# Minimum items per mandatory class, so that every class is large enough to be useful on its own.
_MANDATORY_MINIMA = {"multi_obligation": 10, "zero_lexical": 10, "amount": 10, "must_refuse": 10}
_PROVISION_RE = re.compile(r"^\S+ § \S+")  # "<external_id> § <label>", independent of database ids


# Prediction: what a run_fn (stub or live engine adapter) returns for one gold item.
@dataclass
class Prediction:
    """A run's output for one gold item, using the gold's '<external_id> § <label>' ids.

    ``mode`` is the decision (answer, clarify or refuse). ``seed_ids`` is the ranked retriever seed
    list. ``cited_ids`` are the provisions the answer actually cited. ``cited_clusters`` optionally
    maps each cited id to the obligation cluster it satisfies, so cluster_coverage can be computed;
    without it, cluster_coverage is 0 (measured, never assumed).
    """

    mode: str
    seed_ids: list[str] = field(default_factory=list)
    cited_ids: list[str] = field(default_factory=list)
    cited_clusters: dict[str, str] = field(default_factory=dict)


RunFn = Callable[[dict[str, Any]], Awaitable[Prediction]]


# Loading and validating the gold set.
def load_gold(path: str | pathlib.Path = _SET) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load and validate the gold JSONL file and return (header, items).

    The first line is the header record (schema, corpus_snapshot and the provisional flag); every
    following line is an item.
    """
    lines = [ln for ln in pathlib.Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        raise ValueError(f"gold set {path} is empty")
    header = json.loads(lines[0])
    items = [json.loads(ln) for ln in lines[1:]]
    validate_gold(items)
    return header, items


def validate_gold(items: list[dict[str, Any]]) -> None:
    """Validate the structure of the gold items; raise ValueError on the first problem.

    Checks unique ids, known item_class and mode, well-formed provision ids, the provisional flag,
    and that every expected_fact references a provision listed in expected_provision_ids.
    """
    seen: set[str] = set()
    for i, it in enumerate(items):
        iid = it.get("id")
        if not isinstance(iid, str) or not iid:
            raise ValueError(f"item[{i}] missing string 'id'")
        if iid in seen:
            raise ValueError(f"duplicate item id {iid!r}")
        seen.add(iid)
        if it.get("item_class") not in _ITEM_CLASSES:
            raise ValueError(f"item {iid}: item_class must be in {sorted(_ITEM_CLASSES)}, got {it.get('item_class')!r}")
        if it.get("expected_mode") not in _MODES:
            raise ValueError(f"item {iid}: expected_mode must be in {sorted(_MODES)}, got {it.get('expected_mode')!r}")
        if not isinstance(it.get("question_cz"), str) or not it["question_cz"].strip():
            raise ValueError(f"item {iid}: missing non-empty question_cz")
        if it.get("verified") is not False:
            raise ValueError(f"item {iid}: every label must be PROVISIONAL (verified:false) in the draft set")
        pids = it.get("expected_provision_ids", [])
        if not isinstance(pids, list):
            raise ValueError(f"item {iid}: expected_provision_ids must be a list")
        for pid in pids:
            if not isinstance(pid, str) or not _PROVISION_RE.match(pid):
                raise ValueError(f"item {iid}: malformed provision id {pid!r} (want '<external_id> § <label>')")
        # A must_refuse item has no gold provisions: there is nothing correct to cite.
        if it["item_class"] == "must_refuse" and pids:
            raise ValueError(f"item {iid}: must_refuse items must have empty expected_provision_ids")
        for f in it.get("expected_facts", []):
            prov = f.get("provision")
            if prov not in pids:
                raise ValueError(f"item {iid}: expected_fact provision {prov!r} not in expected_provision_ids")


def assert_class_minima(items: list[dict[str, Any]]) -> dict[str, int]:
    """Return the per-class counts; raise AssertionError if a mandatory class is below its minimum."""
    counts = {c: sum(1 for it in items if it["item_class"] == c) for c in _ITEM_CLASSES}
    for cls, minimum in _MANDATORY_MINIMA.items():
        if counts.get(cls, 0) < minimum:
            raise AssertionError(f"class {cls}: have {counts.get(cls, 0)}, need >= {minimum}")
    return counts


# Metric functions. All are pure: each takes a gold item and a Prediction and returns a float, or None
# when the metric does not apply to the item.
def _gold_ids(item: dict[str, Any]) -> set[str]:
    return set(item.get("expected_provision_ids", []))


def seed_recall_at_k(item: dict[str, Any], pred: Prediction, k: int = 20) -> float | None:
    """Return the share of gold provision ids found in the top-k retriever seeds.

    None when the item has no gold (must_refuse), where recall is undefined.
    """
    gold = _gold_ids(item)
    if not gold:
        return None
    return len(set(pred.seed_ids[:k]) & gold) / len(gold)


def answer_recall_at_k(item: dict[str, Any], pred: Prediction, k: int = 20) -> float | None:
    """Return the share of gold provision ids among the top-k cited provisions; None if no gold."""
    gold = _gold_ids(item)
    if not gold:
        return None
    return len(set(pred.cited_ids[:k]) & gold) / len(gold)


def cluster_coverage(item: dict[str, Any], pred: Prediction) -> float | None:
    """Return the share of expected obligation clusters covered by at least one cited provision.

    Applies to multi-obligation items; None for items without expected_clusters. Needs
    pred.cited_clusters (cited id to cluster); a run without that map scores 0, since coverage is
    measured, never imputed.
    """
    clusters = item.get("expected_clusters") or []
    if not clusters:
        return None
    covered = set(pred.cited_clusters.get(cid) for cid in pred.cited_ids) & set(clusters)
    return len(covered) / len(clusters)


def citation_precision(item: dict[str, Any], pred: Prediction) -> float | None:
    """Return the share of the answer's cited provisions that are in the gold set.

    None when the answer cited nothing, where precision is undefined.
    """
    if not pred.cited_ids:
        return None
    gold = _gold_ids(item)
    return len(set(pred.cited_ids) & gold) / len(set(pred.cited_ids))


def refusal_correct(item: dict[str, Any], pred: Prediction) -> float | None:
    """Return 1.0 if the predicted mode matches an expected refuse or clarify, else 0.0.

    Only items whose expected_mode is refuse or clarify are scored, since that decision is what is
    under test; other items return None. An answerable item that is wrongly refused shows up in the
    recall metrics instead.
    """
    if item.get("expected_mode") not in ("refuse", "clarify"):
        return None
    return 1.0 if pred.mode == item["expected_mode"] else 0.0


_METRIC_FNS: dict[str, Callable[[dict[str, Any], Prediction], float | None]] = {
    "seed_recall@20": lambda it, p: seed_recall_at_k(it, p, 20),
    "seed_recall@10": lambda it, p: seed_recall_at_k(it, p, 10),
    "answer_recall@20": lambda it, p: answer_recall_at_k(it, p, 20),
    "cluster_coverage": cluster_coverage,
    "citation_precision": citation_precision,
    "refusal_correct": refusal_correct,
}


# Aggregation.
def score_item(item: dict[str, Any], pred: Prediction) -> dict[str, float | None]:
    """Compute every metric for one item (None where a metric does not apply)."""
    if pred.mode not in _MODES:
        raise ValueError(f"prediction mode must be in {sorted(_MODES)}, got {pred.mode!r}")
    return {name: fn(item, pred) for name, fn in _METRIC_FNS.items()}


def _mean(vals: list[float | None]) -> float | None:
    present = [v for v in vals if v is not None]
    return st.mean(present) if present else None


def score_set(items: list[dict[str, Any]], preds: list[Prediction]) -> dict[str, Any]:
    """Score a whole set: per-item rows, per-item_class means and overall means.

    A metric that does not apply to an item is left out of the mean rather than counted as 0, so a
    class mean covers only the items where the metric is defined. ``preds`` must be parallel to
    ``items``.
    """
    if len(items) != len(preds):
        raise ValueError(f"items ({len(items)}) and preds ({len(preds)}) must be parallel")
    rows = [
        {"id": it["id"], "item_class": it["item_class"], **score_item(it, p)}
        for it, p in zip(items, preds, strict=True)
    ]
    metric_names = list(_METRIC_FNS)

    def _agg(subset: list[dict[str, Any]]) -> dict[str, float | None]:
        return {m: _mean([r[m] for r in subset]) for m in metric_names}

    by_class = {c: _agg([r for r in rows if r["item_class"] == c]) for c in sorted(_ITEM_CLASSES)}
    return {
        "n": len(items),
        "provisional": True,
        "overall": _agg(rows),
        "by_class": by_class,
        "rows": rows,
    }


# Runner. The run_fn is pluggable: the self-test uses a stub, live runs use one of the adapters below.
async def run_set(items: list[dict[str, Any]], run_fn: RunFn) -> list[Prediction]:
    """Produce one Prediction per gold item with ``run_fn``.

    The harness does not care how predictions are produced: by the stub or by a live adapter.
    """
    return [await run_fn(it) for it in items]


def stub_run_fn(item: dict[str, Any]) -> Prediction:
    """Return a near-perfect stub prediction for the self-test.

    It echoes the gold as if the engine had retrieved and cited exactly the gold provisions, follows
    the expected_mode and assigns cited ids to the expected clusters round-robin. This exercises the
    whole pipeline (load, run, score) without an engine. It is not a model of engine behaviour.
    """
    gold = list(item.get("expected_provision_ids", []))
    clusters = item.get("expected_clusters") or []
    cited_clusters = {pid: clusters[i % len(clusters)] for i, pid in enumerate(gold)} if clusters else {}
    return Prediction(mode=item["expected_mode"], seed_ids=gold, cited_ids=gold, cited_clusters=cited_clusters)


# Live retrieval adapter without any LLM. It separates seed retrieval and graph expansion from the
# answer model: the raw question is retrieved as a single leg, with no LLM decomposition into
# sub-queries, so the metrics measure the seed and expansion path directly. The retrieved provision
# uuids are mapped to the gold '<external_id> § <label>' ids. Read-only. The four retrieval-tuning
# flags (seed-token IDF, seed-pool scaling, concurrent legs, per-leg quotas) are read from config at
# call time, so the same adapter measures the baseline (all off) or any combination switched on
# through the environment.
def make_live_run_fn(*, method: str = "cte", k: int = 20) -> RunFn:
    """Build an async run_fn that retrieves against the live engine without calling an LLM.

    The raw question is retrieved as a single PythiaQuery (no `_decompose` LLM call), so seed recall
    and answer recall reflect only seed selection, pool size and graph expansion. `mode` is always
    'answer': this adapter never refuses, so the refusal metric is out of scope. Needs a live
    database; no unit test calls it. Imports are local so that importing this module needs neither
    a database nor torch.
    """
    from pythia.contract import PythiaQuery
    from pythia.engine import PythiaEngine
    from pythia.ports import NexusPort

    eng = PythiaEngine()
    port = NexusPort()

    async def _to_gold_ids(uuids: list[str]) -> list[str]:
        """Map provision uuids to gold '<external_id> § <label>' ids, deduplicated, keeping order."""
        if not uuids:
            return []
        rows = await port.fetch_provisions(uuids)
        by_id = {r.id: f"{r.external_id} {r.label}" for r in rows if r.external_id and r.label}
        out, seen = [], set()
        for u in uuids:
            g = by_id.get(u)
            if g and g not in seen:
                seen.add(g)
                out.append(g)
        return out

    async def run_fn(item: dict[str, Any]) -> Prediction:
        q = item["question_cz"]
        # Seed list before expansion: the raw hybrid seed, so seed recall isolates seed selection.
        seeds = await port.search(q, k=k, jurisdiction=["CZ", "EU"], rerank=eng.seed_rerank, rerank_pool=None)
        seed_gold = await _to_gold_ids([s.provision_id for s in seeds])
        # Cited list: the full retrieval (seeds plus graph expansion), standing in for answer
        # citations.
        ans = await eng.retrieve(PythiaQuery(query=q, jurisdiction=["CZ", "EU"], k=k), method=method)
        cited_gold = await _to_gold_ids([c.provision_id for c in ans.cited_provisions])
        return Prediction(mode="answer", seed_ids=seed_gold, cited_ids=cited_gold)

    return run_fn


# Multi-leg adapter: the full intake path with reciprocal rank fusion (RRF), as answer() runs it.
# Unlike make_live_run_fn (one raw leg, no LLM), it calls run_intake (a real call to the cheap LLM
# when intake v2 is enabled in active mode) to obtain sub-questions phrased in statutory language
# plus HyDE drafts, retrieves each leg, and merges them with the same per-leg weights, quotas, pool
# size and k_eff that answer() uses. The merged top-k is mapped to gold ids. This measures what
# intake expansion adds to answer recall. The drafting and verification LLMs are never called, so
# cited_ids is the merged retrieved set: the most the answer could cite from. All flags are read
# from config at call time, so the same adapter measures (a) the baseline _decompose path, (b)
# intake v2 active, and (c) intake v2 active together with the retrieval-tuning flags, chosen
# through the environment.
def make_multileg_run_fn(*, method: str = "cte", k: int = 20) -> RunFn:
    """Build an async run_fn for the intake and multi-leg RRF retrieval path.

    Reproduces answer()'s leg logic without the drafting and verification calls, and returns a
    Prediction in gold ids. Needs a live database and an LLM API key. Imports are local so that
    importing this module needs neither a database nor torch.
    """
    from math import ceil

    from pythia.config import get_settings
    from pythia.contract import PythiaQuery
    from pythia.engine import PythiaEngine
    from pythia.llm_factory import get_llm
    from pythia.ports import NexusPort
    from pythia.reasoning.answer import _decompose
    from pythia.reasoning.intake import run_intake

    eng = PythiaEngine()
    port = NexusPort()

    async def _to_gold_ids(uuids: list[str]) -> list[str]:
        if not uuids:
            return []
        rows = await port.fetch_provisions(uuids)
        by_id = {r.id: f"{r.external_id} {r.label}" for r in rows if r.external_id and r.label}
        out, seen = [], set()
        for u in uuids:
            g = by_id.get(u)
            if g and g not in seen:
                seen.add(g)
                out.append(g)
        return out

    async def run_fn(item: dict[str, Any]) -> Prediction:
        q = item["question_cz"]
        s = get_settings()
        llm = get_llm("cheap")
        hyde_legs: list[str] = []
        leg_weights: dict[int, float] = {}
        # Build the legs exactly as answer() does: off or shadow uses _decompose, active uses intake.
        if s.intake_v2_enabled and s.intake_v2_mode == "active":
            rec = await run_intake(llm, q)
            _subs = [sq for sq in rec.sub_questions if sq.strip() and sq.strip() != q.strip()]
            sub_queries = [q, *_subs]
            hyde_legs = list(rec.hyde_drafts)
            juris = rec.jurisdiction
            for idx in range(len(sub_queries)):
                leg_weights[idx] = 1.0
            for h_off in range(len(hyde_legs)):
                leg_weights[len(sub_queries) + h_off] = s.intake_hyde_weight
        else:
            if s.intake_v2_enabled:  # shadow: run intake for comparison, retrieve with _decompose
                await run_intake(llm, q)
            sub_queries, juris, _ = await _decompose(llm, q)
            for idx in range(len(sub_queries)):
                leg_weights[idx] = 1.0
        all_legs = [*sub_queries, *hyde_legs]
        rerank_pool = min(s.seed_pool_cap, 16 * len(all_legs)) if s.seed_pool_scale_enabled else None

        async def _leg(sq: str) -> list[tuple[str, int, float]]:
            a = await eng.retrieve(
                PythiaQuery(query=sq, jurisdiction=juris, k=k, rerank_pool=rerank_pool), method=method
            )
            return [(cp.provision_id, rank, cp.score) for rank, cp in enumerate(a.cited_provisions)]

        if s.retrieval_concurrent_legs_enabled:
            import asyncio as _a

            leg_results = await _a.gather(*(_leg(sq) for sq in all_legs))
        else:
            leg_results = [await _leg(sq) for sq in all_legs]

        leg_cap = s.retrieval_leg_quota if s.retrieval_quotas_enabled else None
        rrf: dict[str, float] = {}
        C = 60
        for idx, triples in enumerate(leg_results):
            w = leg_weights.get(idx, 1.0)
            for pid, rank, _score in triples[:leg_cap] if leg_cap is not None else triples:
                rrf[pid] = rrf.get(pid, 0.0) + w * (1.0 / (C + rank))
        k_eff = min(s.retrieval_k_eff_cap, k * ceil(len(sub_queries) / 2)) if s.retrieval_quotas_enabled else k
        top = sorted(rrf, key=lambda i: -rrf[i])[:k_eff]
        cited_gold = await _to_gold_ids(top)
        # seed_ids: the raw single-leg seed for the original question, as a recall baseline.
        seeds = await port.search(q, k=k, jurisdiction=juris, rerank=eng.seed_rerank, rerank_pool=None)
        seed_gold = await _to_gold_ids([sd.provision_id for sd in seeds])
        return Prediction(mode="answer", seed_ids=seed_gold, cited_ids=cited_gold)

    return run_fn


def self_test() -> dict[str, Any]:
    """Run the pipeline over a 3-item fixture with the stub run_fn and return the score report.

    Needs no database, engine or network. Checks that the gold validates and the metrics are sane,
    and raises on any inconsistency so pytest and the CLI can rely on it.
    """
    fixture: list[dict[str, Any]] = [
        {
            "id": "fx-multi",
            "item_class": "multi_obligation",
            "question_cz": "Beru prvního zaměstnance, co musím udělat?",
            "expected_provision_ids": ["262/2006 § 34", "587/1992 § 3"],
            "expected_clusters": ["smlouva", "pojištění"],
            "expected_facts": [],
            "sufficiency": "multi",
            "expected_mode": "answer",
            "verified": False,
            "source": "draft",
            "notes": "fixture",
        },
        {
            "id": "fx-amount",
            "item_class": "amount",
            "question_cz": "Jaká je minimální mzda?",
            "expected_provision_ids": ["285/2024 § 1"],
            "expected_clusters": [],
            "expected_facts": [{"provision": "285/2024 § 1", "snippet": "Koeficient"}],
            "sufficiency": "answer",
            "expected_mode": "answer",
            "verified": False,
            "source": "draft",
            "notes": "fixture",
        },
        {
            "id": "fx-refuse",
            "item_class": "must_refuse",
            "question_cz": "Kolik je daň v USA?",
            "expected_provision_ids": [],
            "expected_clusters": [],
            "expected_facts": [],
            "sufficiency": "off_corpus",
            "expected_mode": "refuse",
            "verified": False,
            "source": "draft",
            "notes": "fixture",
        },
    ]
    validate_gold(fixture)
    preds = [stub_run_fn(it) for it in fixture]
    report = score_set(fixture, preds)

    # The stub predicts perfectly, so recall, precision and refusal must be 1.0 wherever defined.
    multi = next(r for r in report["rows"] if r["id"] == "fx-multi")
    assert multi["seed_recall@20"] == 1.0, multi
    assert multi["answer_recall@20"] == 1.0, multi
    assert multi["cluster_coverage"] == 1.0, multi
    assert multi["citation_precision"] == 1.0, multi
    assert multi["refusal_correct"] is None, "answer item has no refusal metric"

    amount = next(r for r in report["rows"] if r["id"] == "fx-amount")
    assert amount["cluster_coverage"] is None, "amount item has no clusters"
    assert amount["seed_recall@20"] == 1.0, amount

    refuse = next(r for r in report["rows"] if r["id"] == "fx-refuse")
    assert refuse["refusal_correct"] == 1.0, refuse
    assert refuse["seed_recall@20"] is None, "must_refuse has no gold -> recall N/A"
    assert refuse["citation_precision"] is None, refuse
    return report


if __name__ == "__main__":  # pragma: no cover — command-line entry point
    import argparse
    import asyncio

    ap = argparse.ArgumentParser(description="AQ-03 layman-gold intake eval harness")
    ap.add_argument("--selftest", action="store_true", help="run the DRY self-test (no engine) and print the report")
    ap.add_argument("--live", action="store_true", help="run the LLM-free live-engine adapter (needs a live DB)")
    ap.add_argument(
        "--multileg",
        action="store_true",
        help="run the A2 intake→multi-leg-RRF adapter (real cheap-LLM; needs a live DB + key)",
    )
    ap.add_argument("--method", default="cte", help="expansion method for --live (cte|ppr|gnn)")
    ap.add_argument("--k", type=int, default=20, help="retrieval k for --live")
    ap.add_argument("--set", default=str(_SET), help="path to the gold JSONL")
    args = ap.parse_args()

    if args.selftest:
        rep = self_test()
        print("SELF-TEST OK — stub plumbing report:")
        print(json.dumps(rep["overall"], ensure_ascii=False, indent=2))
    elif args.live:
        from pythia.config import get_settings
        from pythia.db import close_pool

        async def _run_live() -> None:
            _, gold_items = load_gold(args.set)
            cfg = get_settings()
            print(f"LIVE eval · {len(gold_items)} items · method={args.method} k={args.k}")
            print(
                f"  A1 flags: idf={cfg.seed_token_idf_enabled} pool_scale={cfg.seed_pool_scale_enabled} "
                f"concurrent={cfg.retrieval_concurrent_legs_enabled} quotas={cfg.retrieval_quotas_enabled}"
            )
            run_fn = make_live_run_fn(method=args.method, k=args.k)
            preds = await run_set(gold_items, run_fn)
            report = score_set(gold_items, preds)
            ov = report["overall"]
            print(
                "  overall:",
                json.dumps(
                    {m: (round(v, 4) if isinstance(v, float) else v) for m, v in ov.items()}, ensure_ascii=False
                ),
            )
            for cls in sorted(_ITEM_CLASSES):
                bc = report["by_class"][cls]
                print(
                    f"  {cls:<16}",
                    json.dumps(
                        {m: (round(v, 4) if isinstance(v, float) else v) for m, v in bc.items()}, ensure_ascii=False
                    ),
                )
            print("  NOTE: gold labels are PROVISIONAL (verified:false) — treat deltas as DIRECTIONAL.")
            await close_pool()

        asyncio.run(_run_live())
    elif args.multileg:
        from pythia.config import get_settings
        from pythia.db import close_pool

        async def _run_multileg() -> None:
            _, gold_items = load_gold(args.set)
            cfg = get_settings()
            print(f"MULTILEG (A2) eval · {len(gold_items)} items · method={args.method} k={args.k}")
            print(
                f"  A2 flags: intake_v2={cfg.intake_v2_enabled} mode={cfg.intake_v2_mode} "
                f"hyde_w={cfg.intake_hyde_weight}"
            )
            print(
                f"  A1 flags: idf={cfg.seed_token_idf_enabled} pool_scale={cfg.seed_pool_scale_enabled} "
                f"concurrent={cfg.retrieval_concurrent_legs_enabled} quotas={cfg.retrieval_quotas_enabled}"
            )
            run_fn = make_multileg_run_fn(method=args.method, k=args.k)
            preds = await run_set(gold_items, run_fn)
            report = score_set(gold_items, preds)
            ov = report["overall"]
            print(
                "  overall:",
                json.dumps(
                    {m: (round(v, 4) if isinstance(v, float) else v) for m, v in ov.items()}, ensure_ascii=False
                ),
            )
            for cls in sorted(_ITEM_CLASSES):
                bc = report["by_class"][cls]
                print(
                    f"  {cls:<16}",
                    json.dumps(
                        {m: (round(v, 4) if isinstance(v, float) else v) for m, v in bc.items()}, ensure_ascii=False
                    ),
                )
            print("  NOTE: gold labels are PROVISIONAL (verified:false) — treat deltas as DIRECTIONAL.")
            await close_pool()

        asyncio.run(_run_multileg())
    else:
        hdr, gold_items = load_gold(args.set)
        cnts = assert_class_minima(gold_items)
        print(f"gold set: {args.set}")
        print(
            f"  schema={hdr.get('schema')}  snapshot={hdr.get('corpus_snapshot')}  provisional={hdr.get('provisional')}"
        )
        print(f"  {len(gold_items)} items · class counts {cnts}")
        # Score with the stub predictor so the CLI runs end to end without an engine.
        stub_preds = [stub_run_fn(it) for it in gold_items]
        report = score_set(gold_items, stub_preds)
        print("  stub-predictor overall metrics (plumbing proof, NOT an engine result):")
        print(json.dumps(report["overall"], ensure_ascii=False, indent=2))
        print("  NOTE: gold labels are PROVISIONAL (verified:false) — lawyer verification pending.")

    async def _noop() -> None:  # keeps the asyncio import used on every CLI path
        return None

    asyncio.run(_noop())
