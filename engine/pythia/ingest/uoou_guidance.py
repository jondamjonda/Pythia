"""Connector skeleton for ÚOOÚ data-protection guidance (soft law); parsing only, no fetch or store.

The Úřad pro ochranu osobních údajů (ÚOOÚ, the Czech data protection authority) publishes guidance
(metodiky, stanoviska, vyjádření) that shapes how the GDPR and zákon č. 110/2019 Sb. are applied but
is not binding law, so every document is SOFT_LAW. The structure follows cnb_vestnik.py (HTML
listing, then PDF, then ProvisionDraft rows); ``ingest_doc`` lists what is still missing.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field

BINDINGNESS = "SOFT_LAW"
PUBLISHER = "ÚOOÚ"
_HOST = "https://www.uoou.cz"
_LISTING_PATH = "/metodiky-a-stanoviska/os-1000"  # not fetched by this module
_UA = "Mozilla/5.0 (pythia-ingest; Nexus legal-graph; +https://example.invalid)"

_FURNITURE = re.compile(r"^\s*(Strana \d+|Č\.\s*j\.:|ÚŘAD PRO OCHRANU|Pplk\. Sochora|\d+\s*$)", re.IGNORECASE)


def connector_enabled() -> bool:
    """Return True if PYTHIA_UOOU_CONNECTOR_ENABLED is set; the connector is off by default."""
    return os.getenv("PYTHIA_UOOU_CONNECTOR_ENABLED", "0").strip() in {"1", "true", "True", "yes"}


@dataclass
class UoouDoc:
    ext: str  # external_id, e.g. 'ÚOOÚ-STAN-3/2024'
    title: str
    url: str
    kind: str = "stanovisko"  # 'stanovisko', 'metodika' or 'vyjadreni'
    metadata: dict = field(default_factory=dict)


def parse_listing(html_bytes: bytes) -> list[UoouDoc]:
    """Parse the guidance listing HTML into UoouDoc entries (modelled on cnb_vestnik.parse_year_page)."""
    from lxml import html as lhtml

    root = lhtml.fromstring(html_bytes, parser=lhtml.HTMLParser(encoding="utf-8"))
    out: list[UoouDoc] = []
    for a in root.xpath('//a[contains(@href, ".pdf")]'):
        href = a.get("href") or ""
        text = re.sub(r"\s+", " ", a.text_content()).strip()
        m = re.search(r"\b(Stanovisko|Metodik\w+|Vyjádření)\b", text, re.IGNORECASE)
        if not m:
            continue
        kind = "metodika" if "metodik" in text.lower() else "vyjadreni" if "vyjádření" in text.lower() else "stanovisko"
        ext = f"ÚOOÚ-{kind[:4].upper()}-{abs(hash(text)) % 100000}"
        url = href if href.startswith("http") else _HOST + href
        out.append(UoouDoc(ext=ext, title=text[:200], url=url, kind=kind))
    return out


def parse_pdf(data: bytes):
    """Split a born-digital ÚOOÚ PDF into ProvisionDraft rows, like cnb_vestnik.parse_pdf."""
    import pypdf

    from pythia.ingest.esbirka import ProvisionDraft

    raw = "\n".join(pg.extract_text() or "" for pg in pypdf.PdfReader(io.BytesIO(data)).pages)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw.split("\n")]
    lines = [ln for ln in lines if ln and not _FURNITURE.match(ln)]
    if not lines:
        return [ProvisionDraft("text", "Text", "text", 0, "")]
    sec = re.compile(r"^\s*(?:(Čl\.\s*\d+|[IVXL]{1,5}\.|\d+\.)\s+)\S")
    provs, buf, label, eid, head = [], [], None, None, []

    def flush():
        if label and buf:
            provs.append(ProvisionDraft(eid, label, eid, len(provs), "\n".join(buf)))

    for ln in lines:
        if sec.match(ln):
            flush()
            label = ln.split(None, 1)[0]
            eid = re.sub(r"[^\w]+", "_", label).strip("_").lower() or f"sec_{len(provs)}"
            buf = [ln]
        elif label:
            buf.append(ln)
        else:
            head.append(ln)
    flush()
    if not provs:
        return [ProvisionDraft("text", "Text", "text", 0, "\n".join(head or lines))]
    if head:
        provs.insert(0, ProvisionDraft("zahlavi", "Záhlaví", "zahlavi", 0, "\n".join(head)))
    return provs


async def fetch_listing(client) -> list[UoouDoc]:
    """Fetch and parse the live guidance listing (not called anywhere yet)."""
    r = await client.get(_HOST + _LISTING_PATH)
    r.raise_for_status()
    return parse_listing(r.content)


async def ingest_doc(client, doc: UoouDoc, *, store: bool = False) -> dict:
    """Fetch one guidance PDF and return a summary of its provisions (always SOFT_LAW).

    There is no store path yet, and nothing calls this. Still missing before a live ingest: the live
    crawl and PDF fetch; reviewed document types in the taxonomy registry (documents map to the
    existing SOFT_LAW 'metodicky_pokyn' / 'vykladove_stanovisko' types meanwhile); and a per-query
    cap on soft-law provisions so they never crowd hard law out of the retrieval budget.

    Raises:
        NotImplementedError: if `store` is True.
    """
    r = await client.get(doc.url)
    r.raise_for_status()
    provs = parse_pdf(r.content)
    result = {
        "ext": doc.ext,
        "title": doc.title[:60],
        "n_provisions": len(provs),
        "bindingness": BINDINGNESS,
        "publisher": PUBLISHER,
        "kind": doc.kind,
    }
    if not store:
        return result
    raise NotImplementedError(
        "ÚOOÚ store path is a deferred human-approved step (needs the soft-law k-budget crowding cap "
        "+ the doctype governance sign-off)."
    )


async def ingest_sample(*, cap: int = 10, apply: bool = False) -> dict:
    """Return a status dict: DISABLED unless the flag is on, otherwise a skeleton notice (no fetch)."""
    if not connector_enabled():
        return {"mode": "DISABLED", "flag": "PYTHIA_UOOU_CONNECTOR_ENABLED", "publisher": PUBLISHER}
    return {
        "mode": "SKELETON (no fetch executed)",
        "publisher": PUBLISHER,
        "bindingness": BINDINGNESS,
        "note": "live crawl + ingest deferred to a human-approved snapshot step",
        "cap": cap,
    }
