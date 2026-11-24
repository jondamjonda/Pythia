# LITE serving posture

The smallest AQ flag set that still delivers the goal — a **structured plain-language answer**
whose **citations are grounded/verified** (shadow) — with every heavy retrieval enhancer,
extra-LLM-call, concurrency, temporal, and discovery feature turned **OFF**, tuned for a
**memory-tight Mac** with the gpt-5.5 synthesizer moved **off-box**.

Launcher: [`run_aq_lite.sh`](../../run_aq_lite.sh) (orchestration mirrors `run_aq_local.sh`, but
boots the engine with the flag set below and `PYTHIA_SYNTHESIZER_PROVIDER=openai`).

> **NOT a public/paid serving authorization.** LITE is a deliberately reduced-feature test posture.

---

## Flag table

Tiers: **BASELINE** = memory-floor lever (matters far more than flags) · **CORE** = kept ON in LITE ·
**HEAVY** = dropped in LITE. Memory column is the *incremental* cost of turning it ON (baseline levers
show the resident cost they govern).

| Flag | Tier | Memory | LITE state | Requires (parent) | How to re-enable |
| --- | --- | --- | --- | --- | --- |
| `PYTHIA_SYNTHESIZER_PROVIDER` | BASELINE | reclaims ~9-10 GB (local Ollama 14B) when set to `openai` | **`openai`** | — | keep `openai`; `ollama` re-loads the ~9-10 GB local model on-box |
| *(reranker `bge-reranker-v2-m3`)* | BASELINE | ~2-2.5 GB | **ON (hardcoded)** | — | **no env flag — HUMAN engine-code fix** (engine.py:32) |
| *(bi-encoder `bge-m3`)* | BASELINE | ~1.5-2.5 GB | **ON (unconditional)** | — | not flag-gated; part of the resident floor |
| `PYTHIA_STRUCTURED_ANSWER_ENABLED` | CORE | none (pure-python slot routing; same one gpt-5.5 generate) | **1** | — | already ON |
| `PYTHIA_CLAIM_VERIFY_ENABLED` | CORE | +latency/heap only (0 extra LLM in shadow) | **1** | — | already ON |
| `PYTHIA_CLAIM_VERIFY_MODE` | CORE | none | **shadow** | `PYTHIA_CLAIM_VERIFY_ENABLED` | keep `shadow`; `enforce` runs the fix loop (lawyer-gated, gate #4) |
| `PYTHIA_CTX_WINDOW_ENABLED` | CORE | ~0 (pure-python string windowing) | **1** | (uses keep_full from claim-verify) | already ON |
| `PYTHIA_SOFTLAW_QUOTA_ENABLED` | CORE | ~0 (one bounded read-only DB fetch) | **1** | — | already ON |
| `PYTHIA_STAGE_LATENCY_ENABLED` | CORE | ~0 (clock reads, observe-only) | **1** | — | already ON |
| `PYTHIA_QUALITY_TRIPWIRES_ENABLED` | CORE | ~0 (pure-python eval, observe-only) | **1** | — | already ON |
| `PYTHIA_FUSION_ENABLED` | HEAVY | none on answer path (B3-gated, no-op) | **0** | — | `export PYTHIA_FUSION_ENABLED=1` |
| `PYTHIA_ANSWER_PATH_FUSION_ENABLED` | HEAVY | +1 bge-m3 encode per leg | **0** | — | `export PYTHIA_ANSWER_PATH_FUSION_ENABLED=1` |
| `PYTHIA_SEED_TOKEN_IDF_ENABLED` | HEAVY | model-free; unproven benefit (gate #3) | **0** | — | `export PYTHIA_SEED_TOKEN_IDF_ENABLED=1` |
| `PYTHIA_SEED_POOL_SCALE_ENABLED` | HEAVY | more MPS (cross-encoder up to 48 cand/leg) | **0** | — | `export PYTHIA_SEED_POOL_SCALE_ENABLED=1` |
| `PYTHIA_RETRIEVAL_QUOTAS_ENABLED` | HEAVY | bigger gpt-5.5 GENERATE token load (k_eff up to 24) | **0** | — | `export PYTHIA_RETRIEVAL_QUOTAS_ENABLED=1` |
| `PYTHIA_RETRIEVAL_CONCURRENT_LEGS_ENABLED` | HEAVY | **PRIME OOM** — N simultaneous bge-m3 forward passes | **0** | — | `export PYTHIA_RETRIEVAL_CONCURRENT_LEGS_ENABLED=1` (re-enable LAST) |
| `PYTHIA_ANSWER_LEG_CONCURRENCY` | HEAVY | scales the above N-way spike | *(unset)* | `PYTHIA_RETRIEVAL_CONCURRENT_LEGS_ENABLED` | `export PYTHIA_ANSWER_LEG_CONCURRENCY=4` (parent first) |
| `PYTHIA_CLAIM_VERIFY_GAP_PROBE_ENABLED` | HEAVY | extra DB reads; enables promotion | **0** | `PYTHIA_CLAIM_VERIFY_ENABLED` | `export PYTHIA_CLAIM_VERIFY_GAP_PROBE_ENABLED=1` |
| `PYTHIA_CLAIM_ENTAILMENT_LLM_ENABLED` | HEAVY | +cheap-tier LLM call + worker thread per ambiguous claim | **0** | `PYTHIA_CLAIM_VERIFY_ENABLED` | `export PYTHIA_CLAIM_ENTAILMENT_LLM_ENABLED=1` |
| `PYTHIA_CLAIM_REPAIR_LLM_ENABLED` | HEAVY | heaviest LLM sub-flag (redraft prompts) | **0** | `PYTHIA_CLAIM_VERIFY_ENABLED` + `MODE=enforce` | `export PYTHIA_CLAIM_VERIFY_MODE=enforce; export PYTHIA_CLAIM_REPAIR_LLM_ENABLED=1` |
| `PYTHIA_INTAKE_V2_ENABLED` | HEAVY | +1 cheap-tier LLM call per answer | **0** | — | `export PYTHIA_INTAKE_V2_ENABLED=1` |
| `PYTHIA_INTAKE_V2_MODE` | HEAVY | `active` = heavier retrieval (sub-Q + HyDE legs) | **shadow** | `PYTHIA_INTAKE_V2_ENABLED` | `export PYTHIA_INTAKE_V2_MODE=active` (parent first) |
| `PYTHIA_INTAKE_CLARIFY_ENABLED` | HEAVY | +torch bge encode per weak turn | **0** | `PYTHIA_INTAKE_V2_ENABLED` | `export PYTHIA_INTAKE_CLARIFY_ENABLED=1` (parent first) |
| `PYTHIA_STRUCTURED_PLAN_LLM_ENABLED` | HEAVY | +1 cheap-tier JSON LLM call | **0** | `PYTHIA_STRUCTURED_ANSWER_ENABLED` | `export PYTHIA_STRUCTURED_PLAN_LLM_ENABLED=1` (parent already ON) |
| `PYTHIA_DRAFT_PLANNER_ENABLED` | HEAVY | pure-python; user-invisible | **0** | — | `export PYTHIA_DRAFT_PLANNER_ENABLED=1` |
| `PYTHIA_TERM_NORM_ENABLED` | HEAVY | +sparse DB retrieval leg + RRF; lawyer-review-pending (gate #1) | **0** | — | `export PYTHIA_TERM_NORM_ENABLED=1` |
| `PYTHIA_CTX_CLAUSE_SNAP_ENABLED` | HEAVY | +DB round-trip per widened span | **0** | `PYTHIA_CTX_WINDOW_ENABLED` | `export PYTHIA_CTX_CLAUSE_SNAP_ENABLED=1` (parent already ON) |
| `PYTHIA_EVIDENCE_BUNDLE_ENABLED` | HEAVY | bounded read-only DB fan-out | **0** | — | `export PYTHIA_EVIDENCE_BUNDLE_ENABLED=1` |
| `PYTHIA_TEMPORAL_CONSUMER_ENABLED` | HEAVY | display-only; inert on single-version corpus | **0** | — | `export PYTHIA_TEMPORAL_CONSUMER_ENABLED=1` |
| `PYTHIA_TEMPORAL_PROSE_GUARD_ENABLED` | HEAVY | prompt caveats; inert on IN_FORCE corpus | **0** | — | `export PYTHIA_TEMPORAL_PROSE_GUARD_ENABLED=1` |
| `PYTHIA_DISCOVERY_ENABLED` | HEAVY | background feed watcher; not on answer path | **0** | — | `export PYTHIA_DISCOVERY_ENABLED=1` |
| `PYTHIA_METAGATE_ENABLED` | HEAVY | up to 3 extra cheap-tier LLM calls; ENFORCE over-refuses | **0** | — | `export PYTHIA_METAGATE_ENABLED=1` |
| `PYTHIA_METAGATE_MODE` | HEAVY | `enforce` band-refuses | **shadow** | `PYTHIA_METAGATE_ENABLED` | `export PYTHIA_METAGATE_MODE=enforce` (parent first; lawyer-gated) |
| `PYTHIA_METAGATE_TRUE_COSINE_ENABLED` | HEAVY | +bge encode at answer time | **0** | `PYTHIA_METAGATE_ENABLED` | `export PYTHIA_METAGATE_TRUE_COSINE_ENABLED=1` (parent first) |
| `PYTHIA_CLAUSE_READ_ENABLED` | HEAVY | extra bge-ANN leg (shares singleton, +compute) | **0** | — | `export PYTHIA_CLAUSE_READ_ENABLED=1` |
| `ANSWER_ENDPOINT_ENABLED` | CORE (app) | none | **1** | — | already ON (must be ON to serve at all) |
| `NEXT_PUBLIC_ANSWER_VIEW_ENABLED` | CORE (app) | none | **1** | — | already ON (the `/answer` UI) |
| `COMPLIANCE_GUARD_MODE` | CORE (app) | none | **shadow** | — | keep `shadow`; `enforce` is G14 lawyer-gated |
| `ANSWER_PRACTICAL_BLOCK_ENABLED` | HEAVY (app) | inert today (engine omits `.practical`); G14 copy pending | **0** | `ANSWER_INTAKE_PANEL_ENABLED` | `export ANSWER_INTAKE_PANEL_ENABLED=1; export ANSWER_PRACTICAL_BLOCK_ENABLED=1` |
| `ANSWER_INTAKE_PANEL_ENABLED` | HEAVY (app) | inert unless engine `intake_v2` attaches `audit_trace['intake']` | **0** | (engine `PYTHIA_INTAKE_V2_ENABLED`) | `export ANSWER_INTAKE_PANEL_ENABLED=1` (+ engine intake_v2) |
| `ANSWER_PROFILE_CONTEXT_ENABLED` | HEAVY (app) | +DB read to seed intake | **0** | — | `export ANSWER_PROFILE_CONTEXT_ENABLED=1` |
| `ANSWER_CLARIFY_ENABLED` | HEAVY (app) | one conditional DB read (no LLM) | **0** | — | `export ANSWER_CLARIFY_ENABLED=1` |
| `SME_ROLES_ENABLED` | HEAVY (app) | no-op without `entity_scoping_enabled`; not on answer path | **0** | `entity_scoping_enabled` | `export ENTITY_SCOPING_ENABLED=1; export SME_ROLES_ENABLED=1` |
| `AUTHORITY_REGISTRY_ENABLED` | HEAVY (app) | in-memory index from JSON + 1 DB read/req | **0** | (needs `AUTHORITY_*_PATH` seeds) | `export AUTHORITY_REGISTRY_ENABLED=1` (+ seed paths) |
| `CLAIM_LEDGER_EXPORT_ENABLED` | HEAVY (app) | background disk write of the ledger | **0** | — | `export CLAIM_LEDGER_EXPORT_ENABLED=1` |
| `ANSWER_BUDGET_PRECHECK_ENABLED` | HEAVY (app) | spend cap; **refuses every answer under the $1/day TEST cap** | **0** | — | set realistic caps first, then `export ANSWER_BUDGET_PRECHECK_ENABLED=1` |
| `PYTHIA_BOUNDARY_SPEND_GATE_ENABLED` | HEAVY (app) | same spend-cap hazard, client layer | **0** | — | set realistic caps first, then `export PYTHIA_BOUNDARY_SPEND_GATE_ENABLED=1` |

---

## Dependency graph

Re-enable a **child** only after its **parent** is ON.

```
PYTHIA_RETRIEVAL_CONCURRENT_LEGS_ENABLED ── requires ──> PYTHIA_ANSWER_LEG_CONCURRENCY   (parent → child)

PYTHIA_CLAIM_VERIFY_ENABLED ── requires ──> PYTHIA_CLAIM_VERIFY_MODE
PYTHIA_CLAIM_VERIFY_ENABLED ── requires ──> PYTHIA_CLAIM_VERIFY_GAP_PROBE_ENABLED
PYTHIA_CLAIM_VERIFY_ENABLED ── requires ──> PYTHIA_CLAIM_ENTAILMENT_LLM_ENABLED
PYTHIA_CLAIM_VERIFY_ENABLED ── requires ──> PYTHIA_CLAIM_REPAIR_LLM_ENABLED
PYTHIA_CLAIM_VERIFY_MODE    ── requires ──> PYTHIA_CLAIM_REPAIR_LLM_ENABLED   (needs MODE=enforce)

PYTHIA_INTAKE_V2_ENABLED ── requires ──> PYTHIA_INTAKE_V2_MODE
PYTHIA_INTAKE_V2_ENABLED ── requires ──> PYTHIA_INTAKE_CLARIFY_ENABLED

PYTHIA_STRUCTURED_ANSWER_ENABLED ── requires ──> PYTHIA_STRUCTURED_PLAN_LLM_ENABLED

PYTHIA_CTX_WINDOW_ENABLED ── requires ──> PYTHIA_CTX_CLAUSE_SNAP_ENABLED

PYTHIA_METAGATE_ENABLED ── requires ──> PYTHIA_METAGATE_TRUE_COSINE_ENABLED
PYTHIA_METAGATE_ENABLED ── requires ──> PYTHIA_METAGATE_MODE

ANSWER_INTAKE_PANEL_ENABLED ── requires ──> ANSWER_PRACTICAL_BLOCK_ENABLED   (app-side)
entity_scoping_enabled      ── requires ──> SME_ROLES_ENABLED                (app-side)
```

---

## Gaps

### `__baseline__` — the memory floor is baseline, not the flags

**Problem.** The dominant memory cost is BASELINE, not flag-gated, so no flag posture alone fits a
memory-tight Mac. Three unconditional resident consumers dwarf every flag:

1. the OUT-OF-PROCESS default synthesizer Ollama qwen2.5:14b **~9-10 GB** (config.py:318
   `synthesizer_provider='ollama'`),
2. the cross-encoder reranker bge-reranker-v2-m3 **~2-2.5 GB HARDCODED ON** at engine.py:32
   `self.seed_rerank=True` with **NO flag to disable it**,
3. the bge-m3 bi-encoder **~1.5-2.5 GB** unconditional in `dense_seed` (seed.py:353).

That is a **~13-15 GB everything-resident floor** + torch (~0.5-1 GB) + Postgres/app before any
`PYTHIA_` flag is considered.

**Fix.** Attack the baseline, not the flags.

- **(a) HIGHEST LEVERAGE:** set `PYTHIA_SYNTHESIZER_PROVIDER=openai` (gpt-5.5) to move the ~9-10 GB
  generator off-box entirely — the single biggest win, and the requested posture keeps gpt-5.5
  anyway (so this is not a compromise, it is required). **← done by `run_aq_lite.sh`.**
- **(b) Un-hardcode the reranker** — add a settings flag (e.g. `seed_rerank_enabled`, default `True`
  to preserve behavior) at engine.py:32 (`self.seed_rerank = get_settings().seed_rerank_enabled`)
  so a LITE build can drop ~2-2.5 GB — the bi-encoder cosine seed still returns a valid ranked
  shortlist without the cross-encoder. **← HUMAN ENGINE-CODE FIX required (not done here).**
- **(c) Keep `PYTHIA_RETRIEVAL_CONCURRENT_LEGS_ENABLED=0`** so the remaining bge-m3 encoder's peak
  activation is not multiplied N-way (the OOM trigger). **← done by `run_aq_lite.sh`.**

With OpenAI synthesis + reranker gated off, the engine's own resident floor drops to roughly
bge-m3 (~1.5-2.5 GB) + torch (~0.5-1 GB) ≈ **2-3.5 GB**, which is Mac-feasible.

---

## Memory verdict (verbatim)

> Flipping flags alone will NOT fit the Mac: the memory floor is dominated by three UNCONDITIONAL,
> mostly un-flagged consumers — the local Ollama qwen2.5:14b synthesizer (~9-10 GB), the hardcoded
> bge-reranker-v2-m3 cross-encoder (~2-2.5 GB, engine.py:32, NO flag), and the bge-m3 bi-encoder
> (~1.5-2.5 GB) — i.e. a ~13-15 GB everything-resident floor that no PYTHIA_ flag can touch
> (fusion/clause_read reuse the bge singleton for zero new weights; the GNN is method-gated and
> unused). The LITE posture becomes genuinely feasible ONLY because it moves synthesis to gpt-5.5
> via PYTHIA_SYNTHESIZER_PROVIDER=openai (reclaiming the largest ~9-10 GB) AND requires the one-line
> un-hardcode of the reranker to drop another ~2-2.5 GB — after both, the engine's resident floor
> falls to roughly bge-m3 + torch ≈ 2-3.5 GB, which a memory-tight Mac can hold; keeping
> retrieval_concurrent_legs OFF then prevents the N-way encoder-activation spike that is the most
> likely cause of the ':8099 unreachable mid-answer' OOM. Without the reranker un-hardcode (a code
> change, not just an env flag) even this lite posture stays marginal on a very tight box, since
> bge-m3 + reranker + torch alone is ~4-6 GB before Postgres and the app.
