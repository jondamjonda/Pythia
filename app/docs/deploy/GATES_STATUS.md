# GATES_STATUS.md — the single T-PUBLIC runway (WS13 step 103)

> ## ⛔ NOTHING USER-VISIBLE FLIPS UNTIL EVERY ROW BELOW IS GREEN.
> **This file is the RUNWAY, not a switch.** It maps every human / machine / operator gate to
> its packet and its current booking/status. It flips no flag and signs no gate. The serving
> flip is executed only when a row's gate has cleared — and the public shadow→enforce flip
> requires the ENTIRE stacked checklist (bottom of this file) to be green. `main` / committed
> config remains **100% default-OFF** (pinned by the byte-identical guard tests) until then.
>
> **Provenance.** Assembled 2026-07-08 for WS13 step 103 from the flip-gate registries (both
> repos), `DEPLOYMENT_PLAN.md` (WS13 + steps 22/72/87/89), the WS11 measurement status, and
> the WS10 smoke evidence. Statuses are honest: **READY** = packet assembled, gate can be
> booked/signed the day its human sits down; **PENDING-on-X** = an input artifact is not yet
> produced (X names the blocker); **BLOCKED** = an upstream human/governance decision is owed.
>
> **UPDATE 2026-07-08:** gates **#1, #3, #5 are LAWYER-SIGNED** (correctness certified); **gate #4
> (EU-AI-Act) is DEFERRED by operator+lawyer decision to the DEPLOYMENT phase** — test the
> functionality now; the Act determinations (role under Act · Annex III · Art. 50) + cost envelope
> are closed BEFORE public/paid serving. ⏰ **2 Aug 2026 remains a deployment-phase obligation, not
> a test blocker.** **Gate #5 re-proven GREEN** (`binding_recall=1.0`) and enabled in the T-LOCAL
> test posture; production env flip operator-authorized. **Operator go-live sign-off GIVEN**
> (enforce-flip checklist row 4). The remaining path to the PUBLIC flip is now: **(a)** the n=70
> OFF/ON measurement batch + ≥100-answer shadow window (steps 86/87/89 — host-bounded; OFF arm
> running, ON arm not yet run); **(b)** gate #2 (combined output review of ≥50 rendered answers —
> only reviewable after (a)). `main`/committed config stays 100% default-OFF regardless.

Cross-links:
- Step-71 registries: `app/docs/FLAG_GATES.md` (APP) · `engine/docs/FLAG_GATES.md` (ENGINE)
- Amendments record: `docs/deploy/deployment-amendments.md`
- Operator Q-list: `docs/deploy/operator-decisions.md`
- WS10 T-LOCAL smoke evidence: `docs/deploy/smoke-2026-07.md`
- WS11 measurement status: `engine/eval/WS11_MEASUREMENT_STATUS.md`

---

## 1. The gate → packet → status map

| Gate | What it unblocks | Packet (audit folder) | Booking / status |
|---|---|---|---|
| **#1 Lawyer label session** (human) | AQ-03 gold certification · A1.5/`term_norm` serving flip · VL-A-04 (`intake_det_scan`) content · fact-row authoritative use · VL-G-09 typed-claim scoring authority | `gate1-labels/` | **✅ LAWYER-SIGNED 2026-07-08** — the five DRAFT label sets certified correct (gold_layman_v1 · claim_gold_v1 · term_aliases+intake_lexicon · 150-row sample · enums/catalog). **AQ-03 gold now CERTIFIED = CP4** (WS11 numbers become certified once the n=70 batch runs). Owed separately: the AC-METAGATE independent eval (step 95; metagate stays SHADOW). |
| **#2 Combined output review** (human, ≥50 answers) | A2 user-visible flip · structured serving flip · **VL-B-09 promotion (flips LAST)** · loop shadow→enforce (public) | `gate2-output-review/` | **PENDING-on: the n=70 measurement batch + the ≥50-answer shadow window** (steps 87 memo + 89 ledgers — **not yet run**, host-capacity-bounded). Interim proof in packet: WS10 smoke + WS11 status. **Book AFTER #1.** |
| **#3 Advocate copy review** (human) | Czech copy certification for the clarify/gap/slot/panel/worked-example/V-SAFE/sme_roles surfaces | `gate3-advocate-copy/` | **✅ ADVOCATE-SIGNED 2026-07-08** — all ~55 Czech user-facing strings (8 categories) certified honest / non-advisory / correct. |
| **#4 EU-AI-Act counsel package** (human, deadline **2 Aug 2026**) | `answer_profile_context_enabled` · `structured_answer_enabled` · user-visible loop enforce · the metagate/enforce classification | `gate4-eu-ai-act/` | **◑ DEFERRED by operator 2026-07-08 (to the deployment phase)** — operator+lawyer decision: test the functionality first; the EU-AI-Act determinations (role under Act · Annex III high-risk · Art. 50) + the measured cost envelope (step 90) are handled at DEPLOYMENT, **before public/paid serving**. Gate-4-gated functions run ON in the T-LOCAL test posture now; `main` stays default-OFF. ⏰ **2 Aug 2026 deadline is NOT erased — it is a recorded deployment-phase obligation.** NOT a test blocker. |
| **#5 B2 lawyer vocab review** (human) + operator flip | `sme_roles_enabled` (demote-only applicability lane) | `gate5-b2/` | **✅ LAWYER-SIGNED + RE-PROVEN GREEN 2026-07-08** — `applicability_eval`: `binding_recall=1.0` / `oos_precision=1.0` / `transposition_safe` / `ac_pass` (all configs). **Enabled in the T-LOCAL test posture** (`SME_ROLES_ENABLED=1`); production env flip operator-authorized. |
| **Operator Q-list** (operator) | Q2 fetch · Q3 fusion ratify · Q4 rechunk · Q5 booking · Q6 k-budget · cap permanence · export sign-off · honesty record | (memo) `docs/deploy/operator-decisions.md` | **READY** — one memo, one line per decision, commands pre-written. **Awaiting one signature pass.** Q4/Q6-calibration are themselves PENDING-on the n=70 batch. |
| **Machine gate: VL-G-11 protocol** (89) | the shadow→enforce flip-gate protocol green | (protocol) `engine/eval/SHADOW_WINDOW_PROTOCOL.md` | **PENDING-on: the ≥100-answer shadow window + ≥50 exported ledgers (step 89 — not yet run).** Protocol committed; harness ready. |
| **Machine gate: AC-METAGATE** (95) | any future metagate `enforce` (out of scope for T-PUBLIC answer flip) | (re-baseline artifact) | **RED / PENDING-on: the consolidated C14e re-baseline (step 95, after the full distribution-shifting batch).** metagate stays **SHADOW** regardless. |

---

## 2. The stacked enforce-flip checklist (public shadow→enforce)

**The public loop shadow→enforce flip requires ALL of these green — simultaneously.** This is
the last and highest gate; it is NOT reachable until §1 gates #1, #2, #3, #4 are cleared.

| # | Condition | Source / evidence | Current state |
|---|---|---|---|
| 1 | **VL-G-11 flip-gate protocol green** | `eval/SHADOW_WINDOW_PROTOCOL.md` (step 89) — ≥100-answer soak, each checklist row mapped to a `/metrics` path | ⏳ **PENDING** — the ≥100-answer shadow window has **not been run** (host-capacity-bounded; needs a higher-RAM / FE-down batch). Protocol + harness committed. |
| 2 | **VL-G-09 fabrication-per-type = 0** | the independent `fabrication_check` scorer (step 72) over the on-disk ledgers, on the n=70 ON batch (step 87) + `claim_gold_v1.jsonl` | ⏳ **PENDING (input half-cleared)** — the scoring authority `claim_gold_v1` is now **lawyer-CERTIFIED (gate #1, 2026-07-08)** and the scorer is built + unit-tested; still needs the n=70 ON scored batch (step 87) to produce the certified fabrication-per-type numbers. **No number is claimed yet.** |
| 3 | **Lawyer review #2 (combined output review)** | gate #2 packet `gate2-output-review/` (≥50 sha256 ledgers + rendered answers + OFF-vs-ON memo) | ⏳ **PENDING** — gate #1 is now SIGNED (2026-07-08); gate #2 is a SEPARATE review of ≥50 *rendered answers* that only becomes reviewable once the n=70 batch + ≥50-answer window produce them. |
| 4 | **Operator sign-off** | `docs/deploy/operator-decisions.md` (the Q-list) + the explicit enforce-flip authorization | ✅ **GIVEN 2026-07-08** — operator go-live sign-off recorded; the flip still EXECUTES only when rows 1–3 (the n=70 batch + gate #2) are green. |
| 5 | **§II.6 enforce-honesty condition** | step 22 (VL-D-05 entailment lane wired; STRONG arm) — recorded in the engine step-71 registry | ✅ **SATISFIED** — the entailment lane is wired (`claim_entailment_llm_enabled`, batched, prompt-hash pinned); satisfies the condition via the strong arm. Recorded. (Sub-flag rides `claim_verify`, stays default-OFF/enforce-gated.) |

**Stacked verdict (updated 2026-07-08): 3 of 5 conditions PENDING, 2 SATISFIED (row 4 operator
sign-off GIVEN + row 5 honesty condition).** Rows 1–3 are all blocked on the same missing input —
the **n=70 OFF/ON measurement batch + the ≥50-answer / ≥100-answer shadow window** (steps
86/87/89), which is host-capacity-bounded and needs a dedicated higher-RAM / FE-down run; row 3
additionally needs the gate #2 human review of those answers. **The public enforce flip becomes
reachable the moment the batch lands + gate #2 clears** — the operator authorization is already
recorded.

---

## 3. What is READY-to-book vs PENDING-on-what (the honest summary)

**✅ LAWYER-SIGNED 2026-07-08 (correctness certified):**
- **Gate #1** (lawyer label session) — all five DRAFT label sets certified; **AQ-03 gold now
  CERTIFIED = CP4**. (AC-METAGATE independent eval, step 95, still owed separately.)
- **Gate #3** (advocate copy review) — all ~55 Czech strings certified honest / non-advisory.
- **Gate #5** (B2 lawyer vocab review) — vocab + fact→role mapping certified; **operator flip
  now enabled** (re-prove `binding_recall=1.0` → `SME_ROLES_ENABLED=1`).
- **Gate #4** (EU-AI-Act counsel) — **DEFERRED by operator to the deployment phase** (test now,
  comply before public/paid serving). Functions enabled in the T-LOCAL test posture; the Act
  determinations + cost envelope + the ⏰ 2 Aug 2026 deadline are recorded as deployment-phase
  obligations, NOT test blockers.

**Still open (operator):**
- **Operator Q-list** — ready for one signature pass (Q4 rechunk / Q6 calibration are themselves
  PENDING-on the batch).

**PENDING-on the n=70 measurement batch + the shadow window (steps 86/87/89 — not yet run,
host-capacity-bounded):**
- **Gate #2** (combined output review) — needs the ≥50 sha256 ledgers + OFF-vs-ON memo.
- **Enforce-flip checklist rows 1, 2, 3** — VL-G-11 protocol, VL-G-09 fabrication=0, lawyer
  review #2 all wait on this batch.
- **Gate #4's** cost/latency envelope (step 90); **Q4/Q6** calibration.
- **Step-88** HyDE 0.5-vs-0.8 sweep verdict (see amendments row 2).

**PENDING-on other human/governance decisions:**
- **AC-METAGATE** re-baseline (step 95) — metagate stays SHADOW regardless.
- **'pokyn' taxonomy governance + VL-F-12 per-connector lawyer scope** — soft-law FETCH stays
  BLOCKED until both.
- **Operator sign-off** (enforce-flip row 4) and **cap permanence** — operator acts.

**The single critical-path unblocker for the public flip is the dedicated n=70 OFF/ON batch +
the ≥100-answer shadow window on a higher-RAM / FE-down host.** Everything else is either ready
to book or a downstream human signature. The measurement machinery is built + unit-tested; only
the run (host capacity) + the human sign-offs remain.
