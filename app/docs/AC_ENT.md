# AC-ENT — Entitlements / Quota / Metering / Billing acceptance gate (C2)

**Status:** gate harness GREEN at app `main` (T-C2c). **Flip rule (CARDINAL):**
`entitlements_enabled` may flip `False → True` (decision **C2-08**) **ONLY after
AC-ENT is green**. A *paid external launch* additionally waits on **G14** (lawyer
sign-off) — but the flag flip itself is engineering-gated on AC-ENT alone.

**Harness:** `tests/acceptance/test_ac_ent.py` (the 9-row matrix + AC-COST-MODEL).
Supporting suites: `tests/entitlements/*` (resolver/guard/depth/scopes),
`tests/billing/*` (emit + cache invalidation), `tests/observability/test_quota.py`
(quota atomicity/concurrency), `tests/api/test_metering.py` (run-row write-through +
roll-up), `tests/api/test_tier_clamps.py` (clamp-as-note + 429-before-spend wiring).

**Run:**
```
.venv/bin/python -m pytest tests/acceptance/test_ac_ent.py \
  tests/entitlements tests/billing tests/observability tests/api -q
```
DB-backed rows use the Docker Postgres named by `DATABASE_URL`; they SKIP cleanly
when no DB is reachable so the DB-free subset stays green in CI.

---

## The no-silent-fail-open spine

Every denial is an **explicit status code with a machine `code`** — never a silent
grant, never a silent drop, never a 500:

| Status | Meaning | `code` |
|---|---|---|
| **402** Payment Required | feature not in tier (upgrade path: Lite→export, Lite→L3/L4 is a NOTE not 402) | `feature.not_in_tier` |
| **403** Forbidden | not entitled (api_access add-on, below min_tier, unresolved tier) | `feature.not_entitled` / `tier.below_minimum` / `entitlement.unresolved` / `entitlement.error` |
| **429** Quota Exceeded | per-org tier quota exhausted, raised **before** spend | `quota.exceeded` |

The 429 is **coupled** to a `billing_event('quota.exceeded')` — the status code and
the billing signal land together (emitted inside `check_and_reserve_quota`).

---

## The 9-row matrix (each a passing test)

| # | Scenario | Expected | Test |
|---|---|---|---|
| 1 | **Lite L2-L4 request** | executes **L2 only** + a `layers_capped` note + **200** — NOT a 403, NOT a crash | `test_row1_*`, `test_tier_clamps::test_wired_lite_L4_request_runs_L2_with_note_not_403` |
| 2 | **audit_export** | Lite → **402** (`feature.not_in_tier`); Business → 200 | `test_row2_*` |
| 3 | **api_access** | Lite/Business → **403** (`feature.not_entitled`); Advisor → 200 | `test_row3_*` |
| 4 | **Over-quota** (Lite 4th analysis/month) | **429** (`quota.exceeded`) **BEFORE any LLM spend** — ZERO api_call rows for that request; on **both** the app analyze path AND the Conduit path (C5 reuses `check_and_reserve_quota`) | `test_row4_*`, `test_tier_clamps::test_over_quota_429_before_spend_zero_apicalls` |
| 5 | **`scopes_for_org`** | ∅ for a non-Conduit (lite/business) org; granted for advisor | `test_row5_*` |
| 6 | **`entitlements_enabled=False`** | **pure pass-through** — guard returns None; no `analysis_run`/`usage_counter`/`billing_event` rows written (byte-identical) | `test_row6_*` (guard + DB) |
| 7 | **Exactly one `billing_event`** | per metered event (`analysis.metered`) and per `quota.exceeded` | `test_row7_*`, `test_row4_*` |
| 8 | **Quota concurrency** | N parallel reserves at limit K admit exactly K, refund the rest, final count = K | `test_row8_*` |
| 9 | **Fail-closed** | a resolver/DB error denies (402/403), never silently grants; a provider error is swallowed (no 500) | `test_row9_guard_fails_closed_on_resolver_error`, `test_row9_provider_error_is_swallowed` |
| + | **AC-COST-MODEL** | an L4 run rolls a **non-zero** `cost_usd = SUM(api_call.cost_usd WHERE analysis_run_id=run)` | `test_ac_cost_model_*`, `test_metering::test_l4_run_rolls_non_zero_cost` |

---

## Call order (the metered analyze path — C2c ENT-5 / MET-1 / M6)

`api/projects.py::project_analyze` (and the future `api/answer.py`) follow this exact
order when `entitlements_enabled=True` (all INERT when OFF → byte-identical):

1. `metering.resolve_for_entry(org_id)` — resolve the tier fail-closed (`None` when OFF).
2. `metering.clamp_layers(requested, ent)` — CLAMP-as-NOTE depth gate (Lite L4 → L2 + note).
3. `metering.open_analysis_run(...)` — INSERT the `analysis_run` row (status='running', org_id NOT NULL, tier + answer_mode snapshot).
4. `metering.reserve_for_run(...)` → `check_and_reserve_quota` — **429-BEFORE-SPEND** (raises before the cascade starts → zero api_call rows on 429).
5. run the cascade (`run_l2/l3/l4`, each tagging its api_call rows with the threaded `analysis_run_id`).
6a. on success → `finish_analysis_run(status='done')` rolls `cost_usd = SUM(...)`; `record_run_usage` rolls spend onto the counter.
6b. on refusal/failure → `refund_for_run` (a refusal must not consume quota) + `finish_analysis_run(status='refused'|'failed')`.

## Metric convention (C2c / M6 / Conduit MUST use these exact strings)

| metric | bucket | meaning |
|---|---|---|
| `analyses` | month | every analysis (the `analyses_per_month` allowance) |
| `analyses_L4` | month | an analysis that actually ran L4 |
| `queries` | day | the `queries_per_day` allowance |
| `api_calls` | day | telemetry |

`-1` = unlimited (the reserve increments for reporting but never 429s). `org_id` is
**NEVER NULL** on any `analysis_run` / `usage_counter` / `billing_event` write (the
b3-06a org_id bug class) — the real `Principal.org_id` is threaded in.

---

## Cardinal flip (C2-08) — DEFERRED

Only after this gate is green may `entitlements_enabled` flip. Until then `main` is
byte-identical at rest: every C2 path is a pass-through no-op at the default flag.
The flip is reversible (`entitlements_enabled=False` restores the byte-identical
baseline). A *paid* launch additionally awaits G14 (human) — not the flip.
