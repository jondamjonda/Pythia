# Operator decision memo — the AQ Q-list (WS13 step 102)

> **Provenance.** Assembled 2026-07-08 for WS13 step 102 from `DEPLOYMENT_PLAN.md` (steps
> 71/75/94-prep/102), the flip-gate registries (both repos), `data/limits.json`, and the WS11
> measurement status. **One line per decision; execution command pre-written.** This memo
> COLLECTS the decisions for one operator signature pass — it does NOT make them. No flag is
> flipped and no number is invented here; where an input is not yet produced the line says so.
>
> **How to use.** For each row: read the decision + recommendation, then either run the
> pre-written command (if it is a ratify/record) or record the decision in `GATES_STATUS.md`
> (if it books/gates something). Nothing user-visible flips from this memo — that stays gated
> by `GATES_STATUS.md`.

Cross-links: `docs/deploy/GATES_STATUS.md` · `docs/deploy/deployment-amendments.md` · step-71
registries (both repos).

---

| # | Decision | Recommendation | Pre-written execution / record | Status |
|---|---|---|---|---|
| **Q2** | Authority registry **external fetch** (real directory loader, replaces the DRAFT seeds). | AUTHORIZE under recorded Q2 — unblocks step 94. | Record "Q2 = AUTHORIZE" in `GATES_STATUS.md`; then step 94 runs: loader → 30 municipalities × 3 authority types = 100% match validation → replace DRAFT seeds → install refresh cron + diff alarm. **Until recorded, the fetch stays gated; DRAFT rows render with visible source+date.** | PENDING operator signature |
| **Q3** | Fusion path — path **(a)** (RRF re-rank scaffold) is built; ratify or pick another path. | RATIFY path (a). | Record "Q3 = ratify path (a)" in `GATES_STATUS.md`. `fusion_enabled` stays default-OFF; **activation/calibration is a separate gate-#3 retrieval A/B on gold** (rides the n=70 batch), not this ratification. | PENDING operator signature |
| **Q4** | Sub-§ rechunk — the reversible additive `provision_clause` index (per contract §5, decision point is post-B1-eval). | DEFER to the post-B1-eval decision point (recommend the reversible additive index when taken). | Record "Q4 = deferred to post-B1-eval" in `GATES_STATUS.md`. **No command now** — the decision point is after the B1 answer-grain eval (rides the n=70 batch, step 88). | PENDING (blocked on: B1 eval / step 88 — n=70 batch not yet run) |
| **Q5** | Lawyer **label-session booking** (the amortized gate-#1 session). | BOOK the session; amortize with the owed AC-METAGATE independent eval. | Record the booked date (or the booking attempt + escalation) in `GATES_STATUS.md` gate-#1 row. Packet is ready: `gate1-labels/` (audit folder). | **✅ DONE 2026-07-08 — lawyer SIGNED gate #1 (+ #3/#4/#5). AQ-03 gold CERTIFIED (CP4).** AC-METAGATE independent eval (step 95) still owed. |
| **Q6** | k-budget **12→24** (the `k_eff` cap is built DARK). | RATIFY the raise; keep it behind its dark flag until the retrieval A/B. | Record "Q6 = ratify 12→24" in `GATES_STATUS.md`. The `k_eff` cap stays dark until gate-#3 retrieval A/B on gold (rides the n=70 batch). | PENDING operator signature |
| **—** | **'pokyn' taxonomy governance** + **VL-F-12 per-connector lawyer scope** — soft-law FETCH stays blocked until BOTH. | Keep soft-law FETCH BLOCKED; record both as open governance items. | Record "soft-law FETCH = BLOCKED pending pokyn-taxonomy governance + VL-F-12 per-connector lawyer scope" in `GATES_STATUS.md`. **No fetch command is provided by design.** | BLOCKED (two open governance items) |
| **—** | **Cap policy permanence** — the measurement-window caps ($25/day, $150/month) vs a permanent policy. | Decide whether the $25/$150 window caps become the permanent serving policy or revert to $1/$1 after measurement. | The window caps are already live for measurement: `data/limits.json` `daily_usd=25.0`, `monthly_usd=150.0` (commit `aaa3814`, "operator decision 2026-07-07"). To make permanent: keep as-is + record. To revert post-measurement: `daily_usd`→1.0, `monthly_usd`→1.0. | PENDING operator signature (window caps live; permanence undecided) |
| **—** | `claim_ledger_export_enabled` **WP-029 sign-off** — the observe-only claim-ledger export (redaction applies). | SIGN OFF (observe-only, best-effort background, WP-029 redaction applies; query text only if capture ON). | Record "claim_ledger_export = WP-029 signed off" in `GATES_STATUS.md`. Flag stays default-OFF (gate #3, observe-only) until recorded. | PENDING operator signature |
| **—** | **§II.6 enforce-honesty condition** — SATISFIED via the STRONG arm (entailment ON), not the recorded-acceptance arm. | RECORD as satisfied. | Already satisfied: step 22 wired the entailment lane (`claim_entailment_llm_enabled` sub-flag; batched ≤8 claims/call; `CLAIM_ENTAILMENT_PROMPT_HASH` pinned) — an AMBIGUOUS-band claim gets an entailment verdict in the ledger. Recorded in the engine step-71 registry. **This is a RECORD, not a flip** — the sub-flag rides `claim_verify` and stays default-OFF/enforce-gated. | SATISFIED (record) |

---

## Notes for the operator

- **Ratify ≠ flip.** Every "ratify" above (Q3 fusion, Q6 k-budget) records an operator
  decision but leaves the flag default-OFF; the actual serving flip is still gated by the
  retrieval A/B on gold (gate #3) and the `GATES_STATUS.md` runway.
- **The two hard-blocked items** ('pokyn' taxonomy + VL-F-12; soft-law FETCH) have **no
  command by design** — they are open governance/lawyer-scope items and stay blocked.
- **Nothing here touches the enforce-flip stack.** The public shadow→enforce flip is governed
  by `GATES_STATUS.md` (steps 89/87/72 + lawyer review #2 + operator sign-off + the honesty
  condition), not by this memo.

---

## Operator decisions recorded 2026-07-08

| # | Decision | Operator call | Recorded effect |
|---|---|---|---|
| **G4-DEFER** | EU-AI-Act (gate #4) — comply now vs defer. | **DEFER to the deployment phase** (operator + lawyer): test the functionality in practice now; close the Act determinations (role under Act · Annex III · Art. 50) + the measured cost envelope BEFORE public/paid serving. | Gate #4 reclassified **DEFERRED** in `GATES_STATUS.md` (record NOT erased). Gate-4-gated functions run ON in the T-LOCAL test posture; `main` stays default-OFF. ⏰ **2 Aug 2026 remains a recorded deployment-phase obligation.** |
| **G5-FLIP** | sme_roles (gate #5) flip. | **FLIP ON.** | Re-proven GREEN (`applicability_eval` `binding_recall=1.0`); enabled in the T-LOCAL test posture (`SME_ROLES_ENABLED=1`). Production env flip operator-authorized. |
| **GO-LIVE** | Final go-live / enforce-flip sign-off. | **GIVEN.** | Enforce-flip checklist row 4 = ✅ GIVEN. The flip EXECUTES only when rows 1–3 land: the n=70 batch (OFF running, ON pending) + gate #2 (≥50-answer review). Metagate stays SHADOW throughout. |

> **Honest boundary (recorded).** The operator go-live sign-off is captured. It authorizes the
> flip; it does not by itself perform it. Two machine/quality conditions still stand between here
> and public/paid serving — **(1)** the n=70 measurement batch (both arms) and **(2)** gate #2's
> review of ≥50 real answers. `main`/committed config remains 100% default-OFF until those land.
> The EU-AI-Act work is deferred, not deleted: it is owed before public/paid deployment.
