r"""Ingest methodological PDFs published by the Czech National Bank (ČNB).

ČNB publishes regulatory positions, interpretive notes and official communications (úřední
sdělení) only as PDFs, with no machine-readable index, so the relevant files are selected by
hand and placed in ``data/cnb_pdfs/``. Each PDF is converted to GFM Markdown with
`pymupdf4llm.to_markdown` (see amendments/01-markdown-ingestion.md) and split into provisions
as described in `parse_pdf`; quality scores flag documents whose extraction broke, so they can
be re-ingested with a stronger extractor.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pymupdf4llm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from regsuper.config import get_settings
from regsuper.ingest.storage import save_raw
from regsuper.observability.events import emit
from regsuper.observability.logging_config import get_logger
from regsuper.observability.resource_guard import gate_storage
from regsuper.storage.audit import log_event
from regsuper.storage.database import session_scope
from regsuper.storage.models import CrossReference, Provision, SourceDocument

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Drafts (same shapes as in fmx4 and esbirka)
# ---------------------------------------------------------------------------
@dataclass
class ProvisionDraft:
    eid: str
    label: str
    hierarchy_path: str
    ordinal: int
    text: str
    metadata: dict = field(default_factory=dict)


@dataclass
class CrossRefDraft:
    from_eid: str
    to_external_ref: str
    ref_type: str  # 'internal' | 'external'
    detected_by: str
    confidence: float


# ---------------------------------------------------------------------------
# Markdown post-processing
# ---------------------------------------------------------------------------
_CLANEK_HEADING_RE = re.compile(
    r"^(#{1,6})\s+Článek\s+(\d+[a-z]?)(?:\s+(.*))?$",
    flags=re.MULTILINE | re.IGNORECASE,
)
_FALLBACK_HEADING_RE = re.compile(
    r"^(#{1,6})\s+(.{3,120})$",
    flags=re.MULTILINE,
)
_EXT_ACT_RE = re.compile(
    r"(?:zákon|vyhlášk|nařízen)\w*\s+č\.\s*\d+/\d+\s*Sb\.",
    flags=re.IGNORECASE,
)
_INT_CLANEK_RE = re.compile(
    r"článk[uy]\s+(\d+[a-z]?)(?:\s+odst\.\s*(\d+))?",
    flags=re.IGNORECASE,
)


def _normalize_markdown(md: str) -> str:
    """Normalise to LF endings, collapse blank lines and trim trailing whitespace."""
    md = md.replace("\r\n", "\n").replace("\r", "\n")
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"


def split_into_provisions(md: str) -> list[tuple[str, str, str]]:
    """Split `md` on ``## Článek N`` boundaries.

    Returns a list of ``(eid, label, body_markdown)`` triples in document order. The
    ``body_markdown`` includes the heading line itself, so each provision is self-contained.
    """
    matches = list(_CLANEK_HEADING_RE.finditer(md))
    if not matches:
        return []
    provisions: list[tuple[str, str, str]] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        chunk = md[start:end].rstrip()
        num = m.group(2).lower()
        title = (m.group(3) or "").strip()
        label = f"Článek {num}" + (f" — {title}" if title else "")
        eid = f"clanek_{num}"
        provisions.append((eid, label, chunk))
    return provisions


def split_fallback(md: str) -> list[tuple[str, str, str]]:
    """Split on any H1 or H2 heading.

    Used for ČNB documents that do not follow the standard "Článek N" template (some
    interpretive notes use "Bod 1" or unnumbered section titles).
    """
    matches = [
        m
        for m in _FALLBACK_HEADING_RE.finditer(md)
        if len(m.group(1)) <= 2  # H1 or H2 only
    ]
    if not matches:
        return []
    provisions: list[tuple[str, str, str]] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        chunk = md[start:end].rstrip()
        title = m.group(2).strip()
        eid = "section_" + re.sub(r"[^a-z0-9]+", "_", title.lower())[:40].strip("_")
        if not eid or eid == "section_":
            eid = f"section_{i + 1}"
        label = title[:200]
        provisions.append((eid, label, chunk))
    return provisions


# ---------------------------------------------------------------------------
# Quality scoring, with an extra deduction for short whole-document fallbacks
# ---------------------------------------------------------------------------
def score_quality(heading: str, body: str) -> float:
    score = 1.0
    if not _CLANEK_HEADING_RE.match(heading or ""):
        score -= 0.5
    if "\ufffd" in body:
        score -= 0.3
    if re.search(r"\|\s*\|\s*\|", body):
        score -= 0.2
    if len(body) < 50:
        score -= 0.2
    return max(0.0, score)


def score_quality_doc(md: str) -> float:
    """Score a document that has no Článek headings."""
    score = 0.55  # baseline penalty for the lost structural granularity
    if "\ufffd" in md:
        score -= 0.3
    if re.search(r"\|\s*\|\s*\|", md):
        score -= 0.1
    if len(md) < 200:
        score -= 0.2
    return max(0.0, score)


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------
def parse_pdf(
    pdf_bytes: bytes, pdf_path: Path
) -> tuple[list[ProvisionDraft], list[CrossRefDraft], str, dict]:
    """Convert a ČNB PDF into provision and cross-reference drafts.

    Markdown is extracted from `pdf_path` with `pymupdf4llm`; `pdf_bytes` is used only for
    statistics. Documents with at least one `Článek N` heading get one provision per article
    (heading plus body). Otherwise the text is split on H1 or H2 headings, and as a last resort
    the whole document becomes one provision with a reduced quality score, which flags it for
    manual review while keeping the corpus complete.

    Returns ``(provisions, xrefs, markdown_string, stats)`` so the caller can store the rendered
    Markdown next to the raw artefact.
    """
    raw_md = pymupdf4llm.to_markdown(str(pdf_path), show_progress=False)
    md = _normalize_markdown(raw_md)

    structured = split_into_provisions(md)
    mode = "clanek"
    if not structured:
        structured = split_fallback(md)
        mode = "fallback-h2" if structured else "single"
    if not structured:
        # Whole-document fallback: keep the content but penalise quality.
        structured = [("doc_full", pdf_path.stem[:200], md.strip())]

    provisions: list[ProvisionDraft] = []
    xrefs: list[CrossRefDraft] = []
    for ordinal, (eid, label, body) in enumerate(structured):
        # First line is normally the heading; everything after is body.
        first_newline = body.find("\n")
        heading = body[:first_newline] if first_newline > 0 else body
        rest = body[first_newline + 1 :] if first_newline > 0 else ""
        quality = score_quality(heading, rest) if mode == "clanek" else score_quality_doc(body)
        provisions.append(
            ProvisionDraft(
                eid=eid,
                label=label,
                hierarchy_path=eid,  # flat; ČNB documents are not deeply nested
                ordinal=ordinal,
                text=body,
                metadata={
                    "split_mode": mode,
                    "ingestion_quality_raw": quality,
                },
            )
        )
        _mine_xrefs(body, eid, xrefs)

    stats = {
        "n_provisions": len(provisions),
        "split_mode": mode,
        "markdown_size_bytes": len(md.encode("utf-8")),
        "pdf_size_bytes": len(pdf_bytes),
        "n_clanek_headings": len(list(_CLANEK_HEADING_RE.finditer(md))),
    }
    return provisions, xrefs, md, stats


def _mine_xrefs(text: str, from_eid: str, sink: list[CrossRefDraft]) -> None:
    seen: set[str] = set()
    for m in _INT_CLANEK_RE.finditer(text):
        clanek = m.group(1)
        odst = m.group(2)
        ref = f"clanek_{clanek}"
        if odst:
            ref = f"{ref}.odst_{odst}"
        if ref in seen or ref == from_eid:
            continue
        seen.add(ref)
        sink.append(
            CrossRefDraft(
                from_eid=from_eid,
                to_external_ref=ref,
                ref_type="internal",
                detected_by="regex",
                confidence=0.55,
            )
        )
    for m in _EXT_ACT_RE.finditer(text):
        ref = m.group(0)
        key = f"ext:{ref}"
        if key in seen:
            continue
        seen.add(key)
        sink.append(
            CrossRefDraft(
                from_eid=from_eid,
                to_external_ref=ref,
                ref_type="external",
                detected_by="regex",
                confidence=0.55,
            )
        )


# ---------------------------------------------------------------------------
# DB insertion
# ---------------------------------------------------------------------------
async def ingest_pdf(pdf_path: Path, *, session: AsyncSession | None = None) -> dict:
    """Ingest one ČNB PDF end to end."""
    own_session = session is None
    ctx = session_scope() if own_session else _passthrough(session)
    async with ctx as sess:
        return await _ingest_pdf_inner(pdf_path, sess)


class _passthrough:
    def __init__(self, sess: AsyncSession):
        self._sess = sess

    async def __aenter__(self) -> AsyncSession:
        return self._sess

    async def __aexit__(self, *_exc) -> None:
        return None


async def _ingest_pdf_inner(pdf_path: Path, session: AsyncSession) -> dict:
    external_id = pdf_path.stem  # filename without extension
    title = _pretty_title(pdf_path.stem)

    await emit("ingest.document.started", publisher="ČNB", external_id=external_id, name=title)

    pdf_bytes = pdf_path.read_bytes()
    # Refuse before the raw write if it would push free disk below the floor. Both the PDF and,
    # below, the rendered Markdown are written; gating on the PDF size (the Markdown is far
    # smaller) lets the check happen once, up front.
    await gate_storage(required_bytes=len(pdf_bytes))
    artefact = save_raw("cnb", external_id, "pdf", pdf_bytes)
    provs, xrefs, md_text, stats = parse_pdf(pdf_bytes, pdf_path)
    if not provs:
        raise RuntimeError(f"parser produced 0 provisions for {pdf_path.name}")

    # Store the rendered Markdown as well, so a later re-ingestion with a different extractor
    # can be diffed against the `pymupdf4llm` baseline.
    save_raw("cnb", external_id, "md", md_text.encode("utf-8"))

    await _delete_existing(session, external_id)

    sd = SourceDocument(
        external_id=external_id,
        title=title,
        publisher="ČNB",
        jurisdiction="CZ",
        legal_hierarchy="cz-cnb-guideline",
        document_type="methodology",
        language="cs",
        raw_storage_uri=artefact.uri,
        raw_format="pdf",
        raw_checksum=artefact.sha256,
        fetched_at=datetime.now(UTC),
        last_checked_at=datetime.now(UTC),
        status="ingested",
        metadata_={
            "filename": pdf_path.name,
            **stats,
        },
    )
    session.add(sd)
    await session.flush()

    eid_to_id: dict[str, UUID] = {}
    rows: list[Provision] = []
    for p in provs:
        row_id = uuid4()
        eid_to_id[p.eid] = row_id
        rows.append(_provision_row(p, sd, row_id))
    session.add_all(rows)
    await session.flush()

    ref_inserts = 0
    for xref in xrefs:
        from_id = eid_to_id.get(xref.from_eid)
        if not from_id:
            continue
        to_id = eid_to_id.get(xref.to_external_ref) if xref.ref_type == "internal" else None
        session.add(
            CrossReference(
                from_provision=from_id,
                to_provision=to_id,
                to_external_ref=None if to_id else xref.to_external_ref,
                ref_type=xref.ref_type,
                detected_by=xref.detected_by,
                confidence=xref.confidence,
            )
        )
        ref_inserts += 1

    await log_event(
        session,
        actor_type="system",
        actor_id="ingest.cnb",
        action="ingest.document.complete",
        target_type="source_document",
        target_id=str(sd.id),
        payload={
            "external_id": external_id,
            "publisher": "ČNB",
            "n_provisions": len(provs),
            "n_cross_refs": ref_inserts,
            "raw_uri": artefact.uri,
            "checksum": artefact.sha256,
            **stats,
        },
    )
    await emit(
        "ingest.document.complete",
        publisher="ČNB",
        external_id=external_id,
        n_provisions=len(provs),
    )
    return {
        "external_id": external_id,
        "source_document_id": str(sd.id),
        "n_provisions": len(provs),
        "n_cross_refs": ref_inserts,
        "raw_uri": artefact.uri,
        "split_mode": stats["split_mode"],
    }


def _pretty_title(stem: str) -> str:
    """Derive a title from a file stem, e.g. 'us_2019_07' becomes 'us 2019/07'.

    Underscores become spaces and adjacent number pairs are joined with a slash.
    """
    s = re.sub(r"_", " ", stem)
    s = re.sub(r"\b(\d+)\s+(\d+)\b", r"\1/\2", s)
    return s.strip()


def _provision_row(p: ProvisionDraft, sd: SourceDocument, row_id: UUID) -> Provision:
    quality = p.metadata.get("ingestion_quality_raw")
    return Provision(
        id=row_id,
        source_document_id=sd.id,
        parent_id=None,
        hierarchy_path=p.hierarchy_path,
        ordinal=p.ordinal,
        label=p.label,
        eid=p.eid,
        text=p.text,
        text_format="gfm",
        ingestion_quality=quality,
        text_hash=hashlib.sha256(p.text.encode("utf-8")).hexdigest(),
        language=sd.language,
        legal_hierarchy=sd.legal_hierarchy,
        jurisdiction=sd.jurisdiction,
        metadata_={
            "source_eid": p.eid,
            "split_mode": p.metadata.get("split_mode", "clanek"),
        },
    )


async def _delete_existing(session: AsyncSession, external_id: str) -> None:
    existing = (
        (
            await session.execute(
                select(SourceDocument).where(
                    SourceDocument.external_id == external_id,
                    SourceDocument.publisher == "ČNB",
                )
            )
        )
        .scalars()
        .all()
    )
    if not existing:
        return
    for sd in existing:
        await session.delete(sd)
    await session.flush()


# ---------------------------------------------------------------------------
# Folder walker and CLI
# ---------------------------------------------------------------------------
def discover_pdfs(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")


async def _ingest_folder(folder: Path | None = None, only: list[str] | None = None) -> list[dict]:
    settings = get_settings()
    target = folder or (settings.data_dir / "cnb_pdfs")
    pdfs = discover_pdfs(target)
    if only:
        only_set = set(only)
        pdfs = [p for p in pdfs if p.name in only_set or p.stem in only_set]
    if not pdfs:
        print(f"no PDFs found in {target} — drop ČNB methodologies here, then re-run.")
        return []
    results: list[dict] = []
    for pdf in pdfs:
        try:
            r = await ingest_pdf(pdf)
            print(
                f"OK  {pdf.name:50s}  provisions={r['n_provisions']:>4d}  "
                f"xrefs={r['n_cross_refs']:>4d}  mode={r['split_mode']}"
            )
            results.append(r)
        except Exception as exc:
            print(f"ERR {pdf.name:50s}  {exc!r}")
            results.append({"external_id": pdf.stem, "error": repr(exc)})
    return results


def main() -> None:
    from regsuper.observability.logging_config import configure_logging

    configure_logging()
    p = argparse.ArgumentParser(description="Ingest ČNB methodological PDFs.")
    p.add_argument(
        "--folder", type=Path, default=None, help="Override the default data/cnb_pdfs/ folder."
    )
    p.add_argument(
        "--only", action="append", default=None, help="Restrict to specific filenames or stems."
    )
    args = p.parse_args()
    asyncio.run(_ingest_folder(args.folder, args.only))


if __name__ == "__main__":
    main()
