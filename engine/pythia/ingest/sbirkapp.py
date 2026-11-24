r"""Connector for the Sbírka právních předpisů ÚSC: Czech regional and municipal law.

sbirkapp.gov.cz is the central electronic collection, run by the Ministry of the Interior since
2022, of regional decrees (nařízení kraje) and municipal legislation (obecně závazné vyhlášky, OZV,
and nařízení obce). It offers no API or open-data dump, only server-rendered HTML plus each
document's original office file, so this connector scrapes the listings (``list_issuer``) and
segments the files into provisions (``ingest_doc``).

Run (from engine/): .venv/bin/python -m pythia.ingest.sbirkapp [--cap N] [--apply]
  then the taxonomy passes, the cross-reference resolvers and graph materialization to wire the
  new documents into the graph.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import logging
import re
import pathlib
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import uuid4

import httpx
from lxml import etree
from lxml import html as lhtml

from pythia.db import get_pool
from pythia.graph import taxonomy as tx
from pythia.ingest.esbirka import ProvisionDraft, _parse_date as _parse_iso_date, score_quality

_log = logging.getLogger("pythia.ingest.sbirkapp")

_BASE = "https://sbirkapp.gov.cz"
_UA = "pythia-ingest/0.1 (Nexus legal-graph; +https://example.invalid)"
_TIMEOUT = httpx.Timeout(45.0, connect=15.0)

# The listing's 'Datum nabytí účinnosti' (effective date) cell is a Czech 'DD.MM.YYYY' date; the
# registry already resolves formulas such as 'dnem vyhlášení' (on the day of promulgation) to a date.
# esbirka._parse_date accepts only ISO dates, so Czech dates are parsed here with ISO as a fallback.
# Nothing is guessed: an unparseable value becomes None (an unknown effect date).
_CZ_DATE = re.compile(r"^\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\s*$")


def _parse_cz_date(s: str | None) -> date | None:
    if not s:
        return None
    m = _CZ_DATE.match(s.strip())
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return _parse_iso_date(s)


_RAW_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "data" / "raw" / "sbirkapp"

_ART = re.compile(r"^\s*(Čl\.\s*\d+[a-z]?|Článek\s*\d+[a-z]?|§\s*\d+[a-z]?)\b", re.IGNORECASE)


@dataclass(frozen=True)
class UscIssuer:
    kod: str  # the issuer's Data Box ID (datová schránka), used as the ovm= filter key
    name: str  # publisher name; regions contain "kraj", which drives REGIONAL vs MUNICIPAL
    scope: str  # expected 'MUNICIPAL' or 'REGIONAL'; the classifier re-derives it from the title


# A bounded issuer list: the six largest cities and four regions (Data Box IDs verified by hand).
# Full coverage would be roughly 77k documents across 6,250 municipalities.
ISSUERS: tuple[UscIssuer, ...] = (
    UscIssuer("48ia97h", "Hlavní město Praha", "MUNICIPAL"),
    UscIssuer("a7kbrrn", "Statutární město Brno", "MUNICIPAL"),
    UscIssuer("5zubv7w", "Statutární město Ostrava", "MUNICIPAL"),
    UscIssuer("6iybfxn", "Statutární město Plzeň", "MUNICIPAL"),
    UscIssuer("7c6by6u", "Statutární město Liberec", "MUNICIPAL"),
    UscIssuer("kazbzri", "Statutární město Olomouc", "MUNICIPAL"),
    UscIssuer("x2pbqzq", "Jihomoravský kraj", "REGIONAL"),
    UscIssuer("8x6bxsd", "Moravskoslezský kraj", "REGIONAL"),
    UscIssuer("c5kbvkw", "Liberecký kraj", "REGIONAL"),
    UscIssuer("zzjbr3p", "Plzeňský kraj", "REGIONAL"),
)


def _txt(el) -> str:
    return re.sub(r"\s+", " ", el.text_content()).strip() if el is not None else ""


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


# ── listing ────────────────────────────────────────────────────────────────
def parse_list(html_bytes: bytes) -> list[dict]:
    """Parse one result page into dicts with keys spp, druh, cislo, nazev, platnost, ucinnost.

    Each row carries the SPP id (/detail/SPP…), type (Druh), number (Číslo), title (Název), validity
    (Platnost) and effect date (účinnost).
    """
    root = lhtml.fromstring(html_bytes, parser=lhtml.HTMLParser(encoding="utf-8"))
    out, seen = [], set()
    for a in root.xpath('//a[contains(@href, "/detail/SPP")]'):
        m = re.search(r"/detail/(SPP[A-Z0-9]+)", a.get("href") or "")
        if not m or m.group(1) in seen:
            continue
        spp, row = m.group(1), a
        for _ in range(8):
            row = row.getparent()
            if row is None:
                break
            cells = {c.get("data-title"): _txt(c) for c in row.xpath(".//*[@data-title]")}
            if "Druh právního předpisu" in cells:
                seen.add(spp)
                out.append(
                    {
                        "spp": spp,
                        "druh": cells.get("Druh právního předpisu", ""),
                        "cislo": cells.get("Číslo právního předpisu", ""),
                        "nazev": cells.get("Název právního předpisu", ""),
                        "platnost": cells.get("Platnost", ""),
                        "ucinnost": cells.get("Datum nabytí účinnosti", ""),
                    }
                )
                break
    return out


async def list_issuer(client: httpx.AsyncClient, issuer: UscIssuer, *, cap: int) -> list[dict]:
    """Return one issuer's in-force documents, newest first, capped at `cap`.

    Pages through GET /vyhledavani/vysledek?hlavni_typ=pp&ovm={kod}&page=N, an HTML table of 50
    rows per page; a shorter page is the last one.
    """
    docs: list[dict] = []
    for page in range(1, 30):
        r = await client.get(
            f"{_BASE}/vyhledavani/vysledek", params={"hlavni_typ": "pp", "ovm": issuer.kod, "page": page}
        )
        r.raise_for_status()
        rows = parse_list(r.content)
        if not rows:
            break
        for d in rows:
            if "platn" in d["platnost"].lower() and "vyřazeno" not in d["druh"].lower():
                docs.append(d)
                if len(docs) >= cap:
                    return docs
        if len(rows) < 50:
            break
    return docs


# ── body parsing (DOCX, ODT, PDF) ───────────────────────────────────────────
def _zip_lines(z: zipfile.ZipFile, member: str, *, docx: bool) -> list[str]:
    root = etree.fromstring(z.read(member))
    if docx:  # one line per <w:p>, joining its <w:t> runs
        lines = ["".join(t.text or "" for t in p.iter("{*}t")) for p in root.iter("{*}p")]
    else:  # ODT: text:p
        lines = ["".join(p.itertext()) for p in root.iter("{*}p")]
    return [_norm(ln) for ln in lines if _norm(ln)]


def parse_body(data: bytes) -> tuple[str, list[str]]:
    """Detect the format by magic bytes and return (format, paragraph lines).

    Publishers upload whatever office format they use, so the format cannot be assumed.
    """
    if data[:4] == b"%PDF":
        import pypdf

        text = "\n".join(pg.extract_text() or "" for pg in pypdf.PdfReader(io.BytesIO(data)).pages)
        return "pdf", [_norm(ln) for ln in text.split("\n") if _norm(ln)]
    if data[:2] == b"PK":  # a zip container: DOCX or ODT
        z = zipfile.ZipFile(io.BytesIO(data))
        names = set(z.namelist())
        if "word/document.xml" in names:
            return "docx", _zip_lines(z, "word/document.xml", docx=True)
        if "content.xml" in names:
            return "odt", _zip_lines(z, "content.xml", docx=False)
    raise RuntimeError(f"unrecognized body format (first bytes {data[:8]!r})")


# Reference cues: a line starting "§ N" followed by one of these is a citation, not an article header.
_REF_TAIL = re.compile(r"\b(odst|písm|pododst|bod|Sb\.|zákon|zákoník|vyhlášk|nařízení|směrnic|čl\.)", re.IGNORECASE)


def _eid(label: str) -> str:
    m = re.search(r"(\d+[a-z]?)", label)
    n = m.group(1) if m else "1"
    return ("par_" if "§" in label else "cl_") + n


def _article_header(ln: str) -> str | None:
    """Return the article label if `ln` is a real article header, else None.

    A header is the marker alone or the marker plus a short heading. A line that continues a citation
    ('§ 2 odst. 5 a 6 … vyhlášky č. 246/2001 Sb.') is rejected; it would otherwise create a duplicate
    § N provision and break the (document, label) key that version history relies on.
    """
    m = _ART.match(ln)
    if not m:
        return None
    tail = ln[m.end() :].strip(" .,–-")
    if tail and (len(tail) > 60 or _REF_TAIL.search(tail)):
        return None
    return _norm(m.group(1))


def _unique_eids(provs: list[ProvisionDraft]) -> list[ProvisionDraft]:
    """Make (document, label), the version-history identity key, unique within a document.

    If an eid still repeats (an edge case the header check missed), later occurrences get a numeric
    suffix instead of colliding.
    """
    seen: dict[str, int] = {}
    out = []
    for p in provs:
        if p.eid in seen:
            seen[p.eid] += 1
            s = seen[p.eid]
            out.append(
                ProvisionDraft(
                    f"{p.eid}_{s}", f"{p.label} ({s})", f"{p.hierarchy_path}_{s}", p.ordinal, p.text, p.parent_eid
                )
            )
        else:
            seen[p.eid] = 1
            out.append(p)
    return out


def segment(lines: list[str]) -> list[ProvisionDraft]:
    """Split the lines into provisions on the Čl. / § article skeleton, preamble first."""
    provs: list[ProvisionDraft] = []
    pre: list[str] = []
    label = eid = None
    buf: list[str] = []

    def flush():
        if label and buf:
            provs.append(
                ProvisionDraft(eid=eid, label=label, hierarchy_path=eid, ordinal=len(provs), text="\n".join(buf))
            )

    for ln in lines:
        hdr = _article_header(ln)
        if hdr:
            flush()
            label, eid, buf = hdr, _eid(hdr), [ln]
        elif label:
            buf.append(ln)
        else:
            pre.append(ln)
    flush()
    if not provs:  # no article markers: the whole document is one provision
        return [ProvisionDraft("text", "Text", "text", 0, "\n".join(pre or lines))]
    if pre:  # a preamble block precedes the articles
        provs.insert(
            0,
            ProvisionDraft(
                eid="preambule", label="Preambule", hierarchy_path="preambule", ordinal=0, text="\n".join(pre)
            ),
        )
    provs = _unique_eids(provs)
    return [
        ProvisionDraft(p.eid, p.label, p.hierarchy_path, i, p.text, p.parent_eid) for i, p in enumerate(provs)
    ]  # renumber so ordinals are contiguous


# ── store (mirrors esbirka.ingest_act) ──────────────────────────────────────
def _save_raw(spp: str, blob: bytes, ext: str) -> tuple[str, str]:
    _RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = _RAW_DIR / f"{spp}.{ext}"
    path.write_bytes(blob)
    return path.as_uri(), hashlib.sha256(blob).hexdigest()


async def _delete_existing(conn, spp: str) -> None:
    await conn.execute(
        "DELETE FROM provision WHERE source_document_id IN "
        "(SELECT id FROM source_document WHERE external_id=$1 AND publisher LIKE 'ÚSC:%')",
        spp,
    )
    await conn.execute("DELETE FROM source_document WHERE external_id=$1 AND publisher LIKE 'ÚSC:%'", spp)


async def ingest_doc(client: httpx.AsyncClient, issuer: UscIssuer, meta: dict, *, store: bool = True) -> dict:
    """Fetch one document and store it with its provisions, or only preview it if `store` is False.

    GET /detail/{SPPID}/text returns the publisher's original upload, a born-digital DOCX, ODT or PDF
    (no OCR needed), which is segmented on the "Čl. N" / "§ N" skeleton. A stored document replaces
    any earlier copy with the same SPP id.
    """
    r = await client.get(f"{_BASE}/detail/{meta['spp']}/text")
    r.raise_for_status()
    fmt, lines = parse_body(r.content)
    provs = segment(lines)
    title = _norm(
        f"{meta['druh']} {issuer.name} č. {meta['cislo']}"
        + (f", {meta['nazev']}" if meta["nazev"] and meta["nazev"] != meta["druh"] else "")
    )
    if not store:
        return {"spp": meta["spp"], "title": title[:70], "fmt": fmt, "n_provisions": len(provs)}

    cls = tx.classify(title=title, publisher=issuer.name, jurisdiction="CZ")
    raw_uri, raw_sha = _save_raw(meta["spp"], r.content, fmt)
    now = datetime.now(UTC)
    doc_meta = json.dumps(
        {"issuer": issuer.name, "kod": issuer.kod, "cislo": meta["cislo"], "druh": meta["druh"], "format": fmt},
        ensure_ascii=False,
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _delete_existing(conn, meta["spp"])
            doc_id = await conn.fetchval(
                """INSERT INTO source_document
                       (external_id, title, publisher, jurisdiction, legal_hierarchy, document_type,
                        language, raw_storage_uri, raw_format, raw_checksum, fetched_at, last_checked_at,
                        valid_from, status, metadata)
                   VALUES ($1,$2,$3,'CZ',$4,$5,'cs',$6,$7,$8,$9,$9,$10,'ingested',$11::jsonb)
                   RETURNING id""",
                meta["spp"],
                title,
                f"ÚSC:{issuer.name}",
                cls.legal_hierarchy,
                cls.document_type,
                raw_uri,
                f"sbirkapp-{fmt}",
                raw_sha,
                now,
                _parse_cz_date(meta.get("ucinnost")),
                doc_meta,
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
        "spp": meta["spp"],
        "title": title[:70],
        "fmt": fmt,
        "n_provisions": len(provs),
        "document_type": cls.document_type,
        "territorial": cls.territorial_scope,
    }


async def backfill_usc_dates(*, apply: bool = False) -> dict:
    """Fill the effect date (účinnost, stored as valid_from) of stored ÚSC documents that lack one.

    Re-crawls each issuer's listing, because the SPP detail page is a JavaScript shell with no date,
    maps each stored SPP id to its 'Datum nabytí účinnosti' cell and parses the Czech date. The date
    goes through ``provision_versioning.set_effect_date``, which updates the column, the provenance
    bundle, the provisions and the version head and recomputes temporal_status (so future-dated
    regulations become NOT_YET_EFFECTIVE). Idempotent; an unmatched or unparseable row stays NULL.
    """
    from pythia.ingest import provision_versioning as pvz

    pool = await get_pool()
    async with pool.acquire() as conn:
        stored = await conn.fetch(
            "SELECT id, external_id, metadata->>'kod' AS kod FROM source_document WHERE publisher LIKE 'ÚSC:%'"
        )
    spp_date: dict[str, str] = {}
    async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT, headers={"User-Agent": _UA}) as client:
        for kod in sorted({s["kod"] for s in stored if s["kod"]}):
            for page in range(1, 30):
                r = await client.get(
                    f"{_BASE}/vyhledavani/vysledek", params={"hlavni_typ": "pp", "ovm": kod, "page": page}
                )
                r.raise_for_status()
                rows = parse_list(r.content)
                if not rows:
                    break
                for d in rows:
                    spp_date[d["spp"]] = d["ucinnost"]
                if len(rows) < 50:
                    break
    plan = [(s["id"], s["external_id"], _parse_cz_date(spp_date.get(s["external_id"]))) for s in stored]
    matched = sum(1 for *_, d in plan if d is not None)
    sample = {ext: (d.isoformat() if d else None) for _, ext, d in plan[:6]}
    if not apply:
        return {
            "mode": "DRY-RUN",
            "docs": len(plan),
            "matched": matched,
            "unmatched": len(plan) - matched,
            "sample": sample,
        }
    fixed = 0
    async with pool.acquire() as conn:
        for pid, _ext, d in plan:
            if d is None:
                continue
            async with conn.transaction():
                await pvz.set_effect_date(conn, pid, d)
            fixed += 1
    return {"mode": "APPLIED", "docs": len(plan), "fixed": fixed, "left_null": len(plan) - fixed, "sample": sample}


async def ingest_sample(*, cap: int = 6, apply: bool = False) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        present = {
            r["external_id"]
            for r in await conn.fetch("SELECT external_id FROM source_document WHERE publisher LIKE 'ÚSC:%'")
        }
    n_ok = 0
    async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT, headers={"User-Agent": _UA}) as client:
        for issuer in ISSUERS:
            try:
                docs = [d for d in await list_issuer(client, issuer, cap=cap) if d["spp"] not in present]
            except Exception as exc:  # noqa: BLE001
                print(f"  ERR list {issuer.name}: {repr(exc)[:60]}")
                continue
            print(f"\n{issuer.name} ({issuer.scope}) — {len(docs)} in-force docs to ingest")
            for d in docs:
                try:
                    r = await ingest_doc(client, issuer, d, store=apply)
                    n_ok += 1
                    tag = f"{r.get('document_type', '?')}/{r.get('territorial', '?')}" if apply else "dry"
                    print(
                        f"  {('OK ' if apply else 'dry')} {d['spp']:18s} {tag:22s} prov={r['n_provisions']:>3} {r['fmt']} · {r['title'][:42]}"
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"  ERR {d['spp']}: {repr(exc)[:60]}")
    print(
        f"\n{'ingested' if apply else 'previewed'} {n_ok} documents."
        + ("" if apply else "  (dry-run — re-run with --apply, then the NT passes + resolvers + materialize)")
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Sbírka PP ÚSC (regional/municipal) connector for Nexus.")
    ap.add_argument("--cap", type=int, default=6, help="max in-force docs per issuer (default 6)")
    ap.add_argument("--apply", action="store_true", help="write to the DB (default: dry-run)")
    args = ap.parse_args()
    asyncio.run(ingest_sample(cap=args.cap, apply=args.apply))


if __name__ == "__main__":
    main()
