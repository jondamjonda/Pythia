# Expected T-LOCAL Activation Manifest (WS10 step 81)

The FULLY-RESOLVED flag posture the local error-surfacing runner (`run_aq_all.sh`, untracked by design)
brings up for the T-LOCAL everything-ON smoke. Every in-scope answer-quality (AQ, WS4–WS9) flag from the two
`docs/FLAG_GATES.md` registries (app `regsuper` + engine `pythia`) appears here with its EXPECTED
T-LOCAL ON/OFF value. Step 83 boots the stack, dumps the RESOLVED settings to `manifests/*-env-manifest.json`,
and diffs the booted posture against THIS file — a mismatch (a flag the runner should flip but didn't, or a
locked-OFF exception that leaked ON) fails the diff.

This is a LOCAL test posture, NOT a public-serving authorization. metagate stays SHADOW (rule 5); claim_verify
enforce is authorized for LOCAL runs only. `main`/committed config remains 100% default-OFF (pinned by the
byte-identical guard tests). The serving FLIP is governed by the registries' named gates + G14 + EU-AI-Act and
has NOT happened.

Derived from the registries — do not hand-drift. `tests/observability/test_tlocal_posture.py` fails if this
manifest omits any in-scope AQ flag listed in the app `docs/FLAG_GATES.md` registry (coverage guard).

## ENGINE (`pythia`) — boolean flags

| Flag (env `PYTHIA_<UPPER>`) | T-LOCAL value | Note |
| --- | --- | --- |
| `seed_token_idf_enabled` | **ON** | rank query terms by IDF (top-14 discriminative) |
| `seed_pool_scale_enabled` | **ON** | widen rerank shortlist beyond fixed pool=16 |
| `retrieval_concurrent_legs_enabled` | **ON** | run per-sub-query legs concurrently (order-invariant) |
| `retrieval_quotas_enabled` | **ON** | per-leg quota + leg-scaled effective-k |
| `term_norm_enabled` | **ON** | normalized statutory-term sparse leg (add-never-gate) |
| `softlaw_quota_enabled` | **ON** | cap SOFT_LAW at ceil(k/3), demote-only |
| `intake_v2_enabled` | **ON** | richer intake call (paired mode = active, below) |
| `intake_det_scan_enabled` | **OFF** | NOT in the WS10 set; gate #1 (intake_lexicon.json lawyer-review) pending — deliberately unflipped |
| `intake_clarify_enabled` | **ON** | engine-side stage-2 clarify short-circuit (sub-flag of intake_v2, ON) |
| `ctx_window_enabled` | **ON** | ±250-char fact/term ctx windows + faithfulness pass |
| `ctx_clause_snap_enabled` | **ON** | snap ctx windows to enclosing provision_clause (child of ctx_window, ON) |
| `structured_answer_enabled` | **ON** | 7-slot Czech action plan (populates .practical) |
| `structured_plan_llm_enabled` | **ON** | cheap-LLM slot-placement refine (sub-flag of structured_answer, ON) |
| `draft_planner_enabled` | **ON** | lift sections into a typed DraftPlan (pure) |
| `evidence_bundle_enabled` | **ON** | one canonical full-text carrier for V-CIT/practical/sections |
| `metagate_enabled` | **ON** | classify intent/coverage/premise (mode = SHADOW, below) |
| `metagate_true_cosine_enabled` | **ON** | feed true bge-m3 cosine into the coverage 'strong' arm |
| `claim_verify_enabled` | **ON** | atomize + verify + attach claim ledger (mode = enforce LOCAL, below) |
| `claim_verify_gap_probe_enabled` | **ON** | probe missed_in_corpus vs not_in_corpus for ungrounded cites (sub-flag of claim_verify, ON) |
| `claim_entailment_llm_enabled` | **ON** | cheap-LLM per-claim entailment support inside V-CIT |
| `claim_repair_llm_enabled` | **ON** | targeted per-claim LLM prose repair (re-cite/reframe/demote, re-verified) |
| `stage_latency_enabled` | **ON** | per-stage wall-clock telemetry (observe-only) |
| `quality_tripwires_enabled` | **ON** | pure tripwire evaluator telemetry (never raises/demotes) |

## ENGINE (`pythia`) — mode flags (paired with a master `*_enabled`)

| Mode flag | T-LOCAL value | Master | Note |
| --- | --- | --- | --- |
| `metagate_mode` | **`shadow`** | `metagate_enabled` (ON) | STAYS shadow everywhere (rule 5; AC-METAGATE RED) — classify+log only, never routes |
| `claim_verify_mode` | **`enforce`** | `claim_verify_enabled` (ON) | enforce authorized for LOCAL runs ONLY (R5 recorded breach); serving stays shadow |
| `intake_v2_mode` | **`active`** | `intake_v2_enabled` (ON) | feed intake sub-Qs into the RRF loop (shadow = log-only) |

## ENGINE (`pythia`) — numeric knob (read only on its ON path)

| Knob | T-LOCAL value | Note |
| --- | --- | --- |
| `answer_leg_concurrency` | **`4`** | cap concurrent retrieval legs < db_pool_max_size; read only on retrieval_concurrent_legs ON path |

## APP (`regsuper`) — answer-surface flags

| Flag (env `<UPPER>`) | T-LOCAL value | Note |
| --- | --- | --- |
| `answer_endpoint_enabled` | **ON** | mount POST /api/answer live (runner sets it via run_everything full posture) |
| `answer_practical_block_enabled` | **ON** | extend AC-CITATION-CHECK to the typed practical block |
| `answer_intake_panel_enabled` | **ON** | surface the whitelisted intake block |
| `answer_profile_context_enabled` | **ON** | pass whitelisted profile facts as project_context |
| `answer_clarify_enabled` | **ON** | 200 clarify frame before quota/engine when a required slot missing |
| `answer_budget_precheck_enabled` | **ON** | assert_not_killed + enforce_spend_limit before the sidecar call |
| `claim_ledger_export_enabled` | **ON** | fire-and-forget canonical claim-ledger bundle after verify |
| `pythia_boundary_spend_gate_enabled` | **ON** | gate the /v1/answer boundary call behind killswitch + enforce_spend_limit(openai) |
| `answer_shadow_weak_coverage_hedged` | **OFF** | NOT flipped for T-LOCAL (scoped operator decision, gate #4); the runner does not set it — deliberately unflipped |

## APP (`regsuper`) — authority-resolver seed/map/gazetteer paths (W4 / step 76)

`AUTHORITY_REGISTRY_ENABLED=1` only MOUNTS the resolver; without these paths it builds an EMPTY index and
resolves nothing. The topic map is config-not-DB, so the SERVING resolver needs `AUTHORITY_TOPIC_MAP_PATH` set.
Pointed at the committed DRAFT fixtures (advocate-review pending under G14). These are LOCAL file paths only —
the resolver never fetches and never writes the DB.

| Env var | T-LOCAL value |
| --- | --- |
| `AUTHORITY_REGISTRY_ENABLED` | **1** (mount the resolver) |
| `AUTHORITY_SEED_PATH` | `tests/fixtures/authority/authority_registry.sample.json` (DRAFT) |
| `AUTHORITY_TOPIC_MAP_PATH` | `tests/fixtures/authority/topic_authority.sample.json` (DRAFT, config-not-DB) |
| `AUTHORITY_GAZETTEER_PATH` | `data/geo/municipality_gazetteer.json` (the 6,258-row AQ-05 gazetteer) |

## Exception table — flags that STAY OFF / ABSENT at T-LOCAL

| Flag | T-LOCAL posture | Why locked |
| --- | --- | --- |
| `metagate_mode` | SHADOW (never `enforce`) | rule 5 — AC-METAGATE RED; enforce needs G14 + EU-AI-Act |
| `claim_verify_promote_enabled` | OFF | gate #2 (lawyer sign-off) — FLIPS LAST; appends missed_in_corpus provisions to verify evidence |
| `claim_calc_worked_example_enabled` | OFF | gate #3 (LAWYER-gated) — sanctioned worked-example emission lane |
| `claim_repair_strip_enabled` | OFF | strip-the-unverifiable-half repair — kept off for T-LOCAL (whole-sentence demotion is the safe default) |
| `claim_typing_llm_enabled` | ABSENT from runner | env line REMOVED in step 47b — the runner does not set it (the config field still defaults False) |

## OUT-OF-SCOPE — deliberately UNTOUCHED by WS10 (step-81 scope boundary)

These flags are NOT part of the AQ answer-surface activation. The runner does not flip them; they stay at their
default-OFF and are not tracked by this manifest's coverage guard.

- `clause_read_enabled` (engine — sub-§ rechunk read; rollout deferred to operator)
- `gnn_staleness_check_enabled` (engine — Temporal wave-1 drift warning)
- `graph_changed_monitor_enabled` (engine — Temporal wave-1 graph-changed consumer)
- `version_diff_enabled` (engine — Temporal wave-1 micro/macro diff + amendment_impact)
- the temporal SUBSTRATE flags (TMP-01/02 — snapshot-protected, inert)
- the auto-ingest / real-ingest path (operator + snapshot gated, never a test flip)
- the raw-getenv connector flags (discovery connectors / ingestion — outside the answer surface)

NOTE — `discovery_enabled` (engine `PYTHIA_DISCOVERY_ENABLED=1`) sits in the same Temporal wave-1 block but IS
flipped by the T-LOCAL runner as an in-scope WS9 publication-feed watcher lever (writes discovery_queue only, no
ingestion). It is intentionally IN scope and distinct from the untouched temporal siblings above.
