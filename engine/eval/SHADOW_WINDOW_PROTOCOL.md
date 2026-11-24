# Shadow-Window Protocol — the ≥100-answer claim-verify soak (WS11 / B4 · step-89)

This is the step-89 "missing half": the mechanical protocol for the **shadow-window soak** that must run
BEFORE any `claim_verify_mode=enforce` flip. It is the acceptance ledger that turns "the machinery is built"
into "the machinery is measured, stable, and safe to enforce."

**Posture under test.** The stack runs in the *shadow* claim-verify posture — the enforce assembly, EvidenceBundle,
term-norm/softlaw, structured-answer, intake-active, etc. are all **ON**, but `claim_verify_mode=shadow`, so the
loop atomizes → verifies → writes a ledger **without demoting/dropping/repairing the served answer**. metagate
stays `shadow` (contract rule 5). Model = gpt-5.5 (same as the ON eval arm, per step 86). This is the ON arm's
engine posture minus the enforce write-back — the soak measures what the enforce loop WOULD do, at scale, from
the shadow ledgers, before the served answer is ever altered.

**Why shadow first.** Enforce changes the served answer (demote/drop/repair). You cannot certify that change is
safe from zero live evidence. The shadow window produces ≥100 real claim ledgers under the real model + corpus,
so every enforce decision can be scored (independent oracle) and every stability/cost/latency envelope can be
measured, with the served answer still byte-identical to today. Only after the ledger passes this protocol does
the enforce flip become a data-backed decision instead of a leap.

---

## How to run the window

1. Boot the shadow posture (app-side runner `run_aq_all.sh` gives the ON engine flags; for the SOAK, set
   `PYTHIA_CLAIM_VERIFY_MODE=shadow` instead of `enforce`). Confirm the AQ-06 env-manifest dump landed in
   `manifests/` — it is the posture-of-record for this window.
2. Drive **≥100 answers** through the served `/v1/answer` (:8099). The `gold_layman_v1.jsonl` 100-item set is the
   canonical driver; a larger window may repeat classes or add operator queries. Run it with:

   ```
   python eval/aq_answer_eval.py --posture-tag on --out eval/artifacts/deploy_shadow_window.json \
       --ledger-dir ../../Polarius/data/exports/claim_ledgers --ledger-live --ledger-limit 400
   ```

   `--ledger-dir` folds the step-72 independent fabrication re-check over the ledgers the window wrote;
   `--ledger-live` re-grounds against the pythia DB (the honest oracle).
3. Snapshot `/metrics` (`curl -s http://127.0.0.1:8099/metrics > eval/artifacts/shadow_window_metrics.json`)
   at the END of the window. The claim-loop counters are cumulative over the process lifetime, so a single
   end-of-window snapshot is the whole-window aggregate (boot the engine fresh for the window so the counters
   start at zero).
4. Fill the checklist below from that ONE metrics snapshot + the eval artifact + the on-disk ledger files.
   **No bespoke SQL, no ad-hoc queries** — every row is a named JSON path.

---

## The mechanical checklist

Every row is filled from `/metrics` (the telemetry snapshot), the `deploy_shadow_window.json` eval artifact, or
a `ls | wc -l` over the exported-ledger directory. The threshold column is the pass bar; a row that fails blocks
the enforce flip (or, where noted, is a soft watch).

| # | Row | Threshold (PASS bar) | Filled from |
|---|---|---|---|
| 1 | **Per-type pass-rate stability** — the per-check pass rate must not swing wildly across the window | ≤ **10 pt** swing (max − min) per check over the window's halves | `/metrics` → `claim_loop.check_pass_rate_pct.<check>` (v_fact / v_cit / …); compute over the first-half vs second-half metrics snapshots (snapshot at n=50 and n=100), or per-class from the eval artifact per-class splits |
| 2 | **Verified-claim recall** — of the truly-groundable claims, the loop must verify ≥90% | ≥ **0.90** | `deploy_shadow_window.json` → the `fabrication` block's independent oracle: `recheck_verdicts.grounded / (grounded + no_token + fabricated over fact types)`, cross-checked with `/metrics` → `claim_loop.per_status` (verified vs total). The eval `citation_precision` + `recall@k` per-class corroborate |
| 3 | **Fix-loop-2 rate** — fraction of answers needing a SECOND repair round must stay low | < **20%** | `/metrics` → `claim_loop.fix_loop.rounds` vs `claim_loop.events` — `(rounds − events) / events` approximates the multi-round share; `fix_loop.repaired` / `events` for the repair-triggered share. (Shadow runs no repair, so this row is measured on an enforce-*dry-run* pass or read as the demote candidate rate `per_status.demoted_gap / claims_total`.) |
| 4 | **p95 latency envelope** — enabling the loop must not blow the latency budget | ≤ **+50%** vs the OFF baseline p95 | `/metrics` → `latency_ms["/v1/answer"].p95` (this window) vs the OFF baseline artifact's `latency_ms["/v1/answer"].p95`. Per-stage attribution: `stage_latency_ms.<stage>.p95` + `claim_loop.stage_latency_ms.claim_verify_ms.p95` |
| 5 | **$/query envelope + budget breaches** — per-query LLM spend bounded, no runaway loop | `loop_budget_exceeded` ≈ **0**; $/query within the committed envelope | `/metrics` → `claim_loop.loop_budget_exceeded` (must be ~0) + `llm_usage.by_stage.<stage>.tokens_input/output` for the claim-loop stages (`claim_entailment`, `claim_repair_redraft`, `atomize_assist`) → cost = tokens × the app price book. Join to the app's costed rows via the manifest `rids` |
| 6 | **≥50 sha256 ledger artifacts** — the window produced a durable, hash-sealed audit trail | ≥ **50** `.json` + matching `.json.sha256` pairs | `ls data/exports/claim_ledgers/*.json | wc -l` (app repo) and confirm each has a sibling `.json.sha256`. The eval artifact's `fabrication.n_bundles` echoes the count folded in |

**Watch (soft, not a hard block):** `quality_tripwires.per_tripwire` (0-grounded / empty-practical tripwire
fires) and `verdict_split.refuse_pct` — a sudden refuse spike vs the OFF baseline is a metagate/coverage
regression signal, investigated but not auto-blocking (metagate stays shadow either way).

---

## How each row maps to no bespoke query

- **Rows 1, 3, 4, 5** read `/metrics` JSON paths directly — the telemetry module already aggregates the
  claim-loop counters, per-stage latency rings, fix-loop histogram, budget-breach count, and per-stage LLM
  token totals (`pythia/telemetry.py::_claim_loop_metrics` / `_stage_latency_metrics` / the `llm_usage` block).
  Nothing is computed from raw SQL; the counters are bumped by the `kind="claim_verify"` trace the loop already
  records when `claim_verify_enabled` is ON.
- **Row 2** reads the eval artifact's `fabrication` block, which is the step-72 **independent** re-checker
  (`pythia/eval/fabrication_check.py`) run over the on-disk ledgers with `--ledger-live` — it re-grounds each
  fact claim against the FRESH full provision text, so verified-claim recall is measured against ground truth,
  not the loop's own (potentially circular) verdict. The per-class `recall@k` / `citation_precision` in the same
  artifact corroborate at the answer level.
- **Row 6** is a filesystem count of the `ledger_export` bundles (`ledger_export.py`, the step-70 canonical-JSON
  + `.sha256` sidecar writer). Each bundle is content-hashed, so the count IS the count of hash-sealed audit
  artifacts.

The eval artifact + the `/metrics` snapshot + the ledger dir listing are the THREE inputs; every checklist row
is a named read of one of them. The `aq_compare.py` memo (OFF vs the shadow-window ON arm) carries the headline
Answer-Recall Δ + grounded no-regression + fabrication-per-type for the WS13 gate packet alongside this ledger.

---

## Exit criteria → the enforce flip

The shadow window is PASSED when all six rows meet their PASS bar over a ≥100-answer window AND the
`aq_compare` grounded-no-regression check is PASS (no OFF-grounded answer regressed under the ON posture). Even
then, the enforce flip stays **lawyer/operator-gated** (the flip-gate registry — `Verification Loop Flip
Gates.md`): a passing soak is a NECESSARY, not sufficient, condition. The gold labels remain 0/100
lawyer-verified, so every number here is **DIRECTIONAL** until gate #1 (lawyer certification) lands — this
protocol makes the soak reproducible and the decision data-backed; it does not by itself certify the numbers.
