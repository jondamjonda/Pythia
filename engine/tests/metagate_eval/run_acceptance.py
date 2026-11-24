#!/usr/bin/env python3
"""AC-METAGATE acceptance harness (Phase-C M4, JUDGMENT §5 / PLAN §6 step M4).

Runs the labelled meta-eval set (eval_set.jsonl) through the LIVE meta-gateway and scores it against the two
HARD AC-METAGATE constraints, the gate that must be GREEN before `metagate_mode='enforce'` (and the
`metagate_enabled` flip) is allowed:

    1. fabrication_rate == 0      (THE HARD CONSTRAINT) — no row whose expected_route is anything other than
                                  "sound" may be routed to "sound". A single fabrication FAILS the gate.
    2. sound_recall    >= 0.95    — of the rows labelled "sound", at least 95% must route to "sound" (so the
                                  product is still useful, not refusing everything to trivially satisfy #1).

It also prints per-route precision/recall + a confusion matrix, and runs a FAIL-CLOSED probe (monkeypatch an
LLM error and assert every row routes to a SAFE non-sound route and never crashes).

────────────────────────────────────────────────────────────────────────────────────────────────────────────
REQUIRES A RUNNING ENGINE — the OPERATOR runs this later, NOT CI. It needs:
    • PYTHIA_DATABASE_URL pointing at a reachable `pythia` corpus DB (the pinned answer corpus), and
    • a reachable LLM for the cheap meta judges: either Ollama up locally (synthesizer_provider="ollama",
      the default) OR synthesizer_provider="anthropic" + PYTHIA_ANTHROPIC_API_KEY set.
For each eval row it reproduces the answer.py retrieval inputs (decompose → RRF retrieve → _fetch) and then
calls `classify_meta(query, decomp, meta, ordered, rrf, raw)` exactly as answer.py does. NO answer is
generated and NOTHING is written — this is read-only over the corpus.

The SCORING math (score_rows / confusion_matrix / per-route P/R / the constraint check) is PURE and is unit-
tested WITHOUT a live engine by tests/test_metagate_acceptance_smoke.py.

USAGE
    # acceptance run (the gate): score the set at the CURRENT config floors
    python tests/metagate_eval/run_acceptance.py

    # grid-search the SMALLEST-change floor config that satisfies the two constraints
    python tests/metagate_eval/run_acceptance.py --sweep

    # custom set / verbosity
    python tests/metagate_eval/run_acceptance.py --eval-set path/to/set.jsonl --show-misses
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

# The four routes (mirrors metagate.MetaRoute). "sound" is the only answer-bearing route; the other three are
# the honest non-answer routes. fabrication = a NON-sound expected row that the gateway routed to "sound".
ROUTES = ("sound", "not_a_legal_question", "not_in_corpus", "false_premise")
_EVAL_SET_DEFAULT = Path(__file__).with_name("eval_set.jsonl")

# The two AC-METAGATE bars.
FABRICATION_RATE_MAX = 0.0  # HARD: must be exactly 0
SOUND_RECALL_MIN = 0.95  # of the sound-labelled rows


@dataclass
class EvalRow:
    query: str
    expected_route: str
    note: str = ""


@dataclass
class Scored:
    """One eval row after classification: the row + the route the gateway actually produced (or an error)."""

    row: EvalRow
    actual_route: str | None = None  # None if classification crashed (which itself FAILS the never-crash bar)
    error: str | None = None


@dataclass
class Metrics:
    """The PURE scoring output. `passed` reflects the two AC-METAGATE constraints."""

    n: int
    fabrication_rate: float
    fabrications: list[Scored]
    sound_recall: float
    n_sound: int
    sound_hits: int
    per_route: dict[str, dict[str, float]]  # route -> {precision, recall, support_expected, support_actual}
    confusion: dict[str, dict[str, int]]  # expected_route -> {actual_route -> count} (+ "__error__")
    crashes: int
    passed: bool


# ════════════════════════════════════════════════════════════════════════════════════════════════════════
# loading
# ════════════════════════════════════════════════════════════════════════════════════════════════════════
def load_eval_set(path: str | Path = _EVAL_SET_DEFAULT) -> list[EvalRow]:
    """Load eval_set.jsonl. Lines beginning with `//` (and blank lines) are comments and are skipped — the
    set carries a header comment + per-section banners. Validates every row's expected_route ∈ ROUTES."""
    rows: list[EvalRow] = []
    text = Path(path).read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        s = line.strip()
        if not s or s.startswith("//"):
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{lineno}: invalid JSON: {e}") from e
        q = str(obj.get("query", "")).strip()
        er = str(obj.get("expected_route", "")).strip()
        if not q:
            raise ValueError(f"{path}:{lineno}: empty 'query'")
        if er not in ROUTES:
            raise ValueError(f"{path}:{lineno}: expected_route {er!r} not in {ROUTES}")
        rows.append(EvalRow(query=q, expected_route=er, note=str(obj.get("note", ""))))
    if not rows:
        raise ValueError(f"{path}: no eval rows found")
    return rows


# ════════════════════════════════════════════════════════════════════════════════════════════════════════
# PURE scoring math (unit-tested without a live engine)
# ════════════════════════════════════════════════════════════════════════════════════════════════════════
def score_rows(scored: list[Scored]) -> Metrics:
    """PURE. Compute fabrication_rate, sound_recall, per-route precision/recall, the confusion matrix, the
    crash count, and the overall PASS/FAIL against the two AC-METAGATE constraints. No engine, no LLM, no DB —
    it consumes already-classified rows. A row with actual_route=None (the classifier crashed) counts as a
    crash AND is excluded from per-route counts (a crash is never "routed" anywhere)."""
    n = len(scored)
    crashes = sum(1 for s in scored if s.actual_route is None)

    # fabrication_rate = (# non-sound-expected rows routed to sound) / (# non-sound-expected rows).
    non_sound = [s for s in scored if s.row.expected_route != "sound"]
    fabrications = [s for s in non_sound if s.actual_route == "sound"]
    fabrication_rate = (len(fabrications) / len(non_sound)) if non_sound else 0.0

    # sound_recall = (# sound-expected rows routed to sound) / (# sound-expected rows).
    sound_rows = [s for s in scored if s.row.expected_route == "sound"]
    sound_hits = sum(1 for s in sound_rows if s.actual_route == "sound")
    sound_recall = (sound_hits / len(sound_rows)) if sound_rows else 1.0

    # confusion matrix: expected -> actual (with a "__error__" bucket for crashes).
    confusion: dict[str, dict[str, int]] = {er: {ar: 0 for ar in (*ROUTES, "__error__")} for er in ROUTES}
    for s in scored:
        key = s.actual_route if s.actual_route in ROUTES else "__error__"
        confusion[s.row.expected_route][key] += 1

    # per-route precision/recall (errors excluded from both numerator and denominator of precision).
    per_route: dict[str, dict[str, float]] = {}
    for r in ROUTES:
        expected_r = [s for s in scored if s.row.expected_route == r]
        actual_r = [s for s in scored if s.actual_route == r]
        tp = sum(1 for s in actual_r if s.row.expected_route == r)
        precision = (tp / len(actual_r)) if actual_r else 1.0
        recall = (tp / len(expected_r)) if expected_r else 1.0
        per_route[r] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "support_expected": len(expected_r),
            "support_actual": len(actual_r),
        }

    passed = fabrication_rate <= FABRICATION_RATE_MAX and sound_recall >= SOUND_RECALL_MIN and crashes == 0
    return Metrics(
        n=n,
        fabrication_rate=round(fabrication_rate, 4),
        fabrications=fabrications,
        sound_recall=round(sound_recall, 4),
        n_sound=len(sound_rows),
        sound_hits=sound_hits,
        per_route=per_route,
        confusion=confusion,
        crashes=crashes,
        passed=passed,
    )


def format_report(m: Metrics, *, show_misses: bool = False, scored: list[Scored] | None = None) -> str:
    """PURE pretty-printer for a Metrics object: the two constraints, per-route P/R, the confusion matrix, and
    the verdict. `show_misses` lists the off-diagonal rows (when `scored` is supplied)."""
    lines: list[str] = []
    lines.append("═" * 92)
    lines.append("AC-METAGATE acceptance report")
    lines.append("═" * 92)
    lines.append(f"rows scored: {m.n}    crashes (never-crash bar): {m.crashes}")
    lines.append("")
    fab_ok = "OK" if m.fabrication_rate <= FABRICATION_RATE_MAX else "FAIL"
    rec_ok = "OK" if m.sound_recall >= SOUND_RECALL_MIN else "FAIL"
    lines.append(f"  [1] fabrication_rate = {m.fabrication_rate:.4f}   (HARD: == {FABRICATION_RATE_MAX})   → {fab_ok}")
    lines.append(f"      (non-sound rows routed to 'sound': {len(m.fabrications)})")
    lines.append(f"  [2] sound_recall     = {m.sound_recall:.4f}   (>= {SOUND_RECALL_MIN})        → {rec_ok}")
    lines.append(f"      ({m.sound_hits}/{m.n_sound} sound-labelled rows routed to 'sound')")
    lines.append("")
    lines.append("per-route precision / recall:")
    for r in ROUTES:
        pr = m.per_route[r]
        lines.append(
            f"  {r:<22}  P={pr['precision']:.3f}  R={pr['recall']:.3f}  "
            f"(expected {int(pr['support_expected'])}, predicted {int(pr['support_actual'])})"
        )
    lines.append("")
    lines.append("confusion matrix (rows = expected, cols = actual):")
    cols = (*ROUTES, "__error__")
    abbr = {
        "sound": "snd",
        "not_a_legal_question": "nlq",
        "not_in_corpus": "nic",
        "false_premise": "fp",
        "__error__": "ERR",
    }
    header = " " * 24 + "".join(f"{abbr[c]:>7}" for c in cols)
    lines.append(header)
    for er in ROUTES:
        row = "".join(f"{m.confusion[er][c]:>7}" for c in cols)
        lines.append(f"  {er:<22}{row}")
    if show_misses and scored is not None:
        misses = [s for s in scored if s.actual_route != s.row.expected_route]
        if misses:
            lines.append("")
            lines.append(f"misclassifications ({len(misses)}):")
            for s in misses:
                got = s.actual_route if s.actual_route is not None else f"ERROR({s.error})"
                lines.append(f"  expected {s.row.expected_route:<22} got {got:<22} :: {s.row.query[:80]}")
    lines.append("")
    lines.append("═" * 92)
    lines.append(
        f"AC-METAGATE: {'PASS' if m.passed else 'FAIL'}  (fabrication_rate==0 AND sound_recall>=0.95 AND no crashes)"
    )
    lines.append("═" * 92)
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════════════════════════════════
# LIVE classification (requires the engine; the operator runs this, not CI)
# ════════════════════════════════════════════════════════════════════════════════════════════════════════
async def _build_retrieval_inputs(query: str, *, k: int = 12, method: str = "cte", max_hops: int = 3):
    """Reproduce the answer.py pre-gateway retrieval pipeline for one query, returning the exact tuple
    classify_meta consumes: (decomp, meta, ordered, rrf, raw). LIVE — imports the engine + touches the DB.
    Imported lazily so this module imports cleanly with no engine present (the smoke test never calls this)."""
    from pythia.contract import PythiaQuery
    from pythia.llm_factory import get_llm
    from pythia.reasoning.answer import PythiaEngine, _decompose, _fetch

    llm = get_llm("cheap")
    sub_queries, juris, matter = await _decompose(llm, query)
    eng = PythiaEngine()
    rrf: dict[str, float] = {}
    raw: dict[str, float] = {}
    C = 60
    for sq in sub_queries:
        a = await eng.retrieve(PythiaQuery(query=sq, jurisdiction=juris, k=k, max_hops=max_hops), method=method)
        for rank, cp in enumerate(a.cited_provisions):
            rrf[cp.provision_id] = rrf.get(cp.provision_id, 0.0) + 1.0 / (C + rank)
            raw[cp.provision_id] = max(raw.get(cp.provision_id, 0.0), cp.score)
    top = sorted(rrf, key=lambda i: -rrf[i])[:k]
    meta = await _fetch(top) if top else {}
    ordered = [i for i in top if i in meta]
    decomp = {"sub_queries": sub_queries, "jurisdiction": juris, "matter_type": matter}
    return decomp, meta, ordered, rrf, raw


async def classify_one_live(row: EvalRow) -> Scored:
    """Classify ONE row through the live engine + classify_meta. classify_meta itself never raises (each axis
    fails closed), but retrieval CAN (DB down, etc.); we capture that as a crash so the never-crash bar sees
    it. Imported lazily."""
    from pythia.reasoning.metagate import classify_meta

    try:
        decomp, meta, ordered, rrf, raw = await _build_retrieval_inputs(row.query)
        verdict = await classify_meta(row.query, decomp, meta, ordered, rrf, raw)
        return Scored(row=row, actual_route=verdict.route)
    except Exception as e:  # any live failure = a crash on this row (fails the never-crash bar)
        return Scored(row=row, actual_route=None, error=f"{type(e).__name__}: {e}")


async def run_live(rows: list[EvalRow]) -> list[Scored]:
    """Classify every row sequentially (the cheap judges are network-bound; sequential keeps the load gentle
    on Ollama / the paid endpoint and the output deterministic). LIVE."""
    out: list[Scored] = []
    for i, row in enumerate(rows, start=1):
        s = await classify_one_live(row)
        got = s.actual_route if s.actual_route is not None else f"ERROR({s.error})"
        print(
            f"  [{i:>3}/{len(rows)}] expected={row.expected_route:<22} got={got:<22} {row.query[:60]}", file=sys.stderr
        )
        out.append(s)
    return out


# ════════════════════════════════════════════════════════════════════════════════════════════════════════
# fail-closed probe (forces an LLM error → every row must route to a SAFE non-sound route, never crash)
# ════════════════════════════════════════════════════════════════════════════════════════════════════════
async def fail_closed_probe(rows: list[EvalRow], *, sample: int = 8) -> tuple[bool, list[str]]:
    """Monkeypatch the cheap LLM so EVERY judge call raises, then assert classify_meta still produces a verdict
    for a sample of rows and that the route is NEVER "sound" (the fail-closed guarantee — an LLM outage can
    never fabricate an answer) and NEVER crashes. Returns (ok, problems). LIVE retrieval is NOT needed — we
    feed a tiny synthetic retrieved-set so the probe runs even without a corpus. Imported lazily."""
    import pythia.reasoning.metagate as mg
    from pythia.contract import MetaVerdict  # noqa: F401  (kept for signature clarity)
    from pythia.reasoning.metagate import classify_meta

    class _ErrorLLM:
        async def json(self, prompt, schema, *, tier: str = "cheap"):
            raise RuntimeError("forced LLM error (fail-closed probe)")

        async def text(self, prompt, *, system=None, tier: str = "cheap"):
            raise RuntimeError("forced LLM error (fail-closed probe)")

        async def available(self) -> bool:
            return True

    # A tiny synthetic retrieved set so the probe needs no DB. Use a deliberately legal-lexicon-free query so
    # the deterministic INTENT tier-1 does NOT short-circuit — that forces the LLM path, which must fail closed.
    ordered = ["px1", "px2", "px3"]
    meta = {
        "px1": {
            "ext": "111/2000",
            "label": "art 1",
            "txt": "Some retrieved provision text about duties.",
            "jurisdiction": "CZ",
            "legal_hierarchy": "STATUTE",
            "title": "Probe Act",
            "bindingness": "hard-law",
            "is_authoritative": True,
            "temporal_status": "in-force",
        },
        "px2": {
            "ext": "222/2001",
            "label": "art 2",
            "txt": "Another retrieved provision.",
            "jurisdiction": "CZ",
            "legal_hierarchy": "STATUTE",
            "title": "Probe Act 2",
            "bindingness": "hard-law",
            "is_authoritative": True,
            "temporal_status": "in-force",
        },
        "px3": {
            "ext": "333/2002",
            "label": "art 3",
            "txt": "A third retrieved provision.",
            "jurisdiction": "CZ",
            "legal_hierarchy": "STATUTE",
            "title": "Probe Act 3",
            "bindingness": "hard-law",
            "is_authoritative": True,
            "temporal_status": "in-force",
        },
    }
    rrf = {"px1": 0.9, "px2": 0.8, "px3": 0.7}
    raw = {"px1": 0.80, "px2": 0.72, "px3": 0.60}

    # ambiguous-middle queries (no legal lexicon, no non-legal signal) → the deterministic intent tiers do not
    # fire, so the (now-erroring) LLM is reached and MUST fail closed.
    probe_queries = [
        "Tell me about the obligations that may apply to my situation here.",
        "Co bych měl v této situaci zařídit, aby to bylo v pořádku?",
        "Mám nějakou povinnost ohledně toho, co jsem popsal výše?",
        "Is there anything I should be aware of regarding this matter?",
    ]
    if rows:
        # mix in a few real eval queries too (skip ones the deterministic tiers would short-circuit).
        for r in rows:
            if len(probe_queries) >= sample:
                break
            ql = r.query.lower()
            if not mg._LEGAL_LEXICON_RE.search(r.query) and not mg._NON_LEGAL_RE.search(ql):
                probe_queries.append(r.query)

    problems: list[str] = []
    # route every meta judge through the erroring LLM (get_llm is imported INSIDE classify_meta).
    import unittest.mock as _mock

    with _mock.patch("pythia.llm_factory.get_llm", lambda tier: _ErrorLLM()):
        for q in probe_queries[:sample]:
            try:
                v = await classify_meta(q, None, meta, ordered, rrf, raw)
            except Exception as e:
                problems.append(f"CRASH on {q[:50]!r}: {type(e).__name__}: {e}")
                continue
            if v.route == "sound":
                problems.append(f"FABRICATED 'sound' under forced LLM error on {q[:50]!r}")
    ok = not problems
    return ok, problems


# ════════════════════════════════════════════════════════════════════════════════════════════════════════
# --sweep : grid-search the SMALLEST-change floor config satisfying the two constraints
# ════════════════════════════════════════════════════════════════════════════════════════════════════════
# the floor knobs the sweep tunes, with the live defaults as the centre point. JUDGMENT §3.2 names exactly
# these four (INTENT_FLOOR / COVERAGE_FLOOR / PREMISE_FLOOR / STRONG_COS).
_SWEEP_GRID: dict[str, list[float]] = {
    "intent_floor": [0.70, 0.75, 0.80, 0.85],
    "coverage_floor": [0.55, 0.60, 0.65, 0.70],
    "premise_floor": [0.65, 0.70, 0.75, 0.80],
    "strong_cos": [0.50, 0.55, 0.60, 0.65],
}


def _config_distance(cfg: dict[str, float], baseline: dict[str, float]) -> float:
    """'Smallest change' metric: number of knobs that differ from the baseline, then total absolute drift as a
    tiebreak. Lower is closer to today's config (we prefer the least-invasive passing config)."""
    changed = sum(1 for k, v in cfg.items() if abs(v - baseline[k]) > 1e-9)
    drift = sum(abs(v - baseline[k]) for k, v in cfg.items())
    return changed + drift / 100.0


async def run_sweep(rows: list[EvalRow]) -> None:
    """Grid-search the floor config. For each candidate config we re-run the LIVE classification with the
    floors patched onto get_settings(), score it, and keep every config that PASSES; then report the one
    closest to the live baseline. LIVE — this is expensive (it re-classifies the whole set per config); the
    operator runs it after the plain acceptance run, to find the minimal flip if the defaults miss the bar."""
    from pythia.config import PythiaSettings, get_settings
    import pythia.reasoning.metagate as mg

    baseline_settings = get_settings()
    baseline = {k: float(getattr(baseline_settings, k)) for k in _SWEEP_GRID}
    print(f"sweep baseline (live defaults): {baseline}", file=sys.stderr)

    # cartesian product of the grid.
    import itertools

    keys = list(_SWEEP_GRID)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*(_SWEEP_GRID[k] for k in keys))]
    print(f"sweep: {len(combos)} candidate configs × {len(rows)} rows", file=sys.stderr)

    passing: list[tuple[dict[str, float], Metrics]] = []
    for ci, cfg in enumerate(combos, start=1):
        # build a settings object with the candidate floors and patch metagate.get_settings to return it.
        s = PythiaSettings(**{k: cfg[k] for k in cfg})
        with _patched_settings(mg, s):
            scored = await run_live(rows)
        m = score_rows(scored)
        tag = "PASS" if m.passed else "fail"
        print(
            f"  [{ci}/{len(combos)}] {cfg} → fab={m.fabrication_rate:.3f} "
            f"sound_recall={m.sound_recall:.3f} crashes={m.crashes} {tag}",
            file=sys.stderr,
        )
        if m.passed:
            passing.append((cfg, m))

    print("\n" + "═" * 92)
    if not passing:
        print(
            "SWEEP: NO config in the grid satisfied AC-METAGATE "
            "(fabrication_rate==0 AND sound_recall>=0.95). Widen the grid or improve the gateway/eval set."
        )
        print("═" * 92)
        return
    passing.sort(key=lambda pm: _config_distance(pm[0], baseline))
    best_cfg, best_m = passing[0]
    print(f"SWEEP: {len(passing)} passing config(s). SMALLEST-CHANGE config vs the live baseline:")
    for k in _SWEEP_GRID:
        mark = "  (changed)" if abs(best_cfg[k] - baseline[k]) > 1e-9 else ""
        print(f"    {k:<16} = {best_cfg[k]:<6} (baseline {baseline[k]}){mark}")
    print(f"  → fabrication_rate={best_m.fabrication_rate:.4f}  sound_recall={best_m.sound_recall:.4f}")
    print("═" * 92)


class _patched_settings:
    """Context manager: patch metagate.get_settings to return a fixed PythiaSettings for the duration. Used by
    the sweep so each candidate floor config is evaluated without mutating global config. (Mirrors the
    monkeypatch the unit tests use, but as a plain CM so the harness needs no pytest at runtime.)"""

    def __init__(self, mg_module, settings):
        self._mg = mg_module
        self._settings = settings
        self._orig = None

    def __enter__(self):
        self._orig = self._mg.get_settings
        self._mg.get_settings = lambda: self._settings
        return self

    def __exit__(self, *exc):
        self._mg.get_settings = self._orig
        return False


# ════════════════════════════════════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════════════════════════════════════
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AC-METAGATE acceptance harness (requires a running engine + LLM).")
    p.add_argument(
        "--eval-set",
        default=str(_EVAL_SET_DEFAULT),
        help="path to the labelled JSONL eval set (default: alongside this script).",
    )
    p.add_argument(
        "--sweep",
        action="store_true",
        help="grid-search the smallest-change floor config satisfying the two constraints.",
    )
    p.add_argument(
        "--no-probe",
        action="store_true",
        help="skip the fail-closed probe (the probe needs no DB; skip only when debugging).",
    )
    p.add_argument("--show-misses", action="store_true", help="list every misclassified row in the report.")
    return p.parse_args(argv)


async def _amain(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    rows = load_eval_set(args.eval_set)
    print(f"loaded {len(rows)} eval rows from {args.eval_set}", file=sys.stderr)

    if args.sweep:
        await run_sweep(rows)
        return 0

    # 1) the fail-closed probe (cheap, no DB).
    if not args.no_probe:
        print("running fail-closed probe (forced LLM error → must route to a safe non-sound route)…", file=sys.stderr)
        ok, problems = await fail_closed_probe(rows)
        if ok:
            print("  fail-closed probe: OK (no fabrication, no crash under forced LLM error)", file=sys.stderr)
        else:
            print("  fail-closed probe: FAIL", file=sys.stderr)
            for pr in problems:
                print(f"    - {pr}", file=sys.stderr)

    # 2) the live acceptance run.
    print("classifying the eval set through the live gateway…", file=sys.stderr)
    scored = await run_live(rows)
    m = score_rows(scored)
    print()
    print(format_report(m, show_misses=args.show_misses, scored=scored))

    probe_ok = args.no_probe or (await fail_closed_probe(rows))[0]
    gate_green = m.passed and probe_ok
    print(
        f"\nGATE: {'GREEN' if gate_green else 'RED'} "
        f"(acceptance {'PASS' if m.passed else 'FAIL'} + fail-closed probe "
        f"{'OK' if probe_ok else 'FAIL'})"
    )
    print(
        "Reminder: metagate_mode='enforce' (and the metagate_enabled flip) stays BLOCKED until this gate is "
        "GREEN AND the eval set is lawyer-spot-checked. See docs/AC_METAGATE.md."
    )
    return 0 if gate_green else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    raise SystemExit(main())
