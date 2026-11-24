r"""Connector for the Věstník ČNB: the Czech National Bank's soft law and binding measures.

Ingests úřední sdělení (official communications and recommendations, SOFT_LAW) from the Věstník,
the bank's bulletin, and opatření obecné povahy (OOP, "measures of a general nature" such as the
countercyclical capital buffer rate, BINDING_OOP), which are published as individual PDFs on the
financial-stability pages instead. Unlike statutes (HARD_LAW_ERGA_OMNES), these carry lower or
different bindingness. Both are born-digital PDFs (pypdf extracts the text; no OCR needed).

Run (from engine/): .venv/bin/python -m pythia.ingest.cnb_vestnik [--cap N] [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import logging
import pathlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import uuid4

import httpx
from lxml import html as lhtml

from pythia.db import get_pool
from pythia.graph import taxonomy as tx
from pythia.ingest.esbirka import ProvisionDraft, score_quality

_log = logging.getLogger("pythia.ingest.cnb_vestnik")

_HOST = "https://www.cnb.cz"
_UA = "Mozilla/5.0 (pythia-ingest; Nexus legal-graph; +https://example.invalid)"
_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_RAW_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "data" / "raw" / "cnb_vestnik"

_YEARS = (2026, 2025)  # recent years publish exactly one PDF per document; older years are not crawled
_CZ_MONTHS = {
    "ledna": 1,
    "února": 2,
    "března": 3,
    "dubna": 4,
    "května": 5,
    "června": 6,
    "července": 7,
    "srpna": 8,
    "září": 9,
    "října": 10,
    "listopadu": 11,
    "prosince": 12,
}

_MON = r"[a-zžščřďťňáéíóúůě]+"


def parse_ucinnost(text: str) -> date | None:
    """Return the statutory effect date (účinnost) stated in a binding ČNB OOP, or None.

    Matches 'nabývá účinnosti [dnem] D. <month> YYYY' (month as a word) or 'D. M. YYYY' (numeric).
    The on-publication clause ('dnem vyhlášení', no calendar date) and anything unparseable return
    None; no date is ever guessed. Use only for binding OOP documents.
    """
    t = re.sub(r"\s+", " ", text or "")
    m = re.search(r"nab[ýy]v[áa]\s+účinnosti(?:\s+dnem)?\s+(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})", t, re.I)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    m = re.search(rf"nab[ýy]v[áa]\s+účinnosti(?:\s+dnem)?\s+(\d{{1,2}})\.\s*({_MON})\s+(\d{{4}})", t, re.I)
    if m and (mon := _CZ_MONTHS.get(m.group(2).lower())):
        try:
            return date(int(m.group(3)), mon, int(m.group(1)))
        except ValueError:
            return None
    return None


def parse_application(text: str) -> date | None:
    """Return the date from which a ČNB úřední sdělení applies, or None.

    Matches only the self-referential 'Podle tohoto úředního sdělení … se postupuje od <date>' (month
    as a word or numeric). Requiring 'tohoto' ("this") stops a 'ze dne' reference to a repealed
    document from being taken as this document's date. The result is not a statutory effect date and
    must never be promoted to valid_from.
    """
    t = re.sub(r"\s+", " ", text or "")
    m = re.search(
        rf"Podle\s+tohoto\s+úředního\s+sdělení[^.]*?se\s+postupuje\s+od\s+"
        rf"(\d{{1,2}})\.\s*(?:(\d{{1,2}})\.\s*(\d{{4}})|({_MON})\s+(\d{{4}}))",
        t,
        re.I,
    )
    if not m:
        return None
    try:
        if m.group(2):  # numeric: D. M. YYYY
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        mon = _CZ_MONTHS.get((m.group(4) or "").lower())  # month as a word: D. <month> YYYY
        return date(int(m.group(5)), mon, int(m.group(1))) if mon else None
    except (ValueError, TypeError):
        return None


# Curated OOP (BINDING_OOP) PDFs: the archive listing is rendered by JavaScript and cannot be crawled
# with GET, but each PDF is a plain GET. ČNB issues an OOP only when it changes a rate (one to four a
# year), so a short hand-kept list suffices.
OOP_DOCS = (
    (
        "OOP-I/2026",
        "Opatření obecné povahy ČNB č. I/2026 ke stanovení sazby proticyklické kapitálové rezervy",
        "/export/sites/cnb/cs/financni-stabilita/.galleries/makroobezretnostni_politika/opatreni-obecne-povahy/oop_i_2026_banky_druzstva.pdf",
    ),
    (
        "OOP-I/2024",
        "Opatření obecné povahy ČNB č. I/2024 ke stanovení sazby proticyklické kapitálové rezervy",
        "/export/sites/cnb/cs/financni-stabilita/.galleries/makroobezretnostni_politika/opatreni-obecne-povahy/oop_i_2024_banky_druzstva.pdf",
    ),
)

_FURNITURE = re.compile(
    r"^\s*(Strana \d+|Věstník ČNB\b|Počet stran:|NA PŘÍKOPĚ|\d{3} \d{2} PRAHA|"
    r"Třídicí znak|Č\.j\.:|\d+\s*$)",
    re.IGNORECASE,
)
_SEC = re.compile(r"^\s*([IVXL]{1,5})\.\s+\S")  # Roman-numeral section header (I., II., …)
_DOC = re.compile(
    r"^\s*(\d+)\.\s*(Úřední sdělení|Opatření[^\(]*|Metodick[^\(]*).*?ze dne\s+"
    r"(\d+)\.\s*(\w+)\s*(\d{4})",
    re.IGNORECASE,
)


@dataclass
class CnbDoc:
    ext: str  # external_id, e.g. 'ČNB-ÚS-23/2025' or 'OOP-I/2026'
    title: str
    url: str
    kind: str  # 'us' (úřední sdělení) or 'oop'


def _parse_cz_date(d: str, mon: str, y: str):
    from datetime import date

    m = _CZ_MONTHS.get(mon.lower())
    return date(int(y), m, int(d)) if m else None


def parse_year_page(html_bytes: bytes, year: int) -> list[tuple[CnbDoc, str]]:
    """Parse a Věstník year page into (CnbDoc, issue date or None) pairs, skipping index files.

    The page (GET /cs/legislativa/vestnik-cnb/vestnik-cnb-rocnik-{YYYY}/) lists one PDF per
    document; the href is used verbatim and the *_obsah / *_seznamy index files are skipped. The
    anchor text is the number plus 'Úřední sdělení … ze dne …'.
    """
    root = lhtml.fromstring(html_bytes, parser=lhtml.HTMLParser(encoding="utf-8"))
    out, seen = [], set()
    for a in root.xpath('//a[contains(@href, "Vestnik-CNB") and contains(@href, ".pdf")]'):
        href = a.get("href") or ""
        if "_obsah" in href or "_seznamy" in href:
            continue
        znak = re.search(r"_(\d{6,})\.pdf$", href)
        if not znak or znak.group(1) in seen:
            continue
        text = re.sub(r"\s+", " ", a.text_content()).strip()
        m = _DOC.match(text)
        if not m:
            continue
        seen.add(znak.group(1))
        num, label = m.group(1), _norm_label(m.group(2))
        d = _parse_cz_date(m.group(3), m.group(4), m.group(5))
        ext = f"ČNB-{ {'us': 'ÚS', 'oop': 'OOP', 'metod': 'MP'}[label] }-{num}/{year}"
        title = re.sub(r"\s*\(pdf.*$", "", text).strip()
        out.append((CnbDoc(ext=ext, title=title, url=_HOST + href, kind=label), d))  # d may be None
    return out


def _norm_label(s: str) -> str:
    s = s.lower()
    return "oop" if "opatření" in s else "metod" if "metod" in s else "us"


# ── PDF parsing ──────────────────────────────────────────────────────────────
def parse_pdf(data: bytes) -> list[ProvisionDraft]:
    """Split a born-digital ČNB PDF into provisions on its Roman-numeral sections.

    Page furniture (page numbers, running headers, the bank's address block) is stripped first.
    """
    import pypdf

    raw = "\n".join(pg.extract_text() or "" for pg in pypdf.PdfReader(io.BytesIO(data)).pages)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw.split("\n")]
    lines = [ln for ln in lines if ln and not _FURNITURE.match(ln)]

    provs: list[ProvisionDraft] = []
    head: list[str] = []
    label = eid = None
    buf: list[str] = []

    def flush():
        if label and buf:
            provs.append(ProvisionDraft(eid, label, eid, len(provs), "\n".join(buf)))

    for ln in lines:
        m = _SEC.match(ln)
        if m:
            flush()
            label, eid, buf = f"Část {m.group(1)}", f"sec_{m.group(1).lower()}", [ln]
        elif label:
            buf.append(ln)
        else:
            head.append(ln)
    flush()
    if not provs:  # a short memo without sections becomes a single provision
        return [ProvisionDraft("text", "Text", "text", 0, "\n".join(head or lines))]
    if head:
        provs.insert(0, ProvisionDraft("zahlavi", "Záhlaví", "zahlavi", 0, "\n".join(head)))
        provs = [ProvisionDraft(p.eid, p.label, p.hierarchy_path, i, p.text) for i, p in enumerate(provs)]
    return provs


# ── store (mirrors esbirka.ingest_act) ───────────────────────────────────────
def _save_raw(ext: str, blob: bytes) -> tuple[str, str]:
    _RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = _RAW_DIR / (re.sub(r"[^\w.-]", "_", ext) + ".pdf")
    path.write_bytes(blob)
    return path.as_uri(), hashlib.sha256(blob).hexdigest()


async def _delete_existing(conn, ext: str) -> None:
    await conn.execute(
        "DELETE FROM provision WHERE source_document_id IN "
        "(SELECT id FROM source_document WHERE external_id=$1 AND publisher='ČNB')",
        ext,
    )
    await conn.execute("DELETE FROM source_document WHERE external_id=$1 AND publisher='ČNB'", ext)


async def ingest_doc(client: httpx.AsyncClient, doc: CnbDoc, valid_from, *, store: bool = True) -> dict:
    r = await client.get(doc.url)
    r.raise_for_status()
    if not r.content[:4] == b"%PDF":
        raise RuntimeError(f"{doc.ext}: not a PDF ({r.content[:8]!r})")
    provs = parse_pdf(r.content)
    body = " ".join(p.text for p in provs)[:4000]
    # Classify from the title (úřední sdělení map to uredni_sdeleni / SOFT_LAW, opatření obecné povahy
    # to oop / BINDING_OOP); the PDF body can upgrade bindingness when it states it is an OOP.
    cls = tx.classify(title=doc.title, publisher="ČNB", jurisdiction="CZ")
    binding = "BINDING_OOP" if ("opatření obecné povahy" in body.lower() and doc.kind != "us") else cls.bindingness
    # A binding OOP states its statutory effect date in its text, and that becomes valid_from.
    # Soft-law úřední sdělení keep the 'ze dne' adoption date passed in, which provenance.build
    # maps to date_adopted.
    if binding == "BINDING_OOP":
        eff = parse_ucinnost(" ".join(p.text for p in provs))
        if eff is not None:
            valid_from = eff
    if not store:
        return {
            "ext": doc.ext,
            "title": doc.title[:60],
            "n_provisions": len(provs),
            "document_type": cls.document_type,
            "bindingness": binding,
        }

    raw_uri, raw_sha = _save_raw(doc.ext, r.content)
    now = datetime.now(UTC)
    meta = json.dumps({"kind": doc.kind, "url": doc.url, "vestnik": True}, ensure_ascii=False)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _delete_existing(conn, doc.ext)
            doc_id = await conn.fetchval(
                """INSERT INTO source_document
                       (external_id, title, publisher, jurisdiction, legal_hierarchy, document_type,
                        bindingness, language, raw_storage_uri, raw_format, raw_checksum, fetched_at,
                        last_checked_at, valid_from, status, metadata)
                   VALUES ($1,$2,'ČNB','CZ',$3,$4,$5,'cs',$6,'cnb-pdf',$7,$8,$8,$9,'ingested',$10::jsonb)
                   RETURNING id""",
                doc.ext,
                doc.title,
                cls.legal_hierarchy,
                cls.document_type,
                binding,
                raw_uri,
                raw_sha,
                now,
                valid_from,
                meta,
            )
            await conn.executemany(
                """INSERT INTO provision
                       (id, source_document_id, hierarchy_path, ordinal, label, eid, text, text_format,
                        ingestion_quality, text_hash, language, legal_hierarchy, jurisdiction, metadata)
                   VALUES ($1,$2,$3::ltree,$4,$5,$6,$7,'gfm',$8,$9,'cs',$10,'CZ',$11::jsonb)""",
                [
                    (
                        uuid4(),
                        doc_id,
                        p.hierarchy_path,
                        p.ordinal,
                        p.label,
                        p.eid,
                        p.text,
                        score_quality(p.text),
                        hashlib.sha256(p.text.encode()).hexdigest(),
                        cls.legal_hierarchy,
                        json.dumps({"source_eid": p.eid}),
                    )
                    for p in provs
                ],
            )
    return {
        "ext": doc.ext,
        "title": doc.title[:60],
        "n_provisions": len(provs),
        "document_type": cls.document_type,
        "bindingness": binding,
    }


async def backfill_cnb_dates(*, apply: bool = False) -> dict:
    """Recover dates from the stored text of ČNB documents.

    For binding OOP documents the statutory effect date becomes valid_from. For soft-law úřední
    sdělení the application date is stored only as provenance.date_application; valid_from stays
    NULL so soft law is never presented as hard law. Works on the database alone (no network) and is
    idempotent. Documents that take effect on publication ('dnem vyhlášení') or state no date stay
    NULL.
    """
    from pythia.ingest import provision_versioning as pvz

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT sd.id, sd.external_id, sd.bindingness,
                      string_agg(p.text, ' ' ORDER BY p.ordinal) AS txt
                 FROM source_document sd JOIN provision p ON p.source_document_id = sd.id
                WHERE sd.provenance->>'source' = 'cnb'
                GROUP BY sd.id, sd.external_id, sd.bindingness"""
        )
    oop, us = [], []
    for r in rows:
        if r["bindingness"] == "BINDING_OOP":
            oop.append((r["id"], r["external_id"], parse_ucinnost(r["txt"] or "")))
        else:
            us.append((r["id"], r["external_id"], parse_application(r["txt"] or "")))
    if not apply:
        return {
            "mode": "DRY-RUN",
            "oop_valid_from": {e: (str(d) if d else None) for _, e, d in oop},
            "us_date_application": {e: str(a) for _, e, a in us if a},
            "us_left_null": sum(1 for *_, a in us if a is None),
        }
    n_oop = n_us = 0
    async with pool.acquire() as conn:
        for pid, _e, d in oop:
            if d is None:
                continue
            async with conn.transaction():
                await pvz.set_effect_date(conn, pid, d)  # binding: real valid_from everywhere
            n_oop += 1
        for pid, _e, a in us:
            if a is None:
                continue
            async with conn.transaction():
                await pvz.set_application_date(conn, pid, a)  # soft law: date_application only
            n_us += 1
    return {
        "mode": "APPLIED",
        "oop_valid_from_set": n_oop,
        "us_application_set": n_us,
        "us_left_null": sum(1 for *_, a in us if a is None),
    }


async def ingest_sample(*, cap: int = 12, apply: bool = False) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        present = {
            r["external_id"] for r in await conn.fetch("SELECT external_id FROM source_document WHERE publisher='ČNB'")
        }
    n = 0
    async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT, headers={"User-Agent": _UA}) as client:
        # Úřední sdělení (SOFT_LAW) from the Věstník year pages.
        todo: list[tuple[CnbDoc, str]] = []
        for year in _YEARS:
            try:
                r = await client.get(f"{_HOST}/cs/legislativa/vestnik-cnb/vestnik-cnb-rocnik-{year}/")
                r.raise_for_status()
                todo += parse_year_page(r.content, year)
            except Exception as exc:  # noqa: BLE001
                print(f"  ERR year {year}: {repr(exc)[:60]}")
        todo = [t for t in todo if t[0].ext not in present][:cap]
        # OOP (BINDING_OOP) from the curated list.
        oop = [(CnbDoc(e, t, _HOST + u, "oop"), None) for e, t, u in OOP_DOCS if e not in present]
        print(f"=== Věstník ČNB — {'APPLY' if apply else 'DRY-RUN'} · {len(todo)} úřední sdělení + {len(oop)} OOP ===")
        for doc, vf in todo + oop:
            try:
                res = await ingest_doc(client, doc, vf, store=apply)
                n += 1
                print(
                    f"  {('OK ' if apply else 'dry')} {res['ext']:16s} {res['document_type']:14s} "
                    f"{res['bindingness']:20s} prov={res['n_provisions']:>2} · {res['title'][:40]}"
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  ERR {doc.ext}: {repr(exc)[:70]}")
    print(
        f"\n{'ingested' if apply else 'previewed'} {n} documents."
        + ("" if apply else "  (dry-run — re-run with --apply, then the NT passes + resolvers + materialize)")
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Věstník ČNB (soft-law) connector for Nexus.")
    ap.add_argument("--cap", type=int, default=12, help="max úřední sdělení to ingest (default 12)")
    ap.add_argument("--apply", action="store_true", help="write to the DB (default: dry-run)")
    args = ap.parse_args()
    asyncio.run(ingest_sample(cap=args.cap, apply=args.apply))


if __name__ == "__main__":
    main()
