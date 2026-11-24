# AQ-06 — defined_term 5,566 → 5,263 (−303) investigation

**WS0 step 6 (2026-07-06).** Read-only investigation of the corpus `defined_term` row-count drop the
full-deployment audit flagged ("defined_term dropped another 303 uninvestigated"). Gate: this note must exist
before the first stack relaunch (step 11) blesses a `term_norm`-ON posture (§4.3 hard ordering #10).

## Observed
- Current `pythia.defined_term`: **5,263 rows** at 658 docs.
- Test-pinned baseline (2026-06-29, added with the auto-ingestion feature): **5,566 rows**.
- Delta: **−303 rows**. Unique lexicon-eligible terms now (`term IS NOT NULL AND length(term) >= 3`,
  grouped): **4,091**.

## Root cause — classification
**Most likely: RE-EXTRACTION / stricter enrichment filter** (not a harmful delete).
Evidence:
- No explicit `DELETE`/dedup SQL in engine git history since 2026-06-29 touching `defined_term`.
- Only ~18 true row-level duplicates remain today (same term+scope+provision) — the −303 is **not** an
  un-run dedup backlog.
- `document_version` is empty ⇒ no amendment runs (which snapshot/restore `defined_term`) fired in the window.
- The 2026-06-29 enrichment (Qwen defined-terms) is the plausible actor: a re-extraction pass with a stricter
  acceptance criterion replaces rather than loses coverage.

Residual uncertainty: the exact commit/run that shed the 303 was not pinned (the pre-drop dump's per-table
count wasn't recorded in a manifest). This is acceptable because the decisive question below resolves
independently of the exact cause.

## Decisive check — does `term_norm` depend on the dropped rows?
**No.** `pythia/graph/term_norm.py` builds its `MaterializedLexicon` from TWO sources:
1. `defined_term` via `_load_defined_terms()` — SQL `SELECT term, array_agg(...) FROM defined_term
   WHERE term IS NOT NULL AND length(term) >= 3 GROUP BY term`. It keys on **DISTINCT term** (GROUP BY), so
   the lexicon surface is a function of the set of *unique terms*, **not the row count**. Losing 303 rows
   changes the lexicon only if it removed the *last* row of some term — and the drop is consistent with
   duplicate/near-duplicate re-extraction, not last-copy loss.
2. `data/term_aliases.json` — 57 curated layman→statutory aliases, **DB-independent**. Loads even if
   `defined_term` were empty. `term_norm_augmentation()` wraps all DB reads in try/except → `[], []` on any
   error (fail-soft, never a crash).

## VERDICT
**Lexicon UNAFFECTED — `term_norm` is operationally safe to enable.** The retrieval leg reads unique terms +
a static alias file; the −303 rows do not remove lexicon coverage. The step-11 relaunch may bless a
`term_norm`-ON posture on this basis.

Follow-up (non-blocking, backlog): pin the exact enrichment run that shed the 303 and record per-table counts
in a corpus manifest so future drops are attributable at a glance.
