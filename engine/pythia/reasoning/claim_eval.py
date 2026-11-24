"""Evaluation harness for the claim-verification loop, scored against a claim gold set.

Scores the claim-verify loop (draft, atomize, typed verifiers, ledger) the way ``intake_eval`` and
``regsuper.analysis.compliance_eval`` score their surfaces. It provides the per-claim quality
numbers needed before enforce mode is switched on: how precisely the loop verifies true claims, how
many fabrications it catches, and in enforce mode its demote rate and repair yield. It only
measures; nothing here changes engine behaviour, and the module is import-safe (no database or
torch) so the self-test runs offline in CI.

Design (mirrors intake_eval):
  - The metric functions are pure: they take a gold item and a run outcome and return a number or
    None (not applicable), so they are unit-testable with a stub outcome and no live engine or LLM.
  - The live loop is reached only through a pluggable ``run_fn`` (gold item -> ClaimOutcome), so the
    same scorers run over a stub fixture (self-test) or over an adapter around the real engine with
    the loop on.
  - The gold labels are provisional (``verified: false``, ``source: draft``) until a lawyer verifies
    them. The harness measures; it does not certify.

For each draft, a gold item pins the expected atomic claims, each with a type and a ``fabricated``
flag (a claim whose assertion the cited provisions do not support). The loop outcome is the ledger
the loop produced: for each claim, whether the loop verified it and, in enforce mode, whether it was
demoted, dropped or repaired. From gold and outcome the pure scorers compute:
  - ``claim_precision``: of the claims the loop verified, the fraction that are truly non-fabricated
    (did the loop avoid affirming a fabrication?).
  - ``claim_recall``: of the truly non-fabricated claims, the fraction the loop verified.
  - ``fabrication_catch_rate``: of the fabricated claims, the fraction the loop did not verify
    (caught: demoted, dropped or left unverified), per claim type and overall.
  - ``demote_rate``: the fraction of claims the loop demoted to a gap (an enforce signal; 0 in
    shadow mode).
  - ``repair_yield``: the fraction of demoted claims that a repair pass later re-verified. None
    while the outcomes report no repaired claims; the key is always present so the report shape
    stays stable.

``score_set`` rolls these up per claim type and overall, reporting which metrics are defined and
which do not apply. ``self_test`` runs the whole pipeline over a small fixture with a stub run_fn
(no database, LLM or network) to prove that the harness loads and validates gold data and computes
sane metrics.

CLI:  python -m pythia.reasoning.claim_eval [--selftest] [--set <claim_gold.jsonl>]
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from statistics import mean
from typing import Any

# The claim-type and final-status vocabularies are owned by claim_ledger. Importing them validates
# the gold labels and outcome statuses against a single source of truth, so a typo fails loudly.
from pythia.reasoning.claim_ledger import CLAIM_TYPE, FINAL_STATUS

_SET = pathlib.Path(__file__).resolve().parents[2] / "eval" / "claim_gold_v1.jsonl"

_CLAIM_TYPES = frozenset(CLAIM_TYPE)
# The verdict for one claim after the loop ran (harness vocabulary, mapped from the ledger):
#   - verified:   the loop affirmatively verified the claim (a deterministic pass survived).
#   - demoted:    the loop demoted the claim to an honest gap (enforce mode;
#                 final_status='demoted_gap').
#   - dropped:    the loop dropped the claim as unsafe or unverifiable (enforce;
#                 final_status='dropped_*').
#   - unverified: no affirmative verdict (the shadow default, or every check skipped); not an
#     assertion.
_OUTCOME_VERDICTS = frozenset({"verified", "demoted", "dropped", "unverified"})


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# ClaimOutcome: what a run_fn (a stub, or a live adapter with the loop on) returns for one gold
# item.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass
class ClaimOutcome:
    """The loop's per-claim outcome for one gold draft, keyed by gold claim id.

    ``verdicts`` maps gold_claim_id to an outcome verdict (one of _OUTCOME_VERDICTS). ``repaired``
    optionally lists the demoted claim ids that a repair pass re-verified. A gold claim missing from
    ``verdicts`` counts as 'unverified': the loop said nothing affirmative about it.
    """

    verdicts: dict[str, str] = field(default_factory=dict)
    repaired: list[str] = field(default_factory=list)

    def verdict_for(self, claim_id: str) -> str:
        return self.verdicts.get(claim_id, "unverified")


RunFn = Callable[[dict[str, Any]], Awaitable[ClaimOutcome]]


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# Gold loading and validation.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def load_gold(path: str | pathlib.Path = _SET) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load the claim gold JSONL and return (header_manifest, items).

    The first line is the header manifest (schema, corpus_snapshot, provisional flag); every
    following line is an item. Each item is validated.
    """
    lines = [ln for ln in pathlib.Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        raise ValueError(f"claim gold set {path} is empty")
    header = json.loads(lines[0])
    items = [json.loads(ln) for ln in lines[1:]]
    validate_gold(items)
    return header, items


def validate_gold(items: list[dict[str, Any]]) -> None:
    """Validate the gold items structurally.

    Checks unique item ids, a non-empty draft and the provisional flag, and for every claim an id, a
    type in CLAIM_TYPE and a boolean ``fabricated``, with claim ids unique within an item.
    """
    seen: set[str] = set()
    for i, it in enumerate(items):
        iid = it.get("id")
        if not isinstance(iid, str) or not iid:
            raise ValueError(f"item[{i}] missing string 'id'")
        if iid in seen:
            raise ValueError(f"duplicate item id {iid!r}")
        seen.add(iid)
        if not isinstance(it.get("draft"), str) or not it["draft"].strip():
            raise ValueError(f"item {iid}: missing non-empty draft")
        if it.get("verified") is not False:
            raise ValueError(f"item {iid}: every label must be PROVISIONAL (verified:false) in the draft set")
        claims = it.get("claims")
        if not isinstance(claims, list) or not claims:
            raise ValueError(f"item {iid}: 'claims' must be a non-empty list")
        cseen: set[str] = set()
        for c in claims:
            cid = c.get("id")
            if not isinstance(cid, str) or not cid:
                raise ValueError(f"item {iid}: a claim is missing a string 'id'")
            if cid in cseen:
                raise ValueError(f"item {iid}: duplicate claim id {cid!r}")
            cseen.add(cid)
            if c.get("type") not in _CLAIM_TYPES:
                raise ValueError(
                    f"item {iid} claim {cid}: type must be in {sorted(_CLAIM_TYPES)}, got {c.get('type')!r}"
                )
            if not isinstance(c.get("fabricated"), bool):
                raise ValueError(f"item {iid} claim {cid}: 'fabricated' must be a bool")


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# Per-item helpers.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def _gold_claims(item: dict[str, Any]) -> list[dict[str, Any]]:
    return item.get("claims", [])


def _true_claims(item: dict[str, Any]) -> set[str]:
    """Ids of the truly non-fabricated claims (the ones the loop should verify)."""
    return {c["id"] for c in _gold_claims(item) if not c["fabricated"]}


def _fabricated_claims(item: dict[str, Any]) -> set[str]:
    """Ids of the fabricated claims (the ones the loop should not verify)."""
    return {c["id"] for c in _gold_claims(item) if c["fabricated"]}


def _verified(outcome: ClaimOutcome, item: dict[str, Any]) -> set[str]:
    """Ids the loop affirmatively verified."""
    return {c["id"] for c in _gold_claims(item) if outcome.verdict_for(c["id"]) == "verified"}


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# Metric functions, all pure: each takes a gold item and a ClaimOutcome and returns a float, or None
# (N/A).
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def claim_precision(item: dict[str, Any], outcome: ClaimOutcome) -> float | None:
    """Of the claims the loop verified, the fraction that are truly non-fabricated.

    None when the loop verified nothing (precision is undefined over an empty prediction).
    """
    verified = _verified(outcome, item)
    if not verified:
        return None
    return len(verified & _true_claims(item)) / len(verified)


def claim_recall(item: dict[str, Any], outcome: ClaimOutcome) -> float | None:
    """Of the truly non-fabricated claims, the fraction the loop verified.

    None when the item has no true claims (recall is undefined over an empty gold set).
    """
    true = _true_claims(item)
    if not true:
        return None
    return len(_verified(outcome, item) & true) / len(true)


def fabrication_catch_rate(item: dict[str, Any], outcome: ClaimOutcome) -> float | None:
    """Of the fabricated claims, the fraction the loop did not verify.

    A claim counts as caught when it was demoted, dropped or left unverified. None when the item has
    no fabricated claims (the metric does not apply).
    """
    fab = _fabricated_claims(item)
    if not fab:
        return None
    caught = {cid for cid in fab if outcome.verdict_for(cid) != "verified"}
    return len(caught) / len(fab)


def demote_rate(item: dict[str, Any], outcome: ClaimOutcome) -> float | None:
    """Fraction of all claims the loop demoted to a gap.

    An enforce-mode signal; 0.0 in shadow mode, where nothing is demoted. None only when the item
    has no claims, which validate_gold already rules out.
    """
    claims = _gold_claims(item)
    if not claims:
        return None
    demoted = sum(1 for c in claims if outcome.verdict_for(c["id"]) == "demoted")
    return demoted / len(claims)


def repair_yield(item: dict[str, Any], outcome: ClaimOutcome) -> float | None:
    """Of the claims the loop demoted, the fraction that a repair pass re-verified.

    None when nothing was demoted, or when the outcome lists no repaired claims, so the metric reads
    as not applicable instead of a false 0.0. Otherwise it is the number of demoted claims that were
    repaired, divided by the number of demoted claims.
    """
    demoted = {c["id"] for c in _gold_claims(item) if outcome.verdict_for(c["id"]) == "demoted"}
    if not demoted:
        return None  # no demotions: repair yield is undefined
    if not outcome.repaired:
        return None  # no repaired ids reported: not applicable, never a false 0.0
    return len(set(outcome.repaired) & demoted) / len(demoted)


_METRIC_FNS: dict[str, Callable[[dict[str, Any], ClaimOutcome], float | None]] = {
    "claim_precision": claim_precision,
    "claim_recall": claim_recall,
    "fabrication_catch_rate": fabrication_catch_rate,
    "demote_rate": demote_rate,
    "repair_yield": repair_yield,
}


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# Fabrication catch rate per claim type.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def fabrication_by_type(items: list[dict[str, Any]], outcomes: list[ClaimOutcome]) -> dict[str, float | None]:
    """For each claim type, the catch rate over that type's fabricated claims across the whole set.

    That is caught divided by total fabricated for the type. None for a type with no fabricated
    claims in the set (not applicable, never imputed as 0).
    """
    tot: dict[str, int] = {t: 0 for t in CLAIM_TYPE}
    caught: dict[str, int] = {t: 0 for t in CLAIM_TYPE}
    for it, oc in zip(items, outcomes, strict=True):
        for c in _gold_claims(it):
            if not c["fabricated"]:
                continue
            tot[c["type"]] += 1
            if oc.verdict_for(c["id"]) != "verified":
                caught[c["type"]] += 1
    return {t: (caught[t] / tot[t] if tot[t] else None) for t in CLAIM_TYPE}


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# Aggregation.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def score_item(item: dict[str, Any], outcome: ClaimOutcome) -> dict[str, float | None]:
    """Compute every metric for one item (None where the metric does not apply)."""
    for cid, v in outcome.verdicts.items():
        if v not in _OUTCOME_VERDICTS:
            raise ValueError(f"outcome verdict for {cid!r} must be in {sorted(_OUTCOME_VERDICTS)}, got {v!r}")
    return {name: fn(item, outcome) for name, fn in _METRIC_FNS.items()}


def _mean(vals: list[float | None]) -> float | None:
    present = [v for v in vals if v is not None]
    return mean(present) if present else None


def score_set(items: list[dict[str, Any]], outcomes: list[ClaimOutcome]) -> dict[str, Any]:
    """Score a whole set: per-item rows, overall means and the per-type fabrication catch rates.

    Metrics that do not apply to an item are left out of the mean rather than counted as 0.
    ``outcomes`` must be parallel to ``items``.
    """
    if len(items) != len(outcomes):
        raise ValueError(f"items ({len(items)}) and outcomes ({len(outcomes)}) must be parallel")
    rows = [{"id": it["id"], **score_item(it, oc)} for it, oc in zip(items, outcomes, strict=True)]
    metric_names = list(_METRIC_FNS)
    overall = {m: _mean([r[m] for r in rows]) for m in metric_names}
    return {
        "n": len(items),
        "provisional": True,
        "overall": overall,
        "fabrication_by_type": fabrication_by_type(items, outcomes),
        "rows": rows,
    }


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# Runner. Outcomes come from a pluggable run_fn: the self-test uses a stub; a live adapter can plug
# in.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
async def run_set(items: list[dict[str, Any]], run_fn: RunFn) -> list[ClaimOutcome]:
    """Produce a ClaimOutcome per gold item with the pluggable ``run_fn`` (item -> loop outcome).

    A live adapter would run answer() with claim_verify_enabled on and map the ledger rows to
    outcome verdicts (see ledger_to_outcome); the harness itself does not care how outcomes are
    produced.
    """
    return [await run_fn(it) for it in items]


def stub_run_fn(item: dict[str, Any]) -> ClaimOutcome:
    """Stub 'oracle' loop for the self-test: verify every true claim and no fabricated one.

    Every fabricated claim is left unverified, making it a perfect shadow-mode catcher. This
    exercises the whole pipeline (load, run, score) with no live engine or LLM and proves the
    metrics compute sane values. It is not a model of the real loop.
    """
    verdicts = {c["id"]: ("verified" if not c["fabricated"] else "unverified") for c in _gold_claims(item)}
    return ClaimOutcome(verdicts=verdicts)


def ledger_to_outcome(ledger_rows: list[dict[str, Any]], id_map: dict[str, str]) -> ClaimOutcome:
    """Map the rows of a to_audit_dict() ledger to a ClaimOutcome in the gold claim-id space.

    ``id_map`` maps engine claim_id to gold claim_id. The final_status vocabulary (FINAL_STATUS)
    collapses to the harness verdict: 'verified' and 'repaired_verified' become verified,
    'demoted_gap' demoted, 'dropped_*' dropped, and 'shadow' (or anything else) unverified. It lives
    here, for a live adapter, so the mapping is defined in one place. A row whose engine id is not
    in ``id_map`` is skipped (it is not part of the gold set).
    """
    _collapse = {
        "verified": "verified",
        "repaired_verified": "verified",
        "demoted_gap": "demoted",
        "dropped_safety": "dropped",
        "dropped_unverifiable": "dropped",
        "shadow": "unverified",
    }
    verdicts: dict[str, str] = {}
    for row in ledger_rows:
        gold_id = id_map.get(str(row.get("claim_id")))
        if gold_id is None:
            continue
        final = str(row.get("final", "shadow"))
        if final not in FINAL_STATUS:  # unknown status: treat as unverified, never a false verify
            final = "shadow"
        verdicts[gold_id] = _collapse.get(final, "unverified")
    return ClaimOutcome(verdicts=verdicts)


def self_test() -> dict[str, Any]:
    """Run the offline self-test over a tiny fixture with the stub run_fn.

    Needs no database, live engine, LLM or network. It proves the pipeline (build gold, validate,
    score, check the metrics are sane), returns the report, and raises on any inconsistency so
    pytest or the CLI can assert on it.
    """
    fixture: list[dict[str, Any]] = [
        {
            "id": "fx-deadline",
            "verified": False,
            "source": "draft",
            "draft": "## Legislativní rámec\nOznámení podejte do 15 dnů [455/1991 § 6]. Lhůta je 3 měsíce.",
            "claims": [
                {"id": "d1", "type": "deadline", "fabricated": False},  # the 15-day deadline is in the source
                {"id": "d2", "type": "deadline", "fabricated": True},  # the "3 měsíce" deadline is invented
            ],
            "notes": "fixture",
        },
        {
            "id": "fx-amount",
            "verified": False,
            "source": "draft",
            "draft": "## Hodnocení\nMinimální mzda činí 20 800 Kč [285/2024 § 2]. Pokuta až 5 000 000 Kč.",
            "claims": [
                {"id": "a1", "type": "amount", "fabricated": False},  # the min-wage amount is in the source
                {"id": "a2", "type": "sanction", "fabricated": True},  # the 5M penalty is invented
            ],
            "notes": "fixture",
        },
        {
            "id": "fx-rule",
            "verified": False,
            "source": "draft",
            "draft": "## Legislativní rámec\nPodnikatel musí ohlásit živnost [455/1991 § 6].",
            "claims": [
                {"id": "r1", "type": "legal_rule", "fabricated": False},  # grounded rule
            ],
            "notes": "fixture",
        },
    ]
    validate_gold(fixture)
    outcomes = [stub_run_fn(it) for it in fixture]
    report = score_set(fixture, outcomes)

    # Sanity checks on the oracle stub, which verifies every true claim and catches every fabricated
    # one.
    deadline = next(r for r in report["rows"] if r["id"] == "fx-deadline")
    assert deadline["claim_precision"] == 1.0, deadline  # the only verified claim (d1) is genuinely true
    assert deadline["claim_recall"] == 1.0, deadline  # d1 (the only true claim) was verified
    assert deadline["fabrication_catch_rate"] == 1.0, deadline  # d2 (fabricated) was not verified: caught
    assert deadline["demote_rate"] == 0.0, deadline  # the shadow oracle demotes nothing
    assert deadline["repair_yield"] is None, deadline  # no demotions, so not applicable

    rule = next(r for r in report["rows"] if r["id"] == "fx-rule")
    assert rule["fabrication_catch_rate"] is None, "no fabricated claim ⇒ catch-rate N/A"

    # Per-type fabrication catch: deadline and sanction each had one fabricated claim, both caught,
    # so 1.0; every other type had none, so None.
    fbt = report["fabrication_by_type"]
    assert fbt["deadline"] == 1.0, fbt
    assert fbt["sanction"] == 1.0, fbt
    assert fbt["amount"] is None, fbt  # a1 was not fabricated, so there is no fabricated amount
    assert fbt["legal_rule"] is None, fbt

    # Overall precision and recall are 1.0 (perfect oracle); repair_yield is None everywhere (no
    # repairs).
    assert report["overall"]["claim_precision"] == 1.0, report["overall"]
    assert report["overall"]["claim_recall"] == 1.0, report["overall"]
    assert report["overall"]["repair_yield"] is None, report["overall"]
    return report


if __name__ == "__main__":  # pragma: no cover — operator CLI
    import argparse

    ap = argparse.ArgumentParser(description="MP-G (T5) claim-loop eval harness")
    ap.add_argument(
        "--selftest", action="store_true", help="run the DRY self-test (no engine/LLM) and print the report"
    )
    ap.add_argument("--set", default=str(_SET), help="path to the claim gold JSONL")
    args = ap.parse_args()

    if args.selftest:
        rep = self_test()
        print("SELF-TEST OK — stub-oracle plumbing report:")
        print(
            json.dumps(
                {"overall": rep["overall"], "fabrication_by_type": rep["fabrication_by_type"]},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        hdr, gold_items = load_gold(args.set)
        print(f"claim gold set: {args.set}")
        print(
            f"  schema={hdr.get('schema')}  snapshot={hdr.get('corpus_snapshot')}  provisional={hdr.get('provisional')}"
        )
        print(f"  {len(gold_items)} items")
        stub = [stub_run_fn(it) for it in gold_items]
        report = score_set(gold_items, stub)
        print("  stub-oracle overall metrics (plumbing proof, NOT a loop result):")
        print(json.dumps(report["overall"], ensure_ascii=False, indent=2))
        print("  NOTE: gold labels are PROVISIONAL (verified:false) — lawyer verification pending.")
