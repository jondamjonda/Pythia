"""VL-G-09 — the INDEPENDENT fabrication re-check tests (PURE: no DB, no engine, no LLM, no network).

The hard acceptance is the SEEDED SYNTHETIC FABRICATION self-test: plant a value ABSENT from the provision
text and assert the re-checker CATCHES it — even though the loop stamped that claim 'verified' (the
independence trap) — while a genuinely-grounded claim is NOT flagged. Also covers:
  • the re-check is INDEPENDENT of the loop's verdict AND of any ctx window (it re-derives from the oracle);
  • the ledger adapter reads the step-70 bundle shape (and a bare to_audit_dict ledger);
  • whitespace-collapse-only matching (no numeric/date transform: a wrong number never false-passes);
  • the 10-item dry-run emits the FIVE metric families PER claim type over a fixture slice.

RULES: run ONLY this file (the host is memory-constrained). NEVER bare `pytest tests/` (live-DB hang).
"""

from __future__ import annotations

from pythia.eval import fabrication_check as fc


# ═════════ the SEEDED-FABRICATION self-test (hard acceptance) ═════════
def test_selftest_catches_seeded_fabrication_and_clears_grounded():
    # self_test() raises on any inconsistency; a clean return IS the acceptance.
    report = fc.self_test()
    assert report["independent"] is True
    assert report["recheck_verdicts"]["fabricated"] == 1
    assert report["recheck_verdicts"]["grounded"] == 1


def test_recheck_is_independent_of_loop_verdict():
    # The planted fabrication carries final='verified' + a passing v_fact check in the bundle. The independent
    # re-check must flag it fabricated ANYWAY — proving it never consults loop_final / loop_checks.
    bundle, oracle = fc._selftest_bundle_and_oracle()
    by_id = {r.claim_id: r for r in fc.recheck_bundle(bundle, oracle)}
    fab = by_id["c-fabricated"]
    assert fab.verdict == "fabricated"
    assert "353 Kč" in fab.missing_tokens
    # the same claim the loop stamped verified:
    loop_final = next(r["final"] for r in bundle["ledger"]["rows"] if r["claim_id"] == "c-fabricated")
    assert loop_final == "verified"


def test_recheck_ignores_ctx_window_uses_fresh_full_text():
    # Even if the bundle carried a TRUNCATED ctx window that happened to contain the value, the re-check reads
    # ONLY the oracle (fresh full text) — here the oracle does NOT contain the token → still fabricated.
    claim = fc.LedgerClaim(
        claim_id="x", type="amount", text="Pokuta až 5 000 000 Kč [1/2020 § 1].", citations=("PID-X",)
    )
    # a ctx window would have this text; the oracle (fresh full text) does NOT.
    oracle = fc.FixtureOracle(texts={"PID-X": "Za přestupek lze uložit pokutu do 100 000 Kč."})
    res = fc.recheck_claim(claim, oracle)
    assert res.verdict == "fabricated"
    assert res.searched_pids == ("PID-X",)  # it re-searched the fresh source, not a window


def test_grounded_claim_via_full_text_and_via_fact_row():
    # (a) grounded because the value is in the FRESH full text
    c1 = fc.LedgerClaim(claim_id="a", type="amount", text="Stravné 148 Kč [x § 2].", citations=("P",))
    o1 = fc.FixtureOracle(texts={"P": "… stravné 148 Kč, trvá-li cesta 5 až 12 hodin …"})
    assert fc.recheck_claim(c1, o1).verdict == "grounded"
    # (b) grounded because the value is in a practical_fact value_raw row (full text lacks it)
    c2 = fc.LedgerClaim(claim_id="b", type="amount", text="Stravné 148 Kč [x § 2].", citations=("P",))
    o2 = fc.FixtureOracle(texts={"P": "irelevantní text"}, facts={"P": ["148 Kč, trvá-li cesta 5 až 12 h"]})
    assert fc.recheck_claim(c2, o2).verdict == "grounded"


def test_whitespace_collapse_only_no_numeric_transform():
    # "148 Kč" must NOT match a haystack that only has "13 Kč" or "1 480 Kč" — ws-collapse never edits digits.
    c = fc.LedgerClaim(claim_id="a", type="amount", text="Stravné 148 Kč [x § 2].", citations=("P",))
    wrong = fc.FixtureOracle(texts={"P": "stravné 13 Kč a jinde 1 480 Kč"})
    assert fc.recheck_claim(c, wrong).verdict == "fabricated"
    # but an internal whitespace RUN in the source still matches (collapse applied to both sides)
    spaced = fc.FixtureOracle(texts={"P": "stravné 148   Kč za den"})
    assert fc.recheck_claim(c, spaced).verdict == "grounded"


# ═════════ verdict taxonomy edge cases ═════════
def test_non_fact_claim_is_not_applicable():
    c = fc.LedgerClaim(claim_id="r", type="legal_rule", text="Podnikatel musí ohlásit živnost.", citations=("P",))
    assert fc.recheck_claim(c, fc.FixtureOracle()).verdict == "not_applicable"


def test_no_citation_claim():
    c = fc.LedgerClaim(claim_id="n", type="amount", text="Stravné 148 Kč.", citations=())
    assert fc.recheck_claim(c, fc.FixtureOracle()).verdict == "no_citation"


def test_no_token_claim():
    # an 'amount'-typed claim whose text carries no extractable amount token
    c = fc.LedgerClaim(claim_id="t", type="amount", text="Toto je jen věta bez čísla.", citations=("P",))
    assert fc.recheck_claim(c, fc.FixtureOracle(texts={"P": "cokoli"})).verdict == "no_token"


# ═════════ ledger adapter ═════════
def test_bundle_adapter_reads_step70_shape():
    bundle, _ = fc._selftest_bundle_and_oracle()
    claims = fc.bundle_to_claims(bundle)
    assert {c.claim_id for c in claims} == {"c-grounded", "c-fabricated", "c-rule"}
    fab = next(c for c in claims if c.claim_id == "c-fabricated")
    assert fab.citations == ("PID-FAB",)
    assert fab.loop_final == "verified"  # carried for audit-diff only
    assert "v_fact" in fab.loop_checks


def test_bundle_adapter_reads_bare_ledger():
    # a to_audit_dict ledger passed directly (no export wrapper)
    bare = {
        "schema": "claim_ledger_v1",
        "rows": [{"claim_id": "z", "type": "amount", "text": "148 Kč [x § 2].", "citations": ["P"], "final": "shadow"}],
    }
    claims = fc.bundle_to_claims(bare)
    assert len(claims) == 1 and claims[0].claim_id == "z"


def test_bundle_adapter_tolerates_junk():
    assert fc.bundle_to_claims({}) == []
    assert fc.bundle_to_claims({"ledger": {"rows": ["not-a-dict", {"no_claim_id": 1}]}}) == []


# ═════════ 10-item dry-run: five metric families PER claim type ═════════
def test_dry_run_emits_five_metric_families_per_type():
    bundle, oracle = fc._selftest_bundle_and_oracle()
    report = fc.dry_run_report([bundle], oracle, limit=10)
    for t in fc.CLAIM_TYPE:
        fams = report["metric_families_by_type"][t]
        assert set(fams) == set(fc.METRIC_FAMILIES), (t, fams)
    # amount had 1 fabricated (caught) → catch-rate 1.0; other types had no fabricated amount → N/A
    assert report["fabrication_by_type"]["amount"] == 1.0
    assert report["n_claims"] == 3
    assert report["oracle"] == "fixture"


def test_dry_run_limit_slices():
    bundle, oracle = fc._selftest_bundle_and_oracle()
    report = fc.dry_run_report([bundle], oracle, limit=2)
    assert report["n_claims"] == 2


def test_results_to_outcome_collapse():
    # grounded → verified; everything else → unverified (a fabricated claim is thus 'caught' = not verified)
    results = [
        fc.ReCheckResult("g", "amount", "grounded"),
        fc.ReCheckResult("f", "amount", "fabricated"),
        fc.ReCheckResult("n", "amount", "no_citation"),
    ]
    oc = fc.results_to_outcome(results)
    assert oc.verdict_for("g") == "verified"
    assert oc.verdict_for("f") == "unverified"
    assert oc.verdict_for("n") == "unverified"


def test_results_to_gold_item_only_hard_verdicts():
    # only grounded/fabricated claims become independent gold; NA/no-token are excluded (no ground truth)
    results = [
        fc.ReCheckResult("g", "amount", "grounded"),
        fc.ReCheckResult("f", "amount", "fabricated"),
        fc.ReCheckResult("r", "legal_rule", "not_applicable"),
    ]
    item = fc.results_to_gold_item("b1", results)
    ids = {c["id"]: c["fabricated"] for c in item["claims"]}
    assert ids == {"g": False, "f": True}
    assert item["verified"] is False and item["source"] == "draft"
