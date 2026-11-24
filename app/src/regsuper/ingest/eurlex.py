"""EUR-Lex ingester: fetch FORMEX 4 XML, convert it to GFM Markdown and insert provisions.

None of the target acts are published as Akoma Ntoso; EUR-Lex serves them as FORMEX 4
(`formex-05.55-20141201.xd`), which `fmx4.py` walks. A CELEX ID is resolved by fetching its
notice object from publications.europa.eu, picking the Cellar manifestation whose URIs include
`.fmx4`, and downloading `<cellar>/DOC_2` (the canonical ACT XML). `DOC_1`, a sibling
`.doc.xml` wrapper, is used only when `DOC_2` does not return an FMX4 `<ACT>` document.
A healthy run yields 100 to 800 provisions per act.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
from lxml import etree
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from regsuper.ingest import fmx4
from regsuper.ingest.storage import save_raw
from regsuper.observability.events import emit
from regsuper.observability.logging_config import get_logger
from regsuper.observability.resource_guard import gate_storage
from regsuper.storage.audit import log_event
from regsuper.storage.database import session_scope
from regsuper.storage.models import CrossReference, Provision, SourceDocument

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Target CELEX list
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CelexSpec:
    celex: str
    short: str
    title: str
    document_type: str
    legal_hierarchy: str  # 'eu-regulation' | 'eu-directive'


MVP_CELEX: tuple[CelexSpec, ...] = (
    CelexSpec(
        "32011L0061",
        "AIFMD",
        "Directive 2011/61/EU on Alternative Investment Fund Managers",
        "directive",
        "eu-directive",
    ),
    CelexSpec(
        "32014L0065",
        "MiFID II",
        "Directive 2014/65/EU on markets in financial instruments",
        "directive",
        "eu-directive",
    ),
    CelexSpec(
        "32014R0600",
        "MiFIR",
        "Regulation (EU) No 600/2014 on markets in financial instruments",
        "regulation",
        "eu-regulation",
    ),
    CelexSpec(
        "32016R0679",
        "GDPR",
        "Regulation (EU) 2016/679 (General Data Protection Regulation)",
        "regulation",
        "eu-regulation",
    ),
    CelexSpec(
        "32015L0849",
        "5AMLD",
        "Directive (EU) 2015/849 on prevention of money laundering",
        "directive",
        "eu-directive",
    ),
    CelexSpec(
        "32023R1114",
        "MiCA",
        "Regulation (EU) 2023/1114 on markets in crypto-assets",
        "regulation",
        "eu-regulation",
    ),
)


_NOTICE_URL = "https://publications.europa.eu/resource/celex/{celex}"
_NOTICE_HEADERS = {
    "Accept-Language": "en",
    "Accept": "application/xml;notice=object",
}
_USER_AGENT = "regsuper-mvp/0.0.1 (+https://example.invalid)"


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------
async def _http_get(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    headers = {"User-Agent": _USER_AGENT, **kwargs.pop("headers", {})}
    r = await client.get(url, headers=headers, **kwargs)
    r.raise_for_status()
    return r


def _find_fmx4_manifestation(notice_xml: bytes) -> str | None:
    """From a notice-object XML, find the cellar URI of the FMX4 manifestation."""
    try:
        root = etree.fromstring(notice_xml)
    except etree.XMLSyntaxError as exc:
        _log.warning("notice_parse_failed", error=repr(exc))
        return None

    for mblock in root.iter("EXPRESSION_MANIFESTED_BY_MANIFESTATION"):
        cellar = None
        has_fmx4 = False
        for uri in mblock.iter("URI"):
            val = uri.findtext("VALUE") or ""
            typ = uri.findtext("TYPE") or ""
            if typ == "cellar" and not cellar:
                cellar = val
            if val.endswith(".fmx4"):
                has_fmx4 = True
        if has_fmx4 and cellar:
            return cellar
    return None


def _pick_act_from_zip(zip_bytes: bytes) -> bytes:
    """Extract the FMX4 `<ACT>` XML from a ZIP-delivered manifestation.

    Newer EUR-Lex acts (e.g. MiCA, from 2023 onwards) ship FMX4 as a ZIP holding the main act
    XML plus corrigenda and annex XMLs. Picks the file whose root is `<ACT>`, preferring the
    largest match, which is the body of the act.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        candidates: list[tuple[int, bytes]] = []
        for info in zf.infolist():
            if not info.filename.lower().endswith(".xml"):
                continue
            if info.filename.lower().endswith(".doc.xml"):
                continue  # bibliographic wrapper, not the act body
            data = zf.read(info.filename)
            if b"<ACT" in data[:1000]:
                candidates.append((info.file_size, data))
        if not candidates:
            raise RuntimeError("ZIP manifestation contains no FMX4 <ACT> XML")
        candidates.sort(reverse=True)
        return candidates[0][1]


async def fetch_celex(celex: str) -> tuple[bytes, str]:
    """Resolve a CELEX ID to its Cellar manifestation and download the FMX4 XML.

    Returns (xml_bytes, manifestation_cellar_uri).
    """
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        notice = await _http_get(client, _NOTICE_URL.format(celex=celex), headers=_NOTICE_HEADERS)
        manifestation = _find_fmx4_manifestation(notice.content)
        if not manifestation:
            raise RuntimeError(f"no FMX4 manifestation found for CELEX {celex}")

        # Try DOC_2, then DOC_1. Some manifestations only have DOC_1 and deliver the FMX4 as
        # a ZIP archive (e.g. MiCA and other acts from 2023 onwards).
        for doc in ("DOC_2", "DOC_1"):
            r = await client.get(
                f"{manifestation}/{doc}",
                headers={"User-Agent": _USER_AGENT, "Accept-Language": "en"},
            )
            if r.status_code != 200 or not r.content:
                continue
            head = r.content[:200]
            if r.content.startswith(b"PK\x03\x04"):
                return _pick_act_from_zip(r.content), manifestation
            if b"<ACT" in head:
                return r.content, manifestation
        raise RuntimeError(f"no FMX4 ACT XML returned for CELEX {celex}")


# ---------------------------------------------------------------------------
# Database insertion
# ---------------------------------------------------------------------------
async def ingest_celex(spec: CelexSpec, *, session: AsyncSession | None = None) -> dict:
    """Ingest one CELEX ID end to end.

    Idempotent: re-running on the same CELEX deletes its provisions and cross-references and
    inserts them again. Old row IDs are not preserved; the audit log carries the history.
    """
    own_session = session is None
    ctx = session_scope() if own_session else _passthrough(session)
    async with ctx as sess:
        return await _ingest_celex_inner(spec, sess)


class _passthrough:
    def __init__(self, sess: AsyncSession):
        self._sess = sess

    async def __aenter__(self) -> AsyncSession:
        return self._sess

    async def __aexit__(self, *_exc) -> None:
        return None


async def _ingest_celex_inner(spec: CelexSpec, session: AsyncSession) -> dict:
    await emit(
        "ingest.document.started", publisher="EUR-Lex", external_id=spec.celex, name=spec.short
    )

    xml_bytes, manifestation_uri = await fetch_celex(spec.celex)
    # Refuse before the raw write if it would push free disk below the floor: a clean refusal
    # is better than a disk-full crash halfway through embedding.
    await gate_storage(required_bytes=len(xml_bytes))
    artefact = save_raw("eurlex", spec.celex, "xml", xml_bytes)

    root = etree.fromstring(xml_bytes)
    provs, xrefs = fmx4.walk_act(root)

    if not provs:
        raise RuntimeError(f"walker produced 0 provisions for {spec.celex}")

    # Wipe prior rows for this CELEX, if any, so the run is idempotent.
    await _delete_existing(session, spec.celex)

    sd = SourceDocument(
        external_id=spec.celex,
        title=spec.title,
        publisher="EUR-Lex",
        jurisdiction="EU",
        legal_hierarchy=spec.legal_hierarchy,
        document_type=spec.document_type,
        language="en",
        raw_storage_uri=artefact.uri,
        raw_format="xml-formex-4",
        raw_checksum=artefact.sha256,
        fetched_at=datetime.now(UTC),
        last_checked_at=datetime.now(UTC),
        status="ingested",
        metadata_={
            "manifestation_uri": manifestation_uri,
            "short": spec.short,
            "fmx4_size_bytes": artefact.size,
        },
    )
    session.add(sd)
    await session.flush()  # need sd.id

    # First pass: assign UUIDs and build article-level rows in a batch.
    eid_to_id: dict[str, UUID] = {}
    article_rows: list[Provision] = []
    for p in (x for x in provs if x.parent_eid is None):
        row_id = uuid4()
        eid_to_id[p.eid] = row_id
        article_rows.append(_provision_row(p, sd, parent_id=None, row_id=row_id))
    session.add_all(article_rows)
    await session.flush()

    # Second pass: paragraph rows with parent_id (parent IDs already known).
    para_rows: list[Provision] = []
    for p in (x for x in provs if x.parent_eid is not None):
        row_id = uuid4()
        eid_to_id[p.eid] = row_id
        parent_id = eid_to_id.get(p.parent_eid)
        para_rows.append(_provision_row(p, sd, parent_id=parent_id, row_id=row_id))
    session.add_all(para_rows)
    await session.flush()

    # Cross-references: resolve targets within the same document where possible.
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
        actor_id="ingest.eurlex",
        action="ingest.document.complete",
        target_type="source_document",
        target_id=str(sd.id),
        payload={
            "celex": spec.celex,
            "publisher": "EUR-Lex",
            "n_provisions": len(provs),
            "n_cross_refs": ref_inserts,
            "raw_uri": artefact.uri,
            "checksum": artefact.sha256,
        },
    )
    result = {
        "celex": spec.celex,
        "source_document_id": str(sd.id),
        "n_provisions": len(provs),
        "n_cross_refs": ref_inserts,
        "raw_uri": artefact.uri,
    }
    await emit(
        "ingest.document.complete",
        publisher="EUR-Lex",
        external_id=spec.celex,
        n_provisions=len(provs),
    )
    return result


def _provision_row(
    p: fmx4.ProvisionDraft,
    sd: SourceDocument,
    *,
    parent_id: UUID | None,
    row_id: UUID,
) -> Provision:
    return Provision(
        id=row_id,
        source_document_id=sd.id,
        parent_id=parent_id,
        hierarchy_path=p.hierarchy_path,
        ordinal=p.ordinal,
        label=p.label,
        eid=p.eid,
        text=p.text,
        text_format="gfm",
        ingestion_quality=fmx4.score_quality(p.text),
        text_hash=hashlib.sha256(p.text.encode("utf-8")).hexdigest(),
        language=sd.language,
        legal_hierarchy=sd.legal_hierarchy,
        jurisdiction=sd.jurisdiction,
        metadata_={"source_eid": p.eid},
    )


async def _delete_existing(session: AsyncSession, celex: str) -> None:
    """Remove prior provisions for this CELEX so re-runs are idempotent."""
    existing = (
        (await session.execute(select(SourceDocument).where(SourceDocument.external_id == celex)))
        .scalars()
        .all()
    )
    if not existing:
        return
    for sd in existing:
        await session.delete(sd)
    await session.flush()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
async def _ingest_all(celex_list: list[str] | None = None) -> list[dict]:
    """Ingest every CELEX in the target list, or only the given subset."""
    targets = MVP_CELEX
    if celex_list:
        wanted = set(celex_list)
        targets = tuple(c for c in MVP_CELEX if c.celex in wanted)
        if len(targets) != len(wanted):
            missing = wanted - {c.celex for c in MVP_CELEX}
            raise SystemExit(f"unknown CELEX IDs: {sorted(missing)}")
    results: list[dict] = []
    for spec in targets:
        try:
            r = await ingest_celex(spec)
            print(
                f"OK  {spec.celex}  {spec.short:8s}  "
                f"provisions={r['n_provisions']:>4d}  xrefs={r['n_cross_refs']:>4d}"
            )
            results.append(r)
        except Exception as exc:
            print(f"ERR {spec.celex}  {spec.short:8s}  {exc!r}")
            results.append({"celex": spec.celex, "error": repr(exc)})
    return results


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="Ingest EUR-Lex acts into the regsuper DB.")
    p.add_argument(
        "--celex",
        action="append",
        default=None,
        help="One or more CELEX IDs to ingest (omit for the full MVP set).",
    )
    args = p.parse_args()
    asyncio.run(_ingest_all(args.celex))


if __name__ == "__main__":
    main()
