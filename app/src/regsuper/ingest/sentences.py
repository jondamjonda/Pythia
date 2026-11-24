"""Segment provisions into sentence rows for sentence-level embeddings.

Walks each provision's Markdown with markdown-it-py and splits paragraphs into sentences,
protecting Czech and EU legal abbreviations. Tables, fenced code and HTML blocks are kept
whole as kind='block' rows so structured data is not shredded. Each sentence is prefixed
with a structural breadcrumb (the provision label), so retrieval hits read
"§ 627 -- Derivátem se ..." rather than as context-free fragments.

Usage:
    python -m regsuper.ingest.sentences                  # all provisions
    python -m regsuper.ingest.sentences --doc 240/2013   # single document
"""

from __future__ import annotations

import argparse
import asyncio
import re
from dataclasses import dataclass

from markdown_it import MarkdownIt
from sqlalchemy import delete, select

from regsuper.observability.events import emit
from regsuper.observability.logging_config import configure_logging
from regsuper.storage.database import session_scope
from regsuper.storage.models import Provision, ProvisionSentence, SourceDocument

# 'gfm-like' enables linkify, which requires the optional 'linkify-it-py' package. Only
# tables and strikethrough are needed from GFM, so linkify is disabled.
_md = MarkdownIt("gfm-like").disable("linkify")


# Czech and EU legal abbreviations whose trailing period must not end a sentence.
# 'Sb' (Sbírka zákonů) is deliberately absent: it ends nearly every law citation and is
# sentence-terminating more often than not (e.g. "...zákona č. 256/2004 Sb. Tento zákon...").
_ABBREVS = sorted(
    {
        "odst",
        "písm",
        "bod",
        "čl",
        "č",
        "tj",
        "tzv",
        "např",
        "příp",
        "popř",
        "tzn",
        "Art",
        "art",
        "r",
        "vyhl",
        "zák",
        "mj",
        "pod",
        "vč",
        "tab",
    },
    key=len,
    reverse=True,
)
_ABBREV_RE = re.compile(r"\b(" + "|".join(_ABBREVS) + r")\.")
# "n. l." (anno domini): two periods with optional internal whitespace.
_NL_RE = re.compile(r"\b(n)\.(\s*)(l)\.")

# Sentence boundary: ".", "!" or "?" followed by whitespace and a likely sentence starter
# (ASCII or Czech uppercase, "§", or "(" for "(1)" paragraph markers).
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z§ÁÉÍÓÚÝČĎĚŇŘŠŤŮŽ(])")

_DOT_SENTINEL = "\u0001"


def _protect(text: str) -> str:
    out = _ABBREV_RE.sub(lambda m: m.group(1) + _DOT_SENTINEL, text)
    out = _NL_RE.sub(
        lambda m: f"{m.group(1)}{_DOT_SENTINEL}{m.group(2)}{m.group(3)}{_DOT_SENTINEL}",
        out,
    )
    return out


def _restore(text: str) -> str:
    return text.replace(_DOT_SENTINEL, ".")


def _split_sentences(text: str) -> list[str]:
    if not text or not text.strip():
        return []
    protected = _protect(text)
    parts = _SENT_RE.split(protected)
    return [s for p in parts if (s := _restore(p).strip())]


@dataclass(frozen=True)
class Sentence:
    text: str
    kind: str = "sentence"  # 'sentence' or 'block'


def _inline_text(inline_tok) -> str:
    """Render an 'inline' token to plain text, dropping emphasis markup."""
    if inline_tok is None:
        return ""
    children = inline_tok.children or []
    if not children:
        return inline_tok.content or ""
    parts: list[str] = []
    for child in children:
        if child.type == "text":
            parts.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parts.append(" ")
        elif child.type == "code_inline":
            parts.append(child.content)
        # strong_open, em_open, link_open and their closing tokens are skipped; their
        # inner 'text' children carry the visible content.
    return "".join(parts)


def _collect_table(tokens, start: int) -> tuple[str, int]:
    """Render the table that opens at index `start`.

    Returns the rendered text and the token index just after the matching table_close.
    """
    rows: list[str] = []
    current: list[str] = []
    j = start + 1
    while j < len(tokens) and tokens[j].type != "table_close":
        t = tokens[j]
        if t.type == "tr_open":
            current = []
        elif t.type in ("th_open", "td_open"):
            inline = tokens[j + 1] if j + 1 < len(tokens) else None
            current.append(_inline_text(inline).strip())
        elif t.type == "tr_close":
            rows.append(" | ".join(current))
        j += 1
    return "\n".join(rows), j + 1


def segment(provision_text: str, *, breadcrumb: str | None = None) -> list[Sentence]:
    """Split a provision's Markdown into Sentence rows."""
    if not provision_text or not provision_text.strip():
        return []

    tokens = _md.parse(provision_text)
    out: list[Sentence] = []
    # Stack entries are (heading_level, label). A new heading at level L pops
    # everything currently at level >= L, mirroring how Markdown sections nest.
    stack: list[tuple[int, str]] = []
    base = (breadcrumb or "").strip()

    def _prefix() -> str:
        parts = [base] + [s[1] for s in stack]
        return " -- ".join(p for p in parts if p)

    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if tok.type == "heading_open":
            inline = tokens[i + 1] if i + 1 < len(tokens) else None
            label = _inline_text(inline).strip()
            level = int(tok.tag[1:]) if tok.tag and tok.tag.startswith("h") else 6
            while stack and stack[-1][0] >= level:
                stack.pop()
            last = stack[-1][1] if stack else base
            if label and label != last:
                stack.append((level, label))
            i += 3  # heading_open, inline, heading_close
            continue

        if tok.type == "heading_close":
            i += 1
            continue

        if tok.type == "paragraph_open":
            inline = tokens[i + 1] if i + 1 < len(tokens) else None
            txt = _inline_text(inline).strip()
            if txt:
                pre = _prefix()
                for s in _split_sentences(txt):
                    out.append(
                        Sentence(
                            text=f"{pre} -- {s}" if pre else s,
                            kind="sentence",
                        )
                    )
            i += 3
            continue

        if tok.type in ("fence", "code_block", "html_block"):
            content = (tok.content or "").rstrip()
            if content:
                pre = _prefix()
                out.append(
                    Sentence(
                        text=f"{pre} -- {content}" if pre else content,
                        kind="block",
                    )
                )
            i += 1
            continue

        if tok.type == "table_open":
            rendered, next_i = _collect_table(tokens, i)
            if rendered.strip():
                pre = _prefix()
                out.append(
                    Sentence(
                        text=f"{pre} -- {rendered}" if pre else rendered,
                        kind="block",
                    )
                )
            i = next_i
            continue

        # List open and close tokens are skipped; the paragraphs inside them are handled
        # when the loop reaches their paragraph_open tokens.
        i += 1

    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
async def segment_all(*, doc_external_id: str | None = None) -> dict[str, int]:
    stats = {"documents": 0, "provisions": 0, "sentences": 0, "blocks": 0, "empty": 0}

    async with session_scope() as session:
        stmt = select(Provision).order_by(Provision.source_document_id, Provision.ordinal)
        if doc_external_id:
            doc = (
                await session.execute(
                    select(SourceDocument).where(SourceDocument.external_id == doc_external_id)
                )
            ).scalar_one_or_none()
            if doc is None:
                raise RuntimeError(f"no source_document with external_id={doc_external_id}")
            stmt = stmt.where(Provision.source_document_id == doc.id)

        provisions = (await session.execute(stmt)).scalars().all()
        seen_docs: set = set()

        for prov in provisions:
            if prov.source_document_id not in seen_docs:
                seen_docs.add(prov.source_document_id)

            await session.execute(
                delete(ProvisionSentence).where(ProvisionSentence.provision_id == prov.id)
            )
            sentences = segment(prov.text, breadcrumb=prov.label or None)
            if not sentences:
                stats["empty"] += 1
            for idx, s in enumerate(sentences):
                session.add(
                    ProvisionSentence(
                        provision_id=prov.id,
                        sentence_index=idx,
                        kind=s.kind,
                        text=s.text,
                    )
                )
            stats["provisions"] += 1
            stats["sentences"] += sum(1 for s in sentences if s.kind == "sentence")
            stats["blocks"] += sum(1 for s in sentences if s.kind == "block")

            if stats["provisions"] % 500 == 0:
                await session.flush()
                await emit(
                    "segment.progress",
                    provisions=stats["provisions"],
                    sentences=stats["sentences"],
                    blocks=stats["blocks"],
                )
        stats["documents"] = len(seen_docs)

    await emit("segment.complete", **stats)
    return stats


def main() -> None:
    configure_logging()
    p = argparse.ArgumentParser(description="Segment provisions into sentence rows.")
    p.add_argument(
        "--doc",
        help="Restrict to one source_document.external_id (e.g. 240/2013).",
    )
    args = p.parse_args()
    stats = asyncio.run(segment_all(doc_external_id=args.doc))
    print(
        f"documents={stats['documents']} "
        f"provisions={stats['provisions']} "
        f"sentences={stats['sentences']} "
        f"blocks={stats['blocks']} "
        f"empty_provisions={stats['empty']}"
    )


if __name__ == "__main__":
    main()
