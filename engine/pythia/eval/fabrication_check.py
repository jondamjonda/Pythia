"""Independent fabrication re-check for exported claim ledgers, plus a small dry-run report.

The claim-verification loop (v_fact, v_cit and the other verifiers) already checks each claim, but
against the truncated context window it assembled (``evidence["cited_texts"]``, a snapped and capped
window), and it records its own verdict (``row["checks"]`` and ``row["final"]``). Using those to
measure the loop would be circular: a window bug or a lenient verdict would hide exactly the
fabrication we want to catch.

This module is a second, independent oracle. For every claim in a real exported claim-ledger bundle
(the shape written by ``regsuper.observability.ledger_export``) it re-verifies the claim's asserted
fact tokens against the union of:

* the full provision text, fetched fresh from the database via ``NexusPort.fetch_provisions`` (the
  whole ``provision.text``, not the window the loop used); and
* the provision's ``practical_fact`` rows, via ``fact_lookup.lookup``.

A claim whose asserted number, §, date or value is not a verbatim substring of that union (after
whitespace collapsing only, never a numeric or date transform) is a fabrication.

Two properties make the check independent:

1. It never reads the loop's verdict: ``row["checks"]`` and ``row["final"]`` are ignored, and the
   re-checker forms its own verdict from the extracted tokens and the fresh sources.
2. It never reads the loop's context window: ``evidence["cited_texts"]`` and any other snapped or
   capped text in the bundle is ignored; the text is re-fetched for each cited provision id.

The oracle is pluggable (:class:`ReCheckOracle`), so the self-test runs a fixture oracle offline (no
database, deterministic) and the dry run can prime a live oracle (:func:`prime_live_oracle`) when a
database is reachable.

Per-claim verdicts are fed into the same ``claim_eval`` scorers (five metric families:
claim_precision, claim_recall, fabrication_catch_rate, demote_rate, repair_yield) through a
``ClaimOutcome``, so the report has the same shape as the loop-verdict harness but is computed from
the independent oracle.

Purity: token extraction reuses the atomizer's compiled regexes (extraction only locates the
asserted value; it is not a verdict), and the substring check only collapses whitespace. No LLM, no
clock, no randomness. The only I/O is the pluggable oracle (none for the fixture; SELECT-only reads
through the single Nexus door for the live one).

CLI:  python -m pythia.eval.fabrication_check --selftest
      python -m pythia.eval.fabrication_check --bundle <ledger.json> [--bundle ...] [--live] [--limit 10]
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pythia.reasoning.claim_eval import ClaimOutcome, fabrication_by_type, score_set
from pythia.reasoning.claim_ledger import CLAIM_TYPE
from pythia.reasoning.claims import _RE_AMOUNT, _RE_AUTHORITY, _RE_DEADLINE

# Claim types that carry a re-checkable asserted fact token (a number, date, § value or named body).
# The others (legal_rule, situation_application, procedure_step, gap) have no single verbatim value to
# re-ground token by token, so the oracle marks them 'not_applicable', never a false fabrication.
_FACT_TYPES: frozenset[str] = frozenset({"deadline", "amount", "authority"})

# Claim type to the compiled atomizer regexes that v_fact uses to extract tokens. Reusing them keeps the
# extracted span identical to the atomizer's typing signal. Extraction only; it never forms a verdict.
_TOKEN_RE: dict[str, list[re.Pattern[str]]] = {
    "deadline": [_RE_DEADLINE],
    "amount": [_RE_AMOUNT],
    "authority": [_RE_AUTHORITY],
}


def _collapse_ws(s: str) -> str:
    """Trim and fold internal whitespace runs to one space: the only permitted normalisation.

    Mirrors ``v_fact._collapse_ws``. Case and every digit and letter are kept verbatim, so '30 dnů' can
    never match '13 dnů' or '30dnů'.
    """
    return re.sub(r"\s+", " ", s or "").strip()


def _extract_tokens(claim_text: str, claim_type: str) -> list[str]:
    """Extract the asserted fact values from a claim's text, deduplicated and in order.

    Values are whitespace-collapsed. Deadlines use the practical-fact extractor (the full
    'do 30 dnů ...' phrase), as v_fact does, so a truncated typing-regex span cannot pass falsely.
    Amounts and authorities use the atomizer regex directly.
    """
    out: list[str] = []
    seen: set[str] = set()
    if claim_type == "deadline":
        from pythia.reasoning import practical_facts

        for f in practical_facts.extract_practical_facts(claim_text or ""):
            if f.fact_type == "deadline":
                tok = _collapse_ws(f.value_raw)
                if tok and tok not in seen:
                    seen.add(tok)
                    out.append(tok)
        return out
    for pat in _TOKEN_RE.get(claim_type, ()):
        for m in pat.finditer(claim_text or ""):
            tok = _collapse_ws(m.group(0))
            if tok and tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# Ledger adapter: read a real exported claim-ledger bundle into claim records.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class LedgerClaim:
    """One claim lifted from an exported claim-ledger bundle, keeping the loop's own claim ids.

    ``citations`` are the provision ids the atomizer grounded the claim to (the row's ``citations``
    list); the re-checker fetches fresh full text for exactly these ids. ``loop_final`` and
    ``loop_checks`` are carried only so the report can show where the oracle and the loop disagree;
    the re-checker's own verdict never reads them.
    """

    claim_id: str
    type: str
    text: str
    citations: tuple[str, ...]
    slot: str = ""
    # Carried for the audit diff only; the independent re-check must not read these.
    loop_final: str | None = None
    loop_checks: tuple[str, ...] = ()


def _iter_ledger_rows(bundle: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Return the ledger rows from an export bundle or a bare ``to_audit_dict`` ledger.

    Accepts both the export wrapper (``{'schema', 'ledger': {'rows': [...]}}``) and a ledger dict
    passed directly; anything else yields no rows.
    """
    if not isinstance(bundle, dict):
        return []
    ledger = bundle.get("ledger")
    if isinstance(ledger, dict) and isinstance(ledger.get("rows"), list):
        return ledger["rows"]
    if isinstance(bundle.get("rows"), list):  # a bare to_audit_dict ledger
        return bundle["rows"]
    return []


def bundle_to_claims(bundle: dict[str, Any]) -> list[LedgerClaim]:
    """Adapt a parsed ledger bundle into LedgerClaim records.

    Each row's ``citations`` (provision ids) become the re-check search space; ``final`` and the check
    names are carried for the audit diff only. Rows without a claim_id or text are skipped. Pure (no
    I/O).
    """
    claims: list[LedgerClaim] = []
    for row in _iter_ledger_rows(bundle):
        if not isinstance(row, dict):
            continue
        cid = row.get("claim_id")
        text = row.get("text")
        if not isinstance(cid, str) or not cid or not isinstance(text, str):
            continue
        cites = tuple(str(c) for c in (row.get("citations") or []) if c)
        checks = tuple(str(c.get("check")) for c in (row.get("checks") or []) if isinstance(c, dict) and c.get("check"))
        claims.append(
            LedgerClaim(
                claim_id=cid,
                type=str(row.get("type") or ""),
                text=text,
                citations=cites,
                slot=str(row.get("slot") or ""),
                loop_final=(str(row["final"]) if row.get("final") is not None else None),
                loop_checks=checks,
            )
        )
    return claims


def load_bundle(path: str | Path) -> dict[str, Any]:
    """Parse an exported ledger bundle JSON file from disk.

    The ``.json.sha256`` sidecar is not read here; a caller that wants to certify the bytes can verify
    it separately with ``build_canonical_payload``.
    """
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# The independent oracle: fresh full text plus practical_fact rows. Pluggable.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
class ReCheckOracle(Protocol):
    """Independent grounding source for the re-check.

    For a provision id, an implementation returns the full provision text and the practical_fact
    ``value_raw`` strings: the same two sources v_fact checks against, but fetched fresh rather than
    taken from the loop's context window. The fixture oracle is a plain dict; the live oracle reads
    through NexusPort and fact_lookup.
    """

    def provision_text(self, pid: str) -> str | None: ...
    def fact_values(self, pid: str) -> list[str]: ...


@dataclass
class FixtureOracle:
    """Deterministic, database-free oracle for the self-test or a canned dry-run slice.

    ``texts`` maps provision id to full text; ``facts`` maps provision id to practical_fact
    ``value_raw`` strings. A missing id yields nothing, an honest "no grounding for this id".
    """

    texts: dict[str, str] = field(default_factory=dict)
    facts: dict[str, list[str]] = field(default_factory=dict)

    def provision_text(self, pid: str) -> str | None:
        return self.texts.get(str(pid))

    def fact_values(self, pid: str) -> list[str]:
        return list(self.facts.get(str(pid), []))


async def prime_live_oracle(pids: Iterable[str]) -> FixtureOracle:
    """Prefetch fresh full text and practical_fact rows for the given provision ids.

    The result is a FixtureOracle-shaped cache, so the pure, synchronous re-check can run against real
    database ground truth without awaiting per token. Reads only through the single Nexus door
    (``fetch_provisions``) and the practical-fact lookup (``fact_lookup.lookup``), SELECT only.

    This is where the "fresh full text, not the context window" guarantee is realised: it fetches
    ``provision.text`` in full for every cited id, regardless of the truncated ``cited_texts`` the
    bundle carried.
    """
    from pythia.ports import NexusPort
    from pythia.reasoning import fact_lookup

    ids = [str(p) for p in dict.fromkeys(pids) if p]  # deduplicate, keeping order
    texts: dict[str, str] = {}
    facts: dict[str, list[str]] = {}
    if not ids:
        return FixtureOracle()
    provs = await NexusPort().fetch_provisions(ids)
    for pr in provs:
        if getattr(pr, "text", None):
            texts[str(pr.id)] = pr.text
    for pid in ids:
        rows = await fact_lookup.lookup(pid)
        vals = [r.value_raw for r in rows if getattr(r, "value_raw", None)]
        if vals:
            facts[pid] = vals
    return FixtureOracle(texts=texts, facts=facts)


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# The independent re-check.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# Per-claim re-check verdicts (the oracle's own outcome, never the loop's):
#   grounded:       every extracted fact token is a verbatim substring of the fresh text or fact rows.
#   fabricated:     at least one extracted token is in neither source (the value is not in the law).
#   no_citation:    the claim cites no provision to search, so it cannot be grounded (not scored).
#   no_token:       a fact-typed claim with no extractable fact token (nothing to re-check).
#   not_applicable: a non-fact claim type (legal rule, situation, procedure, gap); no single value.
RECHECK_VERDICT: tuple[str, ...] = (
    "grounded",
    "fabricated",
    "no_citation",
    "no_token",
    "not_applicable",
)


@dataclass(frozen=True)
class ReCheckResult:
    """Independent re-check outcome for one claim.

    ``missing_tokens`` are the asserted tokens found in neither fresh source (the fabrication
    evidence). ``searched_pids`` records exactly which provisions' fresh text and fact rows were
    searched, documenting that the check re-fetched them instead of using the loop's context window.
    """

    claim_id: str
    claim_type: str
    verdict: str  # one of RECHECK_VERDICT
    extracted_tokens: tuple[str, ...] = ()
    missing_tokens: tuple[str, ...] = ()
    searched_pids: tuple[str, ...] = ()


def _token_grounded(token: str, pids: Iterable[str], oracle: ReCheckOracle) -> bool:
    """Return True if ``token`` is a verbatim substring of any cited provision's fresh sources.

    The sources are each cited provision's fresh full text and its practical_fact ``value_raw`` rows.
    Whitespace is collapsed identically in token and haystack; nothing else is transformed (no numeric
    or date normalisation).
    """
    tok = _collapse_ws(token)
    if not tok:
        return False
    for pid in pids:
        full = oracle.provision_text(pid)
        if isinstance(full, str) and tok in _collapse_ws(full):
            return True
        for val in oracle.fact_values(pid):
            if tok in _collapse_ws(val):
                return True
    return False


def recheck_claim(claim: LedgerClaim, oracle: ReCheckOracle) -> ReCheckResult:
    """Re-verify one claim's asserted fact tokens against the oracle's fresh sources.

    Reads only ``claim.type``, ``claim.text`` and ``claim.citations`` plus the oracle, never the loop's
    ``loop_final`` or ``loop_checks`` and never a context window. A fact-typed claim with an asserted
    token absent from both fresh sources is a fabrication.
    """
    ctype = claim.type
    if ctype not in _FACT_TYPES:
        return ReCheckResult(claim.claim_id, ctype, "not_applicable")
    tokens = _extract_tokens(claim.text, ctype)
    if not tokens:
        return ReCheckResult(claim.claim_id, ctype, "no_token")
    pids = tuple(dict.fromkeys(claim.citations))
    if not pids:
        return ReCheckResult(claim.claim_id, ctype, "no_citation", extracted_tokens=tuple(tokens))
    missing = [t for t in tokens if not _token_grounded(t, pids, oracle)]
    verdict = "fabricated" if missing else "grounded"
    return ReCheckResult(
        claim.claim_id,
        ctype,
        verdict,
        extracted_tokens=tuple(tokens),
        missing_tokens=tuple(missing),
        searched_pids=pids,
    )


def recheck_bundle(bundle: dict[str, Any], oracle: ReCheckOracle) -> list[ReCheckResult]:
    """Run the independent re-check over every claim in a parsed ledger bundle. Pure given the oracle."""
    return [recheck_claim(c, oracle) for c in bundle_to_claims(bundle)]


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# Feed the independent verdicts into the claim_eval scorers (five metric families, same report shape).
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def results_to_outcome(results: list[ReCheckResult]) -> ClaimOutcome:
    """Map independent re-check verdicts to a ClaimOutcome in the same claim-id space.

    This lets the claim_eval scorers apply unchanged. 'grounded' becomes verified (the oracle
    affirmatively grounded the claim); every other verdict becomes unverified, because the oracle made
    no affirmative grounding. A fabricated claim is therefore correctly not verified, that is, caught.
    """
    verdicts = {r.claim_id: ("verified" if r.verdict == "grounded" else "unverified") for r in results}
    return ClaimOutcome(verdicts=verdicts)


def results_to_gold_item(bundle_id: str, results: list[ReCheckResult]) -> dict[str, Any]:
    """Build a claim_eval gold item whose per-claim ``fabricated`` flag is the oracle's verdict.

    This lets the standard claim_eval scorers rate the loop later: these gold labels plus a loop
    outcome give the five metric families. Only re-checkable claims (fact-typed, with a token) get a
    hard ``fabricated`` flag; not_applicable, no_token and no_citation claims are excluded because they
    have no independent ground truth. Every label is provisional (``source: draft``).
    """
    claims = []
    for r in results:
        if r.verdict not in ("grounded", "fabricated"):
            continue  # only claims with an independent verdict become gold
        claims.append({"id": r.claim_id, "type": r.claim_type, "fabricated": (r.verdict == "fabricated")})
    return {
        "id": bundle_id,
        "verified": False,
        "source": "draft",
        "draft": f"(reconstructed from ledger bundle {bundle_id})",
        "claims": claims or [{"id": f"{bundle_id}::_none", "type": "gap", "fabricated": False}],
        "notes": "AQ/VL-G-09 independent-oracle gold — fabricated flag is the fresh-full-text re-check verdict.",
    }


# The five metric families the report emits per claim type; mirrors claim_eval's scorers exactly.
METRIC_FAMILIES: tuple[str, ...] = (
    "claim_precision",
    "claim_recall",
    "fabrication_catch_rate",
    "demote_rate",
    "repair_yield",
)


def dry_run_report(bundles: list[dict[str, Any]], oracle: ReCheckOracle, *, limit: int = 10) -> dict[str, Any]:
    """Re-check a slice of claims and report metrics per claim type.

    Runs the independent re-check over the first ``limit`` claims (default 10) of the supplied bundles
    and reports, per claim type, the five metric families (via the same claim_eval scorers), a
    histogram of independent verdicts, and the oracle-versus-loop diff (how often the oracle disagreed
    with the loop's own final status). Deterministic given the oracle.

    The report is explicit about provenance: with a FixtureOracle (no real database reached) it is
    stamped ``oracle="fixture"`` and the numbers are only a placeholder over the fixture slice. The
    self-test is the real acceptance check, not these numbers.
    """
    all_claims: list[LedgerClaim] = []
    for b in bundles:
        all_claims.extend(bundle_to_claims(b))
    sliced = all_claims[:limit] if limit and limit > 0 else all_claims

    results = [recheck_claim(c, oracle) for c in sliced]
    outcome = results_to_outcome(results)

    # Independent-verdict histogram and the oracle-versus-loop diff (per claim type).
    verdict_hist: dict[str, int] = {v: 0 for v in RECHECK_VERDICT}
    per_type_hist: dict[str, dict[str, int]] = {t: {v: 0 for v in RECHECK_VERDICT} for t in CLAIM_TYPE}
    oracle_vs_loop: dict[str, int] = {"agree": 0, "oracle_flags_loop_verified": 0, "other_disagree": 0}
    for c, r in zip(sliced, results, strict=True):
        verdict_hist[r.verdict] = verdict_hist.get(r.verdict, 0) + 1
        per_type_hist.setdefault(r.claim_type, {v: 0 for v in RECHECK_VERDICT})[r.verdict] += 1
        # Did the oracle disagree with the loop's own verdict? Audit information only, never a signal.
        loop_verified = c.loop_final in ("verified", "repaired_verified")
        oracle_grounded = r.verdict == "grounded"
        if loop_verified == oracle_grounded:
            oracle_vs_loop["agree"] += 1
        elif loop_verified and not oracle_grounded:
            oracle_vs_loop["oracle_flags_loop_verified"] += 1  # loop said verified, oracle says not grounded
        else:
            oracle_vs_loop["other_disagree"] += 1

    # The five metric families per claim type, via the claim_eval scorers over a per-type gold slice.
    gold_item = results_to_gold_item("dry_run_slice", results)
    per_type_metrics: dict[str, dict[str, Any]] = {}
    for t in CLAIM_TYPE:
        t_claims = [c for c in gold_item["claims"] if c["type"] == t]
        if not t_claims:
            per_type_metrics[t] = {m: None for m in METRIC_FAMILIES}
            continue
        t_item = {**gold_item, "id": f"dry_run::{t}", "claims": t_claims}
        # Score the independent gold against the same independent outcome. This is self-consistent by
        # construction and shows the five families compute per type; the loop's own outcome can be
        # scored against the same gold.
        rep = score_set([t_item], [outcome])
        per_type_metrics[t] = {m: rep["overall"][m] for m in METRIC_FAMILIES}

    return {
        "schema": "fabrication_recheck_report_v1",
        "provisional": True,
        "independent": True,
        "independence_note": (
            "Verdicts are re-derived from FRESH full provision.text (NexusPort.fetch_provisions) ∪ "
            "practical_fact rows (fact_lookup) — the loop's checks/final AND ctx windows are NOT consulted."
        ),
        "oracle": "fixture" if isinstance(oracle, FixtureOracle) else "live",
        "n_claims": len(sliced),
        "limit": limit,
        "recheck_verdicts": verdict_hist,
        "recheck_verdicts_by_type": {t: h for t, h in per_type_hist.items() if any(h.values())},
        "oracle_vs_loop_diff": oracle_vs_loop,
        "metric_families_by_type": per_type_metrics,
        "fabrication_by_type": fabrication_by_type([gold_item], [outcome]),
    }


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# Self-test: a planted synthetic fabrication (offline, deterministic).
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def _selftest_bundle_and_oracle() -> tuple[dict[str, Any], FixtureOracle]:
    """Build a ledger bundle with a planted fabrication and a grounded twin, plus a fixture oracle.

    The oracle's full text contains the grounded value but not the fabricated one. Deterministic.

    Independence trap: the fabricated row's ``checks`` say the loop passed it (final='verified'), so a
    re-check that trusted the loop's verdict would miss the fabrication. The oracle must catch it
    anyway.
    """
    grounded_pid = "PID-GROUNDED"
    fab_pid = "PID-FAB"
    bundle = {
        "schema": "claim-ledger-export-v1",
        "ledger": {
            "schema": "claim_ledger_v1",
            "rows": [
                # Grounded amount: "148 Kč" is in the fixture full text, so the oracle must not flag it.
                {
                    "claim_id": "c-grounded",
                    "type": "amount",
                    "text": "Stravné činí 148 Kč za den [475/2024 § 2].",
                    "citations": [grounded_pid],
                    "slot": "Hodnocení",
                    "checks": [{"check": "v_fact", "status": "pass", "reason": "fact_tokens_grounded"}],
                    "final": "verified",
                },
                # Planted fabrication: "353 Kč" is absent from the fixture full text (which only has
                # 148 Kč), so the oracle must flag it even though the loop stamped it verified.
                {
                    "claim_id": "c-fabricated",
                    "type": "amount",
                    "text": "Stravné činí 353 Kč za den [475/2024 § 2].",
                    "citations": [fab_pid],
                    "slot": "Hodnocení",
                    "checks": [{"check": "v_fact", "status": "pass", "reason": "fact_tokens_grounded"}],
                    "final": "verified",
                },
                # A non-fact claim: not_applicable (no false fabrication).
                {
                    "claim_id": "c-rule",
                    "type": "legal_rule",
                    "text": "Zaměstnavatel je povinen vést evidenci [262/2006 § 96].",
                    "citations": [grounded_pid],
                    "slot": "Legislativní rámec",
                    "checks": [],
                    "final": "shadow",
                },
            ],
        },
    }
    oracle = FixtureOracle(
        # The fresh full text: contains the grounded 148 Kč but not the fabricated 353 Kč.
        texts={
            grounded_pid: "Za jeden den pracovní cesty přísluší zaměstnanci stravné 148 Kč, trvá-li cesta "
            "5 až 12 hodin.",
            fab_pid: "Za jeden den pracovní cesty přísluší zaměstnanci stravné 148 Kč, trvá-li cesta 5 až 12 hodin.",
        },
        # A practical_fact row for the grounded pid (also 148 Kč): the second grounding source.
        facts={grounded_pid: ["148 Kč, trvá-li pracovní cesta 5 až 12 hodin,"]},
    )
    return bundle, oracle


def self_test() -> dict[str, Any]:
    """Run the database-free self-test and return its dry-run report.

    Plants a known fabrication (a value absent from the provision text) and asserts that the
    independent re-check catches it while a grounded claim is not flagged. Also checks that the dry run
    emits the five metric families per claim type. Raises AssertionError on any inconsistency, so
    pytest and the CLI can rely on it.
    """
    bundle, oracle = _selftest_bundle_and_oracle()
    results = {r.claim_id: r for r in recheck_bundle(bundle, oracle)}

    # 1. The planted fabrication is caught, although the loop stamped it verified.
    fab = results["c-fabricated"]
    assert fab.verdict == "fabricated", fab
    assert "353 Kč" in fab.missing_tokens, fab
    # The loop's own verdict on it was 'verified', so catching it shows the check is independent.
    loop_final = next(r["final"] for r in bundle["ledger"]["rows"] if r["claim_id"] == "c-fabricated")
    assert loop_final == "verified", "self-test must plant a loop-verified fabrication (the independence trap)"

    # 2. The grounded claim is not flagged.
    ok = results["c-grounded"]
    assert ok.verdict == "grounded", ok
    assert ok.missing_tokens == (), ok

    # 3. A non-fact claim is not_applicable (never a false fabrication).
    assert results["c-rule"].verdict == "not_applicable", results["c-rule"]

    # 4. The dry run emits the five metric families per claim type over the slice.
    report = dry_run_report([bundle], oracle, limit=10)
    for t in CLAIM_TYPE:
        fams = report["metric_families_by_type"][t]
        assert set(fams) == set(METRIC_FAMILIES), (t, fams)
    # The amount type has one fabricated and one grounded claim, so its catch rate is 1.0.
    assert report["fabrication_by_type"]["amount"] == 1.0, report["fabrication_by_type"]
    # The oracle disagreed with the loop on exactly the planted fabrication (loop verified, oracle
    # not grounded).
    assert report["oracle_vs_loop_diff"]["oracle_flags_loop_verified"] == 1, report["oracle_vs_loop_diff"]
    assert report["recheck_verdicts"]["fabricated"] == 1, report["recheck_verdicts"]
    assert report["recheck_verdicts"]["grounded"] == 1, report["recheck_verdicts"]
    return report


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# CLI: self-test, or a dry run over real bundles (fixture oracle by default; --live uses the database).
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def _run_cli(args: argparse.Namespace) -> int:
    if args.selftest:
        rep = self_test()
        print(
            "SELF-TEST OK — the seeded fabrication was CAUGHT by the independent re-check (loop had it "
            "'verified'); the grounded claim was NOT flagged."
        )
        print(
            json.dumps(
                {
                    "recheck_verdicts": rep["recheck_verdicts"],
                    "fabrication_by_type": rep["fabrication_by_type"],
                    "oracle_vs_loop_diff": rep["oracle_vs_loop_diff"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not args.bundle:
        # No bundles supplied: emit a clearly labelled placeholder report over the self-test fixture.
        bundle, oracle = _selftest_bundle_and_oracle()
        rep = dry_run_report([bundle], oracle, limit=args.limit)
        rep["placeholder"] = True
        rep["placeholder_reason"] = (
            "No exported ledger bundle supplied (--bundle) — this is a SKELETON dry-run over the self-test "
            "fixture slice. Point --bundle at a real data/exports/claim_ledgers/*.json to score live claims."
        )
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0

    bundles = [load_bundle(p) for p in args.bundle]
    if args.live:
        import asyncio

        pids = {pid for b in bundles for c in bundle_to_claims(b) for pid in c.citations}
        oracle: ReCheckOracle = asyncio.run(prime_live_oracle(pids))
    else:
        oracle = FixtureOracle()  # no DB: every token is ungrounded (placeholder numbers, labelled)
    rep = dry_run_report(bundles, oracle, limit=args.limit)
    if not args.live:
        rep["placeholder"] = True
        rep["placeholder_reason"] = (
            "Ran WITHOUT --live: the oracle has no fresh full text, so grounding numbers are a SKELETON. "
            "Re-run with --live against a reachable pythia DB to compute real independent verdicts."
        )
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    """Parse command-line arguments and run the self-test or a dry run."""
    ap = argparse.ArgumentParser(description="VL-G-09 independent fabrication re-check + ledger dry-run")
    ap.add_argument(
        "--selftest", action="store_true", help="run the DB-free seeded-fabrication self-test (the hard acceptance)"
    )
    ap.add_argument(
        "--bundle", action="append", default=[], help="path to a real exported claim-ledger bundle JSON (repeatable)"
    )
    ap.add_argument(
        "--live",
        action="store_true",
        help="prime the LIVE oracle from the pythia DB (fresh full text ∪ practical_fact rows)",
    )
    ap.add_argument("--limit", type=int, default=10, help="dry-run slice size (default 10)")
    return _run_cli(ap.parse_args())


if __name__ == "__main__":  # pragma: no cover — operator CLI
    raise SystemExit(main())
