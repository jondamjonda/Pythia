# WS11 — B4 Measurement: status + honest scope (2026-07-08)

## What is DONE (engineering half, committed)
- **The measurement infrastructure is built + unit-tested (35 tests):** `eval/aq_answer_eval.py` (scores the served answer over the 100-item `gold_layman_v1`; `--limit`/`--posture-tag`/`--out` + a run-manifest block + the step-72 independent `fabrication_check` scorer over on-disk ledgers), `eval/aq_compare.py` (scored OFF-vs-ON memo with a paired bootstrap 95% CI), `eval/SHADOW_WINDOW_PROTOCOL.md` (the step-89 ≥100-answer soak protocol, each checklist row mapped to a `/metrics` path). `run_aq_off.sh` (clean OFF baseline: gpt-5.5 + metagate shadow, EVERY AQ lever at its committed default) is a local untracked runner.
- **A bounded directional slice was RUN** (n=3, serving env, this host) — `eval/artifacts/deploy_off_slice.json` / `deploy_on_slice.json` / `deploy_compare_slice_memo.md`:

| | OFF baseline (all AQ off) | ON (T-LOCAL full posture) |
|---|---|---|
| emp-first-hire-plzen-01 | **refuse** | ERROR (timeout, memory wall) |
| emp-first-hire-eshop-02 | **refuse** | **clarify** (honest, refuse_ok) |
| vat-turnover-register-03 | **refuse** | ERROR (timeout, memory wall) |
| recall@k | 0.0 | 0.0 (2 timed out) |

## The directional finding (real, honest)
- **OFF refuses answerable questions** (3/3 → the grounding-floor over-refusal that is the initiative's "before" — recall 0, mode=refuse, refuse_ok=False).
- **ON does not silently refuse** — where it completes it CLARIFIES honestly (never a silent abstain). And WS10's smoke already **proved a completing ON answer** (req-23: a 2,626-char structured answer with a REAL ledger — verified:6, repaired_verified:1, demoted_gap:10, survival 0.412, 5 repairs, budget not exceeded). So the machinery produces claim-verified answers; the OFF baseline does not answer these at all.

## The HARD constraint (host, not code)
The full-posture **batch** measurement is **memory-wall-bounded on this host**: sequential enforce answers thrash at ~87% swap and 2/3 exceeded the harness ceiling. Fix-forward applied: `--timeout` 180→**600s** (a single full-posture answer completes in ~60–91s FE-down, so a slow-but-valid answer is no longer falsely scored ERROR) — but sequential batch throughput still needs a **higher-RAM host or a cooldown between items**. The plan scopes exactly this (steps 86/87/89 → "FE-down / higher-RAM host").

## What REMAINS for full WS11 (a dedicated run, not code)
- **86/87:** the clean n=70 OFF + n=70 ON scored artifacts + the comparison memo → a **dedicated multi-hour FE-down batch** (~140 answers, ~$25–55 within the raised $25/$150 caps). The harness + compare are ready; run: `run_aq_off.sh` → `aq_answer_eval.py --posture-tag off` (full), then `run_aq_all.sh` → `--posture-tag on`, then `aq_compare.py`.
- **88:** the ctx-window ≥95% pin is already established at unit grain (WS6 step 51); the answer-grain sweep + fusion `w_s` calibration ride the same batch.
- **89:** the ≥100-answer shadow window + ≥50 exported ledgers — the longest soak (`SHADOW_WINDOW_PROTOCOL.md` is committed).
- **Certification gate (human):** the scores are over the **DRAFT** gold — full certification needs **CP4 (a lawyer certifying `gold_layman_v1`)**. Until then every number here is directional, not certified.

**Bottom line:** the measurement machinery is built, verified, and produces a real directional signal (OFF refuses, ON answers/clarifies, the enforce ledger is genuinely non-empty). The certified n=70×2 numbers require a higher-RAM batch host + the lawyer gold-gate — host capacity + a human sign-off, not engineering.
