"""Connector skeleton for GFŘ tax guidance (soft law); parsing only, nothing is fetched or stored.

The Generální finanční ředitelství (GFŘ, the General Financial Directorate) publishes "D" series
instructions (pokyny řady D), information notices (informace) and communications (sdělení). They
bind the tax offices' own practice and shape how the law is applied but are not law, so every
document is SOFT_LAW. The structure follows cnb_vestnik.py (HTML listing, then PDF, then
ProvisionDraft rows); ``ingest_doc`` lists what is still missing before a live ingest.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field

# Bindingness of every document this connector emits.
BINDINGNESS = "SOFT_LAW"
PUBLISHER = "GFŘ"
_HOST = "https://www.financnisprava.cz"
# The guidance listing page (not fetched by this module).
_LISTING_PATH = "/cs/dane/legislativa-a-metodika/pokyny-d"
_UA = "Mozilla/5.0 (pythia-ingest; Nexus legal-graph; +https://example.invalid)"

_FURNITURE = re.compile(r"^\s*(Strana \d+|Č\.\s*j\.:|GENERÁLNÍ FINANČNÍ ŘEDITELSTVÍ|Lazarská|\d+\s*$)", re.IGNORECASE)


def connector_enabled() -> bool:
    """Return True if PYTHIA_GFR_CONNECTOR_ENABLED is set; the connector is off by default."""
    return os.getenv("PYTHIA_GFR_CONNECTOR_ENABLED", "0").strip() in {"1", "true", "True", "yes"}


@dataclass
class GfrDoc:
    ext: str  # external_id, e.g. 'GFŘ-D-59'
    title: str
    url: str
    kind: str = "pokyn"  # 'pokyn', 'informace' or 'sdeleni'
    metadata: dict = field(default_factory=dict)


def parse_listing(html_bytes: bytes) -> list[GfrDoc]:
    """Parse the guidance listing HTML into GfrDoc entries (modelled on cnb_vestnik.parse_year_page).

    Each anchor that links a PDF and whose text names a pokyn, informace or sdělení becomes a GfrDoc.
    The selector is provisional; it is kept pure so it can be fitted to the live page later.
    """
    from lxml import html as lhtml

    root = lhtml.fromstring(html_bytes, parser=lhtml.HTMLParser(encoding="utf-8"))
    out: list[GfrDoc] = []
    for a in root.xpath('//a[contains(@href, ".pdf")]'):
        href = a.get("href") or ""
        text = re.sub(r"\s+", " ", a.text_content()).strip()
        m = re.search(r"\b(Pokyn\s+(?:GFŘ\s+)?D-\d+|Informace\s+GFŘ|Sdělení\s+GFŘ)\b", text, re.IGNORECASE)
        if not m:
            continue
        num = re.search(r"D-(\d+)", text, re.IGNORECASE)
        ext = f"GFŘ-D-{num.group(1)}" if num else f"GFŘ-{abs(hash(text)) % 100000}"
        kind = "informace" if "informace" in text.lower() else "sdeleni" if "sdělení" in text.lower() else "pokyn"
        url = href if href.startswith("http") else _HOST + href
        out.append(GfrDoc(ext=ext, title=text[:200], url=url, kind=kind))
    return out


def parse_pdf(data: bytes):
    """Split a born-digital GFŘ PDF into ProvisionDraft rows.

    Works like cnb_vestnik.parse_pdf: page furniture is stripped and the text is segmented on
    Roman-numeral sections, articles (Čl. N) and "K § N" commentary headings. A short memo without
    such headings becomes a single provision.
    """
    import pypdf

    from pythia.ingest.esbirka import ProvisionDraft

    raw = "\n".join(pg.extract_text() or "" for pg in pypdf.PdfReader(io.BytesIO(data)).pages)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw.split("\n")]
    lines = [ln for ln in lines if ln and not _FURNITURE.match(ln)]
    if not lines:
        return [ProvisionDraft("text", "Text", "text", 0, "")]
    sec = re.compile(r"^\s*(?:(Čl\.\s*\d+|K\s+§\s*\d+|[IVXL]{1,5}\.)\s+)\S")
    provs, buf, label, eid = [], [], None, None

    def flush():
        if label and buf:
            provs.append(ProvisionDraft(eid, label, eid, len(provs), "\n".join(buf)))

    head: list[str] = []
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


async def fetch_listing(client) -> list[GfrDoc]:
    """Fetch and parse the live guidance listing (not called anywhere yet)."""
    r = await client.get(_HOST + _LISTING_PATH)
    r.raise_for_status()
    return parse_listing(r.content)


async def ingest_doc(client, doc: GfrDoc, *, store: bool = False) -> dict:
    """Fetch one guidance PDF and return a summary of its provisions (always SOFT_LAW).

    There is no store path yet, and nothing calls this. Still missing before a live ingest:
      * the live listing crawl and PDF fetch;
      * 'pokyn' / 'informace' document types in the taxonomy registry (taxonomy.py), which need a
        separate review; documents map to the existing SOFT_LAW 'metodicky_pokyn' / 'sdeleni'
        types meanwhile;
      * a cap on the share of soft-law provisions per query, so soft law never crowds hard law
        out of the retrieval budget. Every soft-law connector needs it before its documents are
        served.

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
        "GFŘ store path is a deferred human-approved step (needs the soft-law k-budget crowding cap "
        "+ the 'pokyn' doctype governance sign-off)."
    )


async def ingest_sample(*, cap: int = 10, apply: bool = False) -> dict:
    """Return a status dict: DISABLED unless the flag is on, otherwise a skeleton notice (no fetch)."""
    if not connector_enabled():
        return {"mode": "DISABLED", "flag": "PYTHIA_GFR_CONNECTOR_ENABLED", "publisher": PUBLISHER}
    return {
        "mode": "SKELETON (no fetch executed)",
        "publisher": PUBLISHER,
        "bindingness": BINDINGNESS,
        "note": "live crawl + ingest deferred to a human-approved snapshot step",
        "cap": cap,
    }
