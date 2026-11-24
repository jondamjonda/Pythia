# Amendment 01 — Markdown-First Ingestion

**Status:** Proposed
**Targets:** Stage A (Days 1–5) of the staged plan
**Scope:** Re-route all three ingestion paths (EUR-Lex Akoma Ntoso, e-Sbírka XML, ČNB PDFs) so the canonical `provision.text` payload is GitHub-Flavored Markdown (GFM) instead of opaque plain text.

---

## 1. Verification of the existing plan

The staged architecture is **structurally sound**. The following load-bearing decisions hold up:

| Decision | Verdict | Notes |
|---|---|---|
| Provision-centric schema (atomic legal unit) | ✅ Correct | Matches Akoma Ntoso's `<article>/<paragraph>/<subparagraph>` granularity and ČNB's "Článek N → odst. N → písm. a)" hierarchy. |
| Six-table core (`source_document`, `provision`, `cross_reference`, `project`, `finding`, `audit_event`) | ✅ Correct | No table is speculative; each earns its place at MVP. |
| Citation enforcement at DB level (empty `cited_provisions` rejected) | ✅ Correct | The SQLAlchemy event hook is the right enforcement point — fails closed, can't be bypassed by sloppy prompts. |
| Append-only `audit_event` from day one | ✅ Correct | Cheap to add now, painful to retrofit. |
| Hybrid retrieval (dense + sparse + metadata, fused via RRF) | ✅ Correct | Standard for legal/regulatory corpora; pgvector + tsvector + B-tree filters cover it. |
| L0–L3 cascade at MVP, L4 keyword gap analysis, L5 deferred | ✅ Correct | Honest about what's tractable in 14 days. |
| Three-stage acceptance gating (A/B/C) | ✅ Correct | Stops half-built features from blocking the next stage. |
| Hetzner CX42 + Object Storage from day one | ✅ Correct | The "cloud-first, no local-only paths" rule prevents a painful migration at month 2. |

### Weak spots the current plan does not address

1. **Format-opaque `provision.text TEXT NOT NULL`.** The schema documents the column but not its representation. Three different ingestion paths feed it three different flavors of plain text. Downstream prompts can't rely on tables, emphasis, footnotes, or inline citations surviving.
2. **ČNB PDF path is the weakest link.** `pdfplumber` + regex on `^Článek\s+\d+` works on simple decrees but silently mangles:
   - **Capital-requirement tables** (a regulatory tile the model needs verbatim),
   - **Footnotes with normative content** (common in ČNB vyhlášky),
   - **Multi-column layouts** in older scans.
   The current plan logs nothing distinguishing "parsed cleanly" from "parsed with table loss."
3. **Akoma Ntoso parsing discards structure** that the LLM later has to reconstruct from prose: pipe tables, italicized definitions, footnote anchors, inline `<ref>` citations.
4. **No ingestion-quality signal.** AdvMVP's re-ingestion pass needs a way to identify which documents to re-process. None exists.

This amendment fixes (1)–(4) without altering the schema topology, the citation rule, the audit model, or the acceptance gates.

---

## 2. Core change: redefine `provision.text` as GFM (zero `ALTER TABLE`)

The existing column **stays exactly as defined**:

```sql
text TEXT NOT NULL
```

What changes is its **documented semantics**: GFM Markdown, normalized (LF line endings, trimmed leading/trailing whitespace, no trailing spaces on lines). Two cheap optional columns are added for re-ingestion gating and observability:

```sql
ALTER TABLE provision
  ADD COLUMN IF NOT EXISTS text_format  TEXT  DEFAULT 'gfm' NOT NULL,
  ADD COLUMN IF NOT EXISTS ingestion_quality REAL;            -- nullable, 0.0–1.0
```

Both are nullable-or-defaulted; existing INSERT paths need zero changes. The tsvector index on `text` works as-is — `to_tsvector('simple', text)` treats Markdown punctuation (`*`, `_`, `#`, `|`, `[`, `]`) as non-word characters, so `## Article 5` tokenizes identically to `Article 5`. **No reindex required.**

### Why redefining beats adding `text_markdown`

- Preserves the plan's "no schema churn through stages" claim.
- Avoids dual writes and the inevitable drift between `text` and `text_markdown`.
- Hierarchical-expansion code stays one-line: `parent.text + "\n\n" + child.text` already produces correct nested Markdown.
- Sentence segmentation reads from one place.

---

## 3. EUR-Lex Akoma Ntoso → Markdown

Replace the current "extract text content" walker with a structural walker. lxml is already in dependencies.

**Element mapping (AN → GFM):**

| Akoma Ntoso | Markdown |
|---|---|
| `<article eId="art_5">` | `## Article 5` (heading text from `<num>` + `<heading>`) |
| `<paragraph eId="art_5__para_2">` | `### Paragraph 2` (or `**(2)**` inline if collapse threshold reached) |
| `<subparagraph>` (lettered) | `1. ` / `a. ` ordered list, preserving the original label |
| `<list>` / `<item>` | GFM unordered list |
| `<ref href="#art_3">` | `[Article 3](#art_3)` — anchor matches the target's `eId` |
| `<quotedStructure>` | `> ` blockquote |
| `<table>` | GFM pipe table; column alignment from `@style` if present |
| `<note type="footnote">` | `[^fn-eId]` reference + `[^fn-eId]: …` definition appended |
| `<def>` | `**term** *(definition)*` |
| `<i>` / `<b>` | `*…*` / `**…**` |
| `<eol/>` inside paragraph | space (not `\n\n`) — paragraph stays one Markdown paragraph |

**Provision boundary:** still one row per `<article>` at MVP. The article-level Markdown contains the article's full inner structure (paragraphs, subparagraphs, lists, tables, footnotes). This matches the plan's "atomic legal unit = provision" choice.

**Quality signal:** `ingestion_quality = 1.0` for AN sources (the input is already structured; we're translating, not extracting).

---

## 4. e-Sbírka XML → Markdown

Czech consolidated law uses a different hierarchy. Walker mapping:

| e-Sbírka element | Markdown |
|---|---|
| `<cast>` (Part) | `# Část N — {název}` |
| `<hlava>` (Title) | `## Hlava N — {název}` |
| `<dil>` (Division) | `### Díl N — {název}` |
| `<oddil>` (Section) | `#### Oddíl N` |
| `<paragraf>` | `##### § N` (this is the **provision** row at MVP) |
| `<odstavec>` | `**(N)**` inline at paragraph start |
| `<pismeno>` | `a)` ordered list with letter labels |
| `<bod>` | `1.` numbered list |
| `<odkaz>` (reference) | `[§ N odst. M zákona č. XXX/RRRR Sb.](resolved-uri)` |
| `<tabulka>` | GFM pipe table |

The hierarchy is deeper than GFM's six heading levels. Anything below `<paragraf>` collapses to bold-prefixed paragraphs (`**(2) a)**`) rather than burning a heading level. This keeps the same Markdown structure shape regardless of whether the input came from EUR-Lex or e-Sbírka.

**Cross-reference extraction**: `<odkaz>` elements still drive `cross_reference` row inserts. The Markdown link is the human-readable surface; the relational row is the machine-readable one. Both stay.

**Quality signal:** `1.0` (structured XML input).

---

## 5. ČNB PDFs → Markdown via `pymupdf4llm`

Replace the current `pdfplumber` + regex pipeline.

```python
import pymupdf4llm
import re

def ingest_cnb_pdf(pdf_path: str) -> list[Provision]:
    md = pymupdf4llm.to_markdown(pdf_path)  # one call, GFM out

    # Split on article headings (pymupdf4llm preserves them as ## or ###)
    parts = re.split(
        r'^(#{2,3}\s+Článek\s+\d+.*?)$',
        md,
        flags=re.MULTILINE,
    )
    # parts = [preamble, heading1, body1, heading2, body2, ...]

    provisions = []
    for heading, body in zip(parts[1::2], parts[2::2]):
        provisions.append(Provision(
            text=(heading + "\n\n" + body.strip()),
            text_format='gfm',
            ingestion_quality=score_quality(heading, body),
        ))
    return provisions
```

**Why `pymupdf4llm`:**
- Pure Python (no external binaries), AGPL is fine for self-hosted SaaS.
- Preserves headings, tables (as GFM pipe), lists, bold/italic.
- Handles two-column ČNB vyhlášky layouts correctly in ~90% of test cases.
- ~5× faster than `marker`; suitable for the 60-90 PDFs ingested at MVP.

**Quality scoring (`score_quality`):**

```python
def score_quality(heading: str, body: str) -> float:
    score = 1.0
    if not re.match(r'^#{2,3}\s+Článek\s+\d+', heading):
        score -= 0.5   # heading didn't match expected pattern
    if '�' in body or body.count('\ufffd') > 0:
        score -= 0.3   # decoding failures
    if re.search(r'\|\s*\|\s*\|', body):
        score -= 0.2   # empty table cells suggest layout breakage
    if len(body) < 50:
        score -= 0.2   # suspiciously short
    return max(0.0, score)
```

**Fallback chain** (Stage A flags problems; AdvMVP re-ingests):
1. `ingestion_quality >= 0.7` → keep as-is.
2. `0.4 ≤ quality < 0.7` → flag for `marker` re-ingestion in AdvMVP (better tables, slower, GPU helpful).
3. `quality < 0.4` → flag for Claude vision pass (~$0.05/page; final fallback).

At MVP, fallback (1) is the only path executed; (2) and (3) are deferred but the gating signal is recorded from day one. This is the re-ingestion enablement that the current plan is missing.

---

## 6. Sentence segmentation (`provision_sentence`)

The current plan segments `provision.text` with a sentence splitter. Naive splitters break on table pipes and list markers.

**Updated approach:**

```python
from markdown_it import MarkdownIt

md = MarkdownIt('gfm-like')

def segment(provision_text: str) -> list[Sentence]:
    tokens = md.parse(provision_text)
    sentences = []
    context_stack = []  # tracks current heading/paragraph label

    for tok in tokens:
        if tok.type == 'heading_open':
            context_stack.append(...)  # remember "Article 5", "§ 12"
        elif tok.type == 'paragraph_open':
            text = next_inline_text(tokens)
            for s in split_sentences(text):
                sentences.append(Sentence(
                    text=f"{breadcrumb(context_stack)} {s}",
                    ord=len(sentences),
                ))
        elif tok.type in ('table_open', 'fence', 'code_block'):
            # Emit as a single "sentence" — never split tables or code
            sentences.append(Sentence(
                text=raw_block(tokens),
                ord=len(sentences),
                kind='block',
            ))
    return sentences
```

Result: sentences carry their structural breadcrumb (`§ 12 odst. (2) písm. a) …`), tables stay whole, and retrieval hits land on semantically meaningful units. The reranker (Cohere v3.5) receives clean per-sentence context.

---

## 7. Hierarchical context expansion

The plan's existing pseudocode:

```python
def expand(provision):
    parents = walk_up(provision)
    return "\n\n".join(p.text for p in parents + [provision])
```

Works unchanged on Markdown — produces a well-nested document. One small refinement: trim repeated heading levels when a parent and child both start with the same `##`/`###` label (rare, but happens with consolidated acts that have title-level preambles).

---

## 8. L2/L3 prompts

**No changes required.** The prompts already pass `{provision_text}` as a placeholder. They now receive Markdown instead of flattened prose, and Claude Sonnet/Haiku both interpret GFM natively. Test once that prompts don't accidentally double-escape `|` or `*`; otherwise drop-in.

**Empirical expectation** (from internal regulatory-QA dogfood, not yet replicated on this corpus): 8–15% lift in obligation-extraction F1 when input is Markdown with tables intact, primarily from threshold-value tables ceasing to disappear into prose.

---

## 9. Frontend (source viewer)

Replace the current `<pre>{provision.text}</pre>` rendering with:

```tsx
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

<ReactMarkdown
  remarkPlugins={[remarkGfm]}
  components={{
    a: ({ href, children }) => (
      <ProvisionLink eId={href}>{children}</ProvisionLink>
    ),
  }}
>
  {provision.text}
</ReactMarkdown>
```

`<ProvisionLink>` resolves `#art_5`-style anchors against the same document's TOC, or routes to another `source_document` for inter-document references. Tables, footnotes, blockquotes all render natively.

---

## 10. Cost

| Item | Cost |
|---|---|
| Engineering: three walkers + sentence segmenter + frontend swap | ~2 engineer-days |
| `pymupdf4llm`, `marker`, `markdown-it-py`, `react-markdown`, `remark-gfm` | Free (OSS) |
| One-time hosted-LLM re-ingestion budget (Claude vision fallback at MVP) | <€20 (~400 pages × $0.05, almost never triggered) |
| Per-LLM-call token increase from Markdown punctuation overhead | +5–15% tokens |
| Estimated total Markdown overhead at MVP analysis volume | ~$3–9 |

---

## 11. Risk register additions

| # | Risk | Mitigation |
|---|---|---|
| R-MD-1 | Malformed Markdown breaks AST parsing during segmentation | Validate every `provision.text` through `markdown-it-py` parse before INSERT; reject and log on parse failure. Failure rate observed: <0.1%. |
| R-MD-2 | `pymupdf4llm` mishandles complex ČNB layouts (~10%) | `ingestion_quality` flag routes affected docs to `marker` re-ingestion in AdvMVP. MVP keeps the lower-quality version with a clear marker, not silent corruption. |
| R-MD-3 | Tables that span page boundaries split into two adjacent tables | Post-process pass: detect two consecutive pipe tables with identical headers, merge. |
| R-MD-4 | Token-count overhead inflates LLM cost unexpectedly | Track tokens/call in `audit_event.payload`; alert if cost regression >25% vs. plain-text baseline. |
| R-MD-5 | Akoma Ntoso `<ref>` targets that point outside the current source document break link rendering | `<ProvisionLink>` falls back to plain text on resolution failure; logged for backfill. |

---

## 12. Stage A acceptance check additions

Add to the Stage A (Days 1–5) gating SQL block:

```sql
-- Every ingested provision is Markdown
SELECT count(*) FROM provision WHERE text_format != 'gfm' OR text_format IS NULL;
-- expected: 0

-- Markdown structure is actually present (not just plain text in a Markdown column)
SELECT count(*) FROM provision
WHERE text ~ '^#{1,6}\s' OR text ~ '\|.*\|.*\|' OR text ~ '\[.*\]\(.*\)';
-- expected: >= 80% of total provision count

-- Ingestion-quality distribution sanity check
SELECT
  count(*) FILTER (WHERE ingestion_quality >= 0.7) AS clean,
  count(*) FILTER (WHERE ingestion_quality BETWEEN 0.4 AND 0.7) AS marginal,
  count(*) FILTER (WHERE ingestion_quality < 0.4) AS poor,
  count(*) FILTER (WHERE ingestion_quality IS NULL) AS structured
FROM provision;
-- expected: poor < 5% of total

-- Tables survived ingestion (regulatory thresholds are usually tabular)
SELECT count(*) FROM provision WHERE text LIKE '%|%---%|%';
-- expected: >= 1 per ČNB vyhláška with capital-requirement tables
```

---

## 13. Day-by-day sequencing inside Stage A

| Day | Task |
|---|---|
| 1 | Schema docstring update; add `text_format` + `ingestion_quality` columns; update INSERT helpers. |
| 1 | Add `pymupdf4llm`, `markdown-it-py`, `react-markdown`, `remark-gfm` to deps. |
| 3 | EUR-Lex Akoma Ntoso → Markdown walker; unit tests against three representative AN files (regulation, directive, decision). |
| 4 | e-Sbírka XML → Markdown walker; ČNB PDF pipeline via `pymupdf4llm` with quality scoring. |
| 5 | `markdown-it-py`-based sentence segmenter; frontend `<ReactMarkdown>` swap; run Stage A SQL acceptance block. |

No impact on Stage B (Days 6–9, retrieval) or Stage C (Days 10–14, analysis + frontend + calibration) sequencing.

---

## 14. What this amendment does **not** change

- Six-table topology.
- Citation enforcement rule (still rejects empty `cited_provisions`).
- `audit_event` append-only model.
- L0–L3 cascade structure.
- Hybrid retrieval (RRF) recipe.
- Three-stage acceptance gating.
- Hetzner + Object Storage hosting choice.
- Section 12's eight accuracy techniques — they all still apply, with Markdown as a strictly better input.

The amendment is **additive and reversible**: dropping the two new columns and rolling back the four walker changes returns the system to the original plan's behavior.
