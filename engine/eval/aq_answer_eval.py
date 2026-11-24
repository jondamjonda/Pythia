"""End-to-end answer-quality evaluation, run once with the answer-quality flags off and once on.

Sends every question of the layman gold set (`gold_layman_v1.jsonl`: 100 draft items in 4 classes)
to the live answer endpoint, extracts what each answer grounded, refused and surfaced, scores the
metric families below and writes a reproducible artifact stamped with a run manifest. Comparing an
"off" artifact with an "on" artifact (see aq_compare.py) shows what the answer-quality features
(question gate, claim verification and related flags) change.

It works over HTTP on purpose, so it measures the whole served answer (engine synthesis, app-side
shaping and the app's citation check) rather than an engine internal. The default endpoint is the
engine's `/v1/answer` on port 8099, which takes POST `{"query": ...}`; any extra field is rejected
with a 422. Run once per flag setting (from engine/):

    python eval/aq_answer_eval.py --posture-tag off --limit 6 --out eval/artifacts/deploy_off_slice.json
    # switch the answer-quality flags on and restart the serving stack
    python eval/aq_answer_eval.py --posture-tag on  --limit 6 --out eval/artifacts/deploy_on_slice.json
    python eval/aq_compare.py eval/artifacts/deploy_off_slice.json eval/artifacts/deploy_on_slice.json

Metric families (per item, then aggregated per item_class and overall):
  * answer_recall@k     |expected_provision_ids ∩ returned provisions| / |expected_provision_ids|
  * seed_recall@8       |expected ∩ the seed-marked returned provisions (top 8)| / |expected|
  * citation_precision  share of grounded citations (citations_map) that parse to a valid act and §
  * refusal_correctness must_refuse items should be refused, answerable items should not
  * grounded            the answer grounded at least one citation; used for the no-regression check
                        (an item grounded with the flags off must stay grounded with them on)
  * mode_match          the served render_mode or band matches the item's expected_mode
                        (answer, refuse or clarify)
  * practical_present   the answer includes a practical block with items (deadlines, authorities)

Fabrication per claim type is scored separately, by the independent re-checker in
`pythia.eval.fabrication_check`, over the exported claim-ledger bundles rather than the HTTP answers:
the ledgers are the audit record the claim-verification loop writes. Point --ledger-dir at
data/exports/claim_ledgers to add a fabrication_by_type block to the artifact; --ledger-live
re-checks against the pythia database (SELECT only). Without a ledger directory the block is
omitted, since there is nothing to measure.

Every artifact carries a `manifest` block (posture tag, gold version, corpus snapshot, git SHAs of
engine and app, the environment-manifest reference and the per-item request ids), so a run is
reproducible and can be joined to cost records, engine traces and exported ledgers by request id.

Caveat, recorded in every artifact: none of the 100 gold labels has been verified by a lawyer yet,
so every number is directional, not certified. This harness makes the comparison reproducible;
lawyer-certified labels are what would make the numbers admissible for a promotion decision.
"""

from __future__ import annotations

import argparse
import json
import re as _re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]  # engine/
_GOLD = _REPO / "eval" / "gold_layman_v1.jsonl"
# Maps the engine's render_mode / band values to the gold set's coarse expected_mode.
_MODE_FAMILY = {
    "full": "answer",
    "hedged": "answer",
    "refuse": "refuse",
    "closest": "refuse",
    "notlegal": "refuse",
    "falsepremise": "refuse",
    "clarify": "clarify",
}


def _load_gold(classes: set[str] | None, gold_path: str | None = None) -> list[dict]:
    gold = Path(gold_path) if gold_path else _GOLD
    rows = []
    for line in gold.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not r.get("question_cz") or not r.get("item_class"):  # skip the schema header row
            continue
        if classes and r.get("item_class") not in classes:
            continue
        rows.append(r)
    return rows


def _gold_header(gold_path: str | None = None) -> dict:
    """Return the first record of the gold file: the schema header, which pins the corpus snapshot."""
    gold = Path(gold_path) if gold_path else _GOLD
    for line in gold.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        return r
    return {}


def _post(endpoint: str, query: str, timeout: float) -> dict:
    """POST {"query": ...} to the answer endpoint (the engine rejects extra fields with 422)."""
    body = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (localhost only)
        return json.loads(resp.read().decode("utf-8"))


_ACT_CZ = _re.compile(r"\d+/\d{4}")  # Czech act "262/2006"
_ACT_EU = _re.compile(r"3\d{4}[LRD]\d{4}")  # EU CELEX "32014L0065"
_SEC = _re.compile(r"§\s*(\d+[a-z]*)")
_ART = _re.compile(r"[Aa]rticle\s*(\d+)")


def _norm_cite(s: str) -> str:
    """Normalise a citation to the gold id format ("262/2006 § 34", "32014L0065 Article 27")."""
    return " ".join((s or "").strip().strip("[]()").split())


def _act_of(corpus: str) -> str | None:
    m = _ACT_CZ.search(corpus or "") or _ACT_EU.search(corpus or "")
    return m.group(0) if m else ((corpus or "").strip() or None)


def _sec_of(label: str) -> str | None:
    m = _SEC.search(label or "")
    if m:
        return "§ " + m.group(1)
    m = _ART.search(label or "")
    if m:
        return "Article " + m.group(1)
    return None


def _cite_from(act_src: str, sec_src: str) -> str | None:
    """Build a normalised "act § section" id from an act source and a section source."""
    act, sec = _act_of(act_src), _sec_of(sec_src)
    return _norm_cite(f"{act} {sec}") if (act and sec) else None


def _map_cites(ans: dict) -> set[str]:
    """Return the grounded citations from `citations_map` (corpus holds the act, label the §)."""
    out: set[str] = set()
    for c in ans.get("citations_map") or []:
        if not isinstance(c, dict):
            continue
        cid = _cite_from(str(c.get("corpus") or ""), str(c.get("label") or ""))
        if cid:
            out.add(cid)
    return out


def _chip_cites(ans: dict) -> set[str]:
    """Return citations from app-side chips, whose label holds both act and § ("187/2006 § 93")."""
    out: set[str] = set()

    def _chips(obj: Any) -> list:
        return (obj.get("chips") or []) if isinstance(obj, dict) else []

    for obj in [ans, *(ans.get("sections") or [])]:
        for ch in _chips(obj):
            lbl = str(ch.get("label") or "") if isinstance(ch, dict) else ""
            cid = _cite_from(lbl, lbl)
            if cid:
                out.add(cid)
    return out


def _cited_provision_cites(ans: dict) -> set[str]:
    """Return citations from `cited_provisions` (act from source_title, § from label).

    This is the full retrieved set, broader than the grounded map, so it feeds answer_recall: was
    the right provision retrieved at all?
    """
    out: set[str] = set()
    for c in ans.get("cited_provisions") or []:
        if not isinstance(c, dict):
            continue
        cid = _cite_from(str(c.get("source_title") or c.get("corpus") or ""), str(c.get("label") or ""))
        if cid:
            out.add(cid)
    return out


def _seed_cites(ans: dict) -> set[str]:
    """Return the first 8 seed provisions (those whose relation_path contains 'seed').

    Falls back to the first 8 cited_provisions when none is marked as a seed, which happens with an
    app-shaped answer that drops relation_path.
    """
    prov = [c for c in (ans.get("cited_provisions") or []) if isinstance(c, dict)]
    seeds = [c for c in prov if "seed" in (c.get("relation_path") or [])]
    pool = (seeds or prov)[:8]
    out: set[str] = set()
    for c in pool:
        cid = _cite_from(str(c.get("source_title") or c.get("corpus") or ""), str(c.get("label") or ""))
        if cid:
            out.add(cid)
    return out


def _returned_cites(ans: dict) -> set[str]:
    """Return every provision the answer mentions, as normalised act and § labels.

    The union of the grounded map, app chips and cited_provisions is the search space for
    answer_recall: did the answer surface the expected provision anywhere?
    """
    return {c for c in (_map_cites(ans) | _chip_cites(ans) | _cited_provision_cites(ans)) if c}


def _served_mode(ans: dict) -> str:
    rm = ans.get("render_mode") or ans.get("band") or ""
    return _MODE_FAMILY.get(str(rm), str(rm) or "unknown")


def _refused(ans: dict) -> bool:
    """Return True if the answer is a refusal (flag, refuse-family mode or abstain/refuse verdict)."""
    if bool(ans.get("refused")):
        return True
    if _served_mode(ans) == "refuse":
        return True
    return str(ans.get("verdict") or "") in ("abstain", "refuse")


def _practical_present(ans: dict) -> bool:
    p = ans.get("practical")
    return bool(isinstance(p, dict) and p.get("items"))


def _rid_of(ans: dict) -> str | None:
    at = ans.get("audit_trace")
    return str(at.get("request_id")) if isinstance(at, dict) and at.get("request_id") else None


def _score_item(item: dict, ans: dict | None) -> dict:
    exp_ids = {e for e in {_norm_cite(str(x)) for x in (item.get("expected_provision_ids") or [])} if e}
    cls = item.get("item_class")
    exp_mode = item.get("expected_mode")
    must_refuse = (exp_mode == "refuse") or (cls == "must_refuse")
    if ans is None:
        return {
            "id": item.get("id"),
            "item_class": cls,
            "error": True,
            "recall": 0.0,
            "seed_recall": 0.0,
            "citation_precision": None,
            "mode_match": False,
            "grounded": False,
            "practical_present": False,
            "refused": False,
            "refusal_correct": None,
            "rid": None,
            "expected_mode": exp_mode,
            "served_mode": "ERROR",
            "n_expected": len(exp_ids),
            "n_got": 0,
        }
    got = _returned_cites(ans)
    seeds = _seed_cites(ans)
    mapped = _map_cites(ans)  # grounded citations only, for precision
    recall = round(len(exp_ids & got) / len(exp_ids), 4) if exp_ids else None
    seed_recall = round(len(exp_ids & seeds) / len(exp_ids), 4) if exp_ids else None
    # Citation precision: the share of grounded citations that parse to a valid act and § id (a proxy
    # for well-formed, resolvable citations). None when nothing was grounded: undefined, not 0.
    raw_map = [c for c in (ans.get("citations_map") or []) if isinstance(c, dict)]
    n_map = len(raw_map)
    cite_precision = (len(mapped) / n_map) if n_map else None
    served = _served_mode(ans)
    refused = _refused(ans)
    # Refusal correctness: a must_refuse item is correct if refused, any other item if not refused.
    refusal_correct = refused if must_refuse else (not refused)
    return {
        "id": item.get("id"),
        "item_class": cls,
        "expected_mode": exp_mode,
        "served_mode": served,
        "mode_match": (served == exp_mode) if exp_mode else None,
        "recall": recall,
        "seed_recall": seed_recall,
        "citation_precision": (round(cite_precision, 4) if cite_precision is not None else None),
        "n_expected": len(exp_ids),
        "n_got": len(got),
        "n_grounded": len(mapped),
        "grounded": len(mapped) > 0 or len(got) > 0,
        "practical_present": _practical_present(ans),
        "refused": refused,
        "must_refuse": must_refuse,
        "refusal_correct": refusal_correct,
        "rid": _rid_of(ans),
    }


def _mean(vals: list) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def _rate(scored: list[dict], key: str) -> float | None:
    return _mean([(1.0 if s[key] else 0.0) if s.get(key) is not None else None for s in scored])


def _group_metrics(grp: list[dict]) -> dict:
    return {
        "n": len(grp),
        "recall@k": _mean([s["recall"] for s in grp]),
        "seed_recall@8": _mean([s["seed_recall"] for s in grp]),
        "citation_precision": _mean([s["citation_precision"] for s in grp]),
        "mode_match_rate": _mean([1.0 if s["mode_match"] else 0.0 for s in grp if s["mode_match"] is not None]),
        "grounded_rate": _rate(grp, "grounded"),
        "practical_present_rate": _rate(grp, "practical_present"),
        "refusal_correct_rate": _mean(
            [1.0 if s["refusal_correct"] else 0.0 for s in grp if s.get("refusal_correct") is not None]
        ),
        "errors": sum(1 for s in grp if s.get("error")),
    }


def _aggregate(scored: list[dict]) -> dict:
    by_class = {
        cls: _group_metrics([s for s in scored if s["item_class"] == cls])
        for cls in sorted({s["item_class"] for s in scored})
    }
    return {"by_class": by_class, "overall": _group_metrics(scored)}


def _git_sha(repo: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],  # noqa: S603, S607
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 - the git SHA in the manifest is best-effort
        return None


def _build_manifest(args, gold: list[dict], scored: list[dict]) -> dict:
    hdr = _gold_header(args.gold)
    app_repo = _REPO.parent / "app"  # the web application in the same monorepo
    return {
        "posture_tag": args.posture_tag,
        "arm": args.arm,
        "endpoint": args.endpoint,
        "gold_path": str(Path(args.gold).resolve()) if args.gold else str(_GOLD),
        "gold_schema": hdr.get("schema"),
        "gold_version": hdr.get("version"),
        "corpus_snapshot": hdr.get("corpus_snapshot"),
        "n_items": len(gold),
        "limit": args.limit or None,
        "engine_git_sha": _git_sha(_REPO),
        "app_git_sha": _git_sha(app_repo),
        "env_manifest_ref": args.env_manifest or "manifests/pythia-engine-env-manifest.json (app-side AQ-06 dump)",
        "ledger_dir": args.ledger_dir or None,
        "rids": [s.get("rid") for s in scored if s.get("rid")],
        "caveat": "DIRECTIONAL — gold labels are 0/100 lawyer-verified; not a certified promotion number.",
    }


def _fabrication_block(ledger_dir: str, *, live: bool, limit: int) -> dict:
    """Run the independent fabrication re-checker over the exported claim-ledger bundles.

    Reads every *.json bundle in `ledger_dir` (skipping the *.sha256 sidecars), re-checks each fact
    claim against freshly loaded full provision text (live) or a stub (fixture), and returns
    fabrication_by_type plus the verdict histogram. This scoring is separate from the HTTP answers.
    """
    from pythia.eval import fabrication_check as fc

    d = Path(ledger_dir)
    bundle_paths = sorted(p for p in d.glob("*.json") if not p.name.endswith(".sha256")) if d.is_dir() else []
    if not bundle_paths:
        return {
            "ledger_dir": ledger_dir,
            "n_bundles": 0,
            "note": "no exported claim-ledger bundles found — fabrication-per-type not computed.",
        }
    bundles = [fc.load_bundle(p) for p in bundle_paths]
    if live:
        import asyncio

        pids = {pid for b in bundles for c in fc.bundle_to_claims(b) for pid in c.citations}
        oracle: fc.ReCheckOracle = asyncio.run(fc.prime_live_oracle(pids))
    else:
        oracle = fc.FixtureOracle()  # no database: placeholder numbers, labelled as such below
    rep = fc.dry_run_report(bundles, oracle, limit=limit)
    return {
        "ledger_dir": ledger_dir,
        "n_bundles": len(bundle_paths),
        "oracle": rep["oracle"],
        "n_claims": rep["n_claims"],
        "recheck_verdicts": rep["recheck_verdicts"],
        "fabrication_by_type": rep["fabrication_by_type"],
        "oracle_vs_loop_diff": rep["oracle_vs_loop_diff"],
        "note": (
            "independent oracle over FRESH full text"
            if live
            else "FIXTURE oracle (no --ledger-live): grounding numbers are a SKELETON; re-run with "
            "--ledger-live against a reachable pythia DB for real independent verdicts."
        ),
    }


def build_artifact(args, gold: list[dict], scored: list[dict], classes: set[str] | None) -> dict:
    """Assemble the run artifact: manifest, aggregates, items and the optional fabrication block.

    Pure given the scored rows (all network calls happen earlier), so it can be unit-tested with
    fixture rows.
    """
    artifact: dict[str, Any] = {
        "schema": "aq_answer_eval_v2",
        "posture_tag": args.posture_tag,
        "arm": args.arm,
        "endpoint": args.endpoint,
        "n": len(scored),
        "classes": sorted(classes) if classes else "all",
        "caveat": "DIRECTIONAL — gold labels are 0/100 lawyer-verified; not a certified promotion number.",
        "manifest": _build_manifest(args, gold, scored),
        "aggregate": _aggregate(scored),
        "items": scored,
    }
    if args.ledger_dir:
        artifact["fabrication"] = _fabrication_block(args.ledger_dir, live=args.ledger_live, limit=args.ledger_limit)
    return artifact


def run(args) -> int:
    classes = set(args.classes.split(",")) if args.classes else None
    gold = _load_gold(classes, args.gold)
    if args.limit:
        gold = gold[: args.limit]
    print(f"[{args.posture_tag}] {len(gold)} items → {args.endpoint}", file=sys.stderr)
    scored = []
    for i, item in enumerate(gold, 1):
        try:
            ans = _post(args.endpoint, item["question_cz"], args.timeout)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"  [{i}/{len(gold)}] {item.get('id')} ERROR {e}", file=sys.stderr)
            ans = None
        scored.append(_score_item(item, ans))
        r = scored[-1]
        print(
            f"  [{i}/{len(gold)}] {item.get('id')} recall={r['recall']} seed={r['seed_recall']} "
            f"mode={r.get('served_mode')} grounded={r['grounded']} "
            f"refuse_ok={r.get('refusal_correct')} practical={r['practical_present']}",
            file=sys.stderr,
        )
    artifact = build_artifact(args, gold, scored, classes)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(artifact["aggregate"]["overall"], ensure_ascii=False))
    return 0


def compare(off_path: str, on_path: str) -> int:
    """Print a quick off-versus-on delta table; aq_compare.py writes the full scored memo."""
    off = json.loads(Path(off_path).read_text(encoding="utf-8"))
    on = json.loads(Path(on_path).read_text(encoding="utf-8"))
    metrics = (
        "recall@k",
        "seed_recall@8",
        "citation_precision",
        "mode_match_rate",
        "grounded_rate",
        "practical_present_rate",
        "refusal_correct_rate",
    )
    print("OFF-vs-ON DELTA (DIRECTIONAL — gold not lawyer-certified)\n")
    print(f"{'class':<18}{'metric':<22}{'OFF':>8}{'ON':>8}{'Δ':>8}")
    for cls in sorted(set(off["aggregate"]["by_class"]) | set(on["aggregate"]["by_class"])):
        for m in metrics:
            o = (off["aggregate"]["by_class"].get(cls) or {}).get(m)
            n = (on["aggregate"]["by_class"].get(cls) or {}).get(m)
            d = round(n - o, 4) if (o is not None and n is not None) else None
            print(f"{cls:<18}{m:<22}{str(o):>8}{str(n):>8}{str(d):>8}")
    print("-" * 64)
    for m in metrics:
        o = off["aggregate"]["overall"].get(m)
        n = on["aggregate"]["overall"].get(m)
        d = round(n - o, 4) if (o is not None and n is not None) else None
        print(f"{'OVERALL':<18}{m:<22}{str(o):>8}{str(n):>8}{str(d):>8}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="OFF-vs-ON answer-quality eval over the AQ-03 gold set (WS11/B4)")
    ap.add_argument("--gold", default=None, help="override gold jsonl path")
    ap.add_argument(
        "--posture-tag",
        dest="posture_tag",
        default=None,
        choices=["off", "on"],
        help="the flag posture this run booted with (off=clean baseline / on=T-LOCAL activation); "
        "defaults to --arm when that is off/on, else 'off'",
    )
    ap.add_argument("--arm", default=None, help="free-text label (defaults to --posture-tag)")
    ap.add_argument(
        "--endpoint",
        default="http://127.0.0.1:8099/v1/answer",
        help="the served answer endpoint (engine /v1/answer @ :8099, or app /api/answer @ :8002)",
    )
    ap.add_argument("--classes", default=None, help="comma list of item_class to include (default: all)")
    ap.add_argument("--limit", type=int, default=0, help="bounded slice — first N gold items")
    # With every answer-quality feature enforced, one answer takes about 60-90 s, but a long
    # sequential batch on a memory-constrained machine (heavy swapping) can exceed 180 s per item.
    # The generous timeout keeps a slow but valid answer from being scored as an error; it does not
    # fix the memory pressure itself, which needs a machine with more RAM or pauses between items.
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--out", default="eval/artifacts/run.json")
    ap.add_argument(
        "--env-manifest",
        dest="env_manifest",
        default=None,
        help="path/ref to the AQ-06 env-manifest dump (recorded in the run manifest)",
    )
    ap.add_argument(
        "--ledger-dir",
        dest="ledger_dir",
        default=None,
        help="dir of exported claim-ledger bundles → fold in fabrication-per-type (step-72)",
    )
    ap.add_argument(
        "--ledger-live",
        dest="ledger_live",
        action="store_true",
        help="re-ground the fabrication check against the pythia DB (else FIXTURE/skeleton)",
    )
    ap.add_argument(
        "--ledger-limit",
        dest="ledger_limit",
        type=int,
        default=200,
        help="max claims to re-check from the ledgers (default 200)",
    )
    ap.add_argument("--compare", nargs=2, metavar=("OFF", "ON"), default=None)
    args = ap.parse_args()
    # posture_tag defaults to the arm when the arm is "off" or "on" (so older `--arm on` invocations
    # are tagged correctly), otherwise to "off". The arm defaults to the resolved posture tag.
    if args.posture_tag is None:
        args.posture_tag = args.arm if args.arm in ("off", "on") else "off"
    if args.arm is None:
        args.arm = args.posture_tag
    if args.compare:
        return compare(*args.compare)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
