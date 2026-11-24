# Corpus recovery & rebuild

The Pythia legal corpus lives in the Postgres database `pythia` (not in git — the engine `.gitignore`
excludes `data/`). This file is the map for getting that database back if it is ever lost.

## 1. PRIMARY recovery path — restore the latest dump (fast, faithful)

The supported recovery is **restoring the most recent verified `pg_dump`**, *not* rebuilding from
scratch. The dump is the only artifact that faithfully preserves the **Qwen-extracted `defined_term`
rows**, which are produced by a local LLM (`qwen2.5:14b`) and are **non-deterministic** — re-running
enrichment yields a *different* set, so they cannot be regenerated to parity.

```bash
# into a throwaway DB first, then verify before trusting it
createdb pythia_restore
pg_restore -d pythia_restore --no-owner /path/to/pythia_<DATE>_FINAL_post-enrich.dump
# then re-run the integrity gates against it: d1_check / n5_check / n6_check
```

Latest authoritative dumps (keep at least one copy **off this disk**):
- `snapshots/pythia_<DATE>_FINAL_post-enrich.dump` — the engine corpus (pin it to the engine commit it
  was taken at; see the dump's sibling `.sha256`).
- `snapshots/regsuper_<DATE>.dump` — the app database.

## 2. SECONDARY path — rebuild from source (slow, non-hermetic, statute layer only)

`scripts/rebuild_corpus.py` re-ingests the corpus from `corpus/corpus_manifest.json` (the committed source-of-truth
list of every ingestable act — 274 rows: esbirka + eurlex + cnb_vestnik + regional). It needs **live
network** to e-Sbírka and EUR-Lex/CELLAR, so it is **not** reproducible offline, and it does **not**
restore the non-deterministic definition layer (run `enrich_definitions --all-missing` separately if
you want it back — it will differ from the original).

```bash
python scripts/build_manifest.py        # regenerate corpus_manifest.json from the live DB (optional refresh)
python scripts/rebuild_corpus.py        # ingest (esbirka+eurlex) → normalise → reindex → d1/n5/n6 gate
```

### The load-bearing order (do NOT reorder)
`scripts/rebuild_corpus.py` encodes it; it is proven by the 2026-06-21 `reindex.log`:

```
ingest acts
  → classify_taxonomy --apply      (re-tier constitutional / decree acts)
  → provision_versioning --apply   (MUST run before the first _reindex — N5 fails otherwise)
  → cascade_provision_tier --apply (propagate tier to provision grain)
  → _reindex()  PASS 1             (expected n5_ok=false — this is NORMAL, not an error)
  → _reindex()  PASS 2             (flips the gate green)
  → d1_check / n5_check / n6_check (hard gate; non-zero exit on failure)
```

The first `_reindex()` alone fails N5 until `provision_versioning` has seeded version rows — that is
why the chain runs the four `--apply` steps first and `_reindex()` **twice**. `scripts/run_reindex.py` runs
only the final `_reindex()`; the full chain is in `scripts/rebuild_corpus.py`.

## 3. Connectors not in the rebuild driver
`scripts/rebuild_corpus.py` covers the **esbirka (CZ Sb.)** + **eurlex (EU)** layers (207 of 274 acts). The
remaining source layers have their own connectors and are not re-run by the driver:
- **ČNB soft-law** (18 acts) → `pythia/ingest/cnb_vestnik.py`
- **regional / municipal předpis** (49 acts) → the ÚSC connector
- **case law** (200 rulings) → `pythia/ingest/justice_caselaw.py` + `caselaw.py` (MoJ open data)

For these, the dump (path 1) is the only fast recovery.

## 4. External dependencies (the rebuild is NOT hermetic)
- e-Sbírka open data (CZ statutes) · EUR-Lex / CELLAR FORMEX-4 (EU acts)
- a running Ollama with `qwen2.5:14b` (only for the optional, non-deterministic definition layer)
- a Postgres `pythia` database (the driver refuses to run unless `DATABASE_URL` targets `pythia`)
