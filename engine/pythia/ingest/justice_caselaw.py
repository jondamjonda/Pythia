r"""Ingest lower-court rulings from the Czech Ministry of Justice open-data portal as CASE_LAW.

A ruling construes the law rather than being law; its statute citations bind it into the graph.
Each ruling that cites an in-corpus act becomes one `source_document` (external_id = its ECLI) plus
one provision per section (výrok, the operative order; odůvodnění, the statement of reasons; právní
věta, the headnote). Its structured citations are stored in the provision metadata for
`link_caselaw` to turn into interprets / applies edges.

Run (from engine/):
  .venv/bin/python -m pythia.ingest.justice_caselaw [--year Y --month M --day D] [--max-docs N]
                                                    [--dry-run | --apply | --down]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import pathlib
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from uuid import uuid4

import httpx

from pythia.db import close_pool, get_pool
from pythia.graph import taxonomy as tx
from pythia.ingest.esbirka import score_quality

_log = logging.getLogger("pythia.ingest.justice_caselaw")

# The Ministry of Justice open-data portal publishes lower-court decisions only, none from apex courts.
_HOST = "https://rozhodnuti.justice.cz"
_UA = "pythia-ingest/0.1 (+https://example.invalid)"
_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_PAGE_SIZE = 100  # maximum page size the API accepts
_PUBLISHER_PREFIX = "Soud:"  # provenance.source_of maps a 'Soud:' publisher to 'court'
_RAW_FORMAT = "justice-finaldoc-json"
_RAW_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "data" / "raw" / "justice_caselaw"

# metadata.type to the Czech noun that opens the title, so tx.classify yields 'rozsudek' / CASE_LAW.
# Nearly all records are JUDGEMENT (Rozsudek); the other nouns also stay on the case-law branch.
_TYPE_PREFIX = {
    "JUDGEMENT": "Rozsudek",
    "RESOLUTION": "Usnesení",
    "USNESENI": "Usnesení",
    "ROZSUDEK": "Rozsudek",
    "PENALTY_ORDER": "Trestní příkaz",
    "PAYMENT_ORDER": "Platební rozkaz",
}

# An act number in a `zminenaUstanoveni` string; 'NUM/YEAR' is exactly source_document.external_id.
_ZU_ACT = re.compile(r"(\d+)/(\d{4})\s*Sb", re.IGNORECASE)


# ── Parsing: the cheap screening strings and the structured citations ─────────────────────
def zu_act_ids(zminena_ustanoveni: list[str] | None) -> set[str]:
    """Return the act external_ids ('NUM/YEAR') named in a record's `zminenaUstanoveni` strings.

    Acts (z. č., zák.), decrees (vyhl. č.) and government regulations (nař. vl. č.) are handled alike,
    since all carry 'NUM/YEAR Sb.'. Used to screen a record for an in-corpus citation before the
    heavier finaldoc fetch.
    """
    out: set[str] = set()
    for s in zminena_ustanoveni or []:
        m = _ZU_ACT.search(s or "")
        if m:
            out.add(f"{int(m.group(1))}/{int(m.group(2))}")
    return out


@dataclass(frozen=True)
class Cite:
    """One structured statute citation from finaldoc.metadata.regulations."""

    act_external_id: str  # 'NUM/YEAR', joins source_document.external_id
    label: str | None  # '§ N', or None for a whole-act citation (empty paragraphNumber)
    lex_type: str  # 'PREDPIS_ZAKON' | 'PREDPIS_VYHLASKA' | … (provenance only)
    section: str  # where the citation was recorded (always 'regulations' here)
    raw_paragraph: str  # the raw paragraphNumber as published ('' for whole-act)

    def key(self) -> str:
        return f"{self.act_external_id}|{self.label or '*'}"


def parse_regulation(reg: dict, section: str = "regulations") -> Cite | None:
    """Convert one finaldoc.metadata.regulations entry to a Cite.

    {lexNumber}/{lexYear} maps directly onto source_document.external_id ('NUM/YEAR') and
    paragraphNumber onto the provision label '§ N'. An empty paragraphNumber (e.g. {lexNumber: 549,
    lexYear: 1991, paragraphNumber: ''}) is a whole-act citation with label None. Returns None when
    lexNumber or lexYear is missing, rather than guessing an act id.
    """
    ln, ly = reg.get("lexNumber"), reg.get("lexYear")
    if ln in (None, "") or ly in (None, ""):
        return None
    try:
        act = f"{int(ln)}/{int(ly)}"
    except (TypeError, ValueError):
        return None
    raw = str(reg.get("paragraphNumber") or "").strip()
    label = f"§ {raw}" if raw else None
    return Cite(
        act_external_id=act, label=label, lex_type=str(reg.get("lexType") or ""), section=section, raw_paragraph=raw
    )


def parse_cites(finaldoc: dict) -> list[Cite]:
    """Return a ruling's structured statute citations, de-duplicated on (act, label).

    The full document (/api/finaldoc/{uuid}, linked from the record's odkaz) has the shape {header,
    verdict, verdictText, justification, justificationText, information, metadata{type: 'JUDGEMENT'
    | ..., ecli, decisionAt, caseNumber, regulations: [{paragraphNumber, lexNumber, lexYear,
    lexType}]}}. metadata.regulations is the structured list of cited statutes; it maps directly onto
    act ids and § labels, which is what lets the linker use a deterministic join rather than NLP.
    """
    md = finaldoc.get("metadata") or {}
    seen: dict[str, Cite] = {}
    for reg in md.get("regulations") or []:
        cite = parse_regulation(reg)
        if cite is not None:
            seen.setdefault(cite.key(), cite)
    return list(seen.values())


# ── Parsing: ruling sections into provision drafts ────────────────────────────
@dataclass
class RulingProvision:
    eid: str  # stable per (ECLI, section) so re-ingest is idempotent
    label: str  # 'Výrok' | 'Odůvodnění' | 'Právní věta'
    ordinal: int
    text: str
    metadata: dict = field(default_factory=dict)


def _section_text(finaldoc: dict, key_text: str, key_struct: str) -> str:
    """Return the flat *Text string, or else flatten the structured block ({texts: [{text}]})."""
    v = finaldoc.get(key_text)
    if isinstance(v, str) and v.strip():
        return v.strip()
    blocks = finaldoc.get(key_struct)
    if isinstance(blocks, list):
        out: list[str] = []
        for b in blocks:
            for t in (b.get("texts") or []) if isinstance(b, dict) else []:
                if isinstance(t, dict) and t.get("text"):
                    out.append(str(t["text"]))
        return "\n".join(out).strip()
    return ""


# Ruling sections: (eid suffix, label, *Text key, structured key, is_reasoning). is_reasoning marks the
# court's own construal (odůvodnění, právní věta), which the linker uses to choose interprets over
# applies; výrok, the operative order, is application rather than construal.
_SECTIONS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("pravni_veta", "Právní věta", "legalSentenceText", "legalSentence", True),  # rarely present
    ("vyrok", "Výrok", "verdictText", "verdict", False),
    ("oduvodneni", "Odůvodnění", "justificationText", "justification", True),
)


def parse_sections(finaldoc: dict, cites: list[Cite]) -> list[RulingProvision]:
    """Split a ruling into section provisions with stable eids.

    The citations are stored in every section's metadata (a ruling cites a § as one act of
    reasoning), tagged with the section's reasoning flag so the linker can pick interprets or
    applies. Empty sections are dropped. Citations live only in this metadata, never as
    cross_reference rows (those are for navigation within an act), so a ruling citation can never
    become both an interprets / applies edge (from the linker) and a generic `references` edge (from
    materialize).
    """
    cite_dicts = [
        {"act": c.act_external_id, "label": c.label, "lex_type": c.lex_type, "raw_paragraph": c.raw_paragraph}
        for c in cites
    ]
    provs: list[RulingProvision] = []
    ordinal = 0
    for suffix, label, ktext, kstruct, is_reasoning in _SECTIONS:
        body = _section_text(finaldoc, ktext, kstruct)
        if not body:
            continue
        provs.append(
            RulingProvision(
                eid=f"ruling_{suffix}",
                label=label,
                ordinal=ordinal,
                text=body,
                metadata={"section": suffix, "is_reasoning": is_reasoning, "cites": cite_dicts},
            )
        )
        ordinal += 1
    return provs


# ── Parsing: document-level fields (the title is built so tx.classify yields CASE_LAW) ──
@dataclass
class RulingDoc:
    ecli: str
    soud: str
    jednaci_cislo: str
    decision_date: date | None
    doc_type: str  # metadata.type (JUDGEMENT, ...)
    title: str
    finaldoc_url: str


def build_title(soud: str, jednaci_cislo: str, doc_type: str) -> str:
    """Build a Czech ruling title, '<Rozsudek | Usnesení | ...> <court> <case number>'.

    The title's cues make tx.classify return 'rozsudek' / CASE_LAW: the court name contains 'soud'
    (court), so the classifier's 'rozsudek' plus 'soud' cue fires, and the leading noun keeps
    resolutions (usnesení) and orders (příkaz) on the case-law branch too.
    """
    prefix = _TYPE_PREFIX.get((doc_type or "").upper(), "Rozhodnutí")
    soud = (soud or "").strip()
    # Every Czech court name contains 'soud', but make sure the classifier sees it.
    parts = [prefix, soud, jednaci_cislo.strip()]
    title = " ".join(p for p in parts if p)
    if "soud" not in title.lower():
        title = f"{title} (soud)"
    return title


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def build_ruling_doc(record: dict, finaldoc: dict) -> RulingDoc:
    """Assemble the document-level fields from the list record and the finaldoc metadata.

    The ECLI comes from the finaldoc metadata when present (canonical), else from the list record.
    """
    md = finaldoc.get("metadata") or {}
    ecli = (md.get("ecli") or record.get("ecli") or "").strip()
    soud = (record.get("soud") or "").strip()
    jc = (record.get("jednaciCislo") or md.get("caseNumber") or "").strip()
    dd = _parse_date(record.get("datumVydani") or md.get("decisionAt"))
    doc_type = md.get("type") or ""
    return RulingDoc(
        ecli=ecli,
        soud=soud,
        jednaci_cislo=jc,
        decision_date=dd,
        doc_type=doc_type,
        title=build_title(soud, jc, doc_type),
        finaldoc_url=record.get("odkaz") or "",
    )


# ── Classification (fail fast: a ruling must classify as CASE_LAW before it is stored) ────────
def classify_ruling(doc: RulingDoc):
    """Classify the constructed title and raise AssertionError unless it is CASE_LAW."""
    cls = tx.classify(
        title=doc.title,
        publisher=_PUBLISHER_PREFIX + doc.soud,
        jurisdiction="CZ",
        valid_from=doc.decision_date,
        valid_to=None,
    )
    if cls.legal_hierarchy != "CASE_LAW":
        raise AssertionError(
            f"ruling {doc.ecli!r} classified {cls.legal_hierarchy!r} (document_type={cls.document_type!r}) "
            f"— expected CASE_LAW; title={doc.title!r}"
        )
    return cls


# ── HTTP ─────────────────────────────────────────────────────────────
async def _get_json(client: httpx.AsyncClient, url: str, params: dict | None = None) -> dict | list:
    r = await client.get(url, params=params)
    r.raise_for_status()
    return r.json()


async def fetch_day_records(client: httpx.AsyncClient, year: int, month: int, day: int):
    """Yield every decision record for a day, paging through the API's 100-record pages.

    The list API is hierarchical: GET /api/opendata gives [{rok, pocet, odkaz}], /api/opendata/{y}
    adds mesic, /api/opendata/{y}/{m} adds den, and /api/opendata/{y}/{m}/{d}?pageNumber=k&pageSize=100
    returns {items: [record, ...], numberOfItems, pageSize, pageNumber, totalPages, totalElements}.
    Record fields: jednaciCislo (case number), soud (court), autor, ecli, predmetRizeni, datumVydani
    (decision date), datumZverejneni, klicovaSlova[], zminenaUstanoveni[] (cited-statute strings such
    as '§ 142 z. č. 99/1963 Sb.') and odkaz (link to the full document).
    """
    page = 0
    while True:
        env = await _get_json(
            client, f"{_HOST}/api/opendata/{year}/{month}/{day}", {"pageNumber": page, "pageSize": _PAGE_SIZE}
        )
        if not isinstance(env, dict):
            return
        items = env.get("items") or []
        for rec in items:
            yield rec
        total_pages = env.get("totalPages") or 0
        page += 1
        if page >= total_pages or not items:
            return


# ── Storage (same pattern as esbirka.ingest_act and cnb_vestnik.ingest_doc) ───────────────
def _save_raw(ecli: str, blob: bytes) -> tuple[str, str]:
    """Save the raw {record, finaldoc} JSON and return (file URI, sha256).

    This gives source_document.raw_storage_uri (NOT NULL) a real target and lets the ruling be
    re-derived later.
    """
    _RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = _RAW_DIR / (re.sub(r"[^\w.-]", "_", ecli) + ".json")
    path.write_bytes(blob)
    return path.as_uri(), hashlib.sha256(blob).hexdigest()


async def _load_corpus_acts(conn) -> set[str]:
    """Return the in-corpus act external_ids, loaded once per run (the graph bound)."""
    return {r["external_id"] for r in await conn.fetch("SELECT external_id FROM source_document")}


async def _delete_existing(conn, ecli: str) -> None:
    await conn.execute(
        "DELETE FROM provision WHERE source_document_id IN "
        "(SELECT id FROM source_document WHERE external_id=$1 AND publisher LIKE $2)",
        ecli,
        _PUBLISHER_PREFIX + "%",
    )
    await conn.execute(
        "DELETE FROM source_document WHERE external_id=$1 AND publisher LIKE $2", ecli, _PUBLISHER_PREFIX + "%"
    )


async def store_ruling(conn, doc: RulingDoc, cls, provs: list[RulingProvision], raw_blob: bytes) -> str:
    """Write one ruling, idempotently on ECLI (delete then insert), and return its source_document id.

    The ECLI is the dedupe key, so re-ingesting a ruling is a no-op. valid_from is the decision date
    (a date object) and valid_to is NULL. The caller runs this inside one transaction per document.
    """
    raw_uri, raw_sha = _save_raw(doc.ecli, raw_blob)
    now = datetime.now(UTC)
    publisher = _PUBLISHER_PREFIX + doc.soud
    meta = json.dumps(
        {
            "finaldoc_url": doc.finaldoc_url,
            "soud": doc.soud,
            "jednaci_cislo": doc.jednaci_cislo,
            "metadata_type": doc.doc_type,
            "court_ruling": True,
        },
        ensure_ascii=False,
    )
    # A ruling speaks from its decision date (valid_from) and is never repealed (valid_to NULL), so it is
    # IN_FORCE once issued. temporal_status is set explicitly: NULL would disagree with the provenance
    # bundle (which turns None into 'UNKNOWN'), mislabel a past-dated ruling as UNKNOWN and fail
    # d1_check's temporal_status/bundle agreement check.
    ts = tx._temporal_status(doc.decision_date, None, date.today())
    await _delete_existing(conn, doc.ecli)
    doc_id = await conn.fetchval(
        """INSERT INTO source_document
               (external_id, title, publisher, jurisdiction, legal_hierarchy, document_type,
                bindingness, language, raw_storage_uri, raw_format, raw_checksum, fetched_at,
                last_checked_at, valid_from, valid_to, status, temporal_status, issuing_authority, metadata)
           VALUES ($1,$2,$3,'CZ',$4,$5,$6,'cs',$7,$8,$9,$10,$10,$11,NULL,'ingested',$14,$12,$13::jsonb)
           RETURNING id""",
        doc.ecli,
        doc.title,
        publisher,
        cls.legal_hierarchy,
        cls.document_type,
        cls.bindingness,
        raw_uri,
        _RAW_FORMAT,
        raw_sha,
        now,
        doc.decision_date,
        doc.soud,
        meta,
        ts,
    )
    await conn.executemany(
        """INSERT INTO provision
               (id, source_document_id, hierarchy_path, ordinal, label, eid, text, text_format,
                ingestion_quality, text_hash, language, legal_hierarchy, jurisdiction, metadata)
           VALUES ($1,$2,$3::ltree,$4,$5,$6,$7,'plain',$8,$9,'cs',$10,'CZ',$11::jsonb)""",
        [
            (
                uuid4(),
                doc_id,
                p.eid,
                p.ordinal,
                p.label,
                p.eid,
                p.text,
                score_quality(p.text),
                hashlib.sha256(p.text.encode("utf-8")).hexdigest(),
                cls.legal_hierarchy,
                json.dumps({"source_eid": p.eid, **p.metadata}, ensure_ascii=False),
            )
            for p in provs
        ],
    )
    return str(doc_id)


# ── Orchestration ────────────────────────────────────────────────────────────
async def ingest_day(*, year: int, month: int, day: int, max_docs: int | None = None, apply: bool = False) -> dict:
    """Walk one day's decisions and keep rulings that cite an in-corpus act.

    The in-corpus act set is loaded once, and a ruling is kept only if it cites one of those acts, so
    every kept ruling anchors onto the corpus. The cheap `zminenaUstanoveni` strings are screened
    before the heavier finaldoc fetch; the finaldoc's structured `regulations` are the citations that
    get stored. A dry run reports what was parsed; with `apply=True` the kept rulings are stored.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        corpus_acts = await _load_corpus_acts(conn)
        present = {
            r["external_id"]
            for r in await conn.fetch(
                "SELECT external_id FROM source_document WHERE publisher LIKE $1", _PUBLISHER_PREFIX + "%"
            )
        }

    stats = {
        "scanned": 0,
        "screened_in": 0,
        "kept": 0,
        "stored": 0,
        "skipped_present": 0,
        "skipped_no_incorpus_cite": 0,
        "skipped_no_sections": 0,
        "errors": 0,
        "samples": [],
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT, headers={"User-Agent": _UA}) as client:
        async for rec in fetch_day_records(client, year, month, day):
            stats["scanned"] += 1
            # Cheap screen before the finaldoc fetch: do the zminenaUstanoveni strings name an in-corpus act?
            if not (zu_act_ids(rec.get("zminenaUstanoveni")) & corpus_acts):
                stats["skipped_no_incorpus_cite"] += 1
                continue
            stats["screened_in"] += 1
            try:
                finaldoc = await _get_json(client, rec["odkaz"])
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                _log.warning("finaldoc_fetch_failed odkaz=%s error=%s", rec.get("odkaz"), repr(exc))
                continue
            cites = parse_cites(finaldoc)
            # Confirm on the structured citations; the free-text strings can be malformed.
            if not ({c.act_external_id for c in cites} & corpus_acts):
                stats["skipped_no_incorpus_cite"] += 1
                continue
            doc = build_ruling_doc(rec, finaldoc)
            if not doc.ecli:
                stats["errors"] += 1
                continue
            try:
                cls = classify_ruling(doc)  # raises unless CASE_LAW
            except AssertionError as exc:
                stats["errors"] += 1
                _log.error("classify_assert_failed: %s", exc)
                continue
            provs = parse_sections(finaldoc, cites)
            if not provs:
                stats["skipped_no_sections"] += 1
                continue
            stats["kept"] += 1
            in_corpus_cites = sorted({c.act_external_id for c in cites if c.act_external_id in corpus_acts})
            if len(stats["samples"]) < 8:
                stats["samples"].append(
                    {
                        "ecli": doc.ecli,
                        "soud": doc.soud,
                        "title": doc.title[:70],
                        "legal_hierarchy": cls.legal_hierarchy,
                        "document_type": cls.document_type,
                        "decision_date": doc.decision_date.isoformat() if doc.decision_date else None,
                        "n_sections": len(provs),
                        "n_cites": len(cites),
                        "in_corpus_acts": in_corpus_cites,
                        "n_whole_act_cites": sum(1 for c in cites if c.label is None),
                    }
                )
            if apply:
                if doc.ecli in present:
                    stats["skipped_present"] += 1
                    # Re-store anyway: delete-then-insert leaves the content unchanged.
                raw_blob = json.dumps({"record": rec, "finaldoc": finaldoc}, ensure_ascii=False).encode("utf-8")
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        await store_ruling(conn, doc, cls, provs, raw_blob)
                stats["stored"] += 1
                present.add(doc.ecli)
            if max_docs is not None and stats["kept"] >= max_docs:
                break
    return stats


async def ingest_range(*, start: date, end: date, max_total: int | None = None, apply: bool = False) -> dict:
    """Walk an inclusive date range, keeping rulings until `max_total` is reached.

    Each day is the idempotent per-day walk (deduplicated on ECLI), so re-running a range is safe;
    empty days return quickly.
    """
    from datetime import timedelta

    agg = {
        "days": 0,
        "scanned": 0,
        "screened_in": 0,
        "kept": 0,
        "stored": 0,
        "skipped_no_incorpus_cite": 0,
        "skipped_no_sections": 0,
        "errors": 0,
        "samples": [],
    }
    d = start
    while d <= end:
        if max_total is not None and agg["kept"] >= max_total:
            break
        remaining = None if max_total is None else max_total - agg["kept"]
        s = await ingest_day(year=d.year, month=d.month, day=d.day, max_docs=remaining, apply=apply)
        agg["days"] += 1
        for k in (
            "scanned",
            "screened_in",
            "kept",
            "stored",
            "skipped_no_incorpus_cite",
            "skipped_no_sections",
            "errors",
        ):
            agg[k] += s.get(k, 0)
        if len(agg["samples"]) < 8:
            agg["samples"].extend(s["samples"][: 8 - len(agg["samples"])])
        d += timedelta(days=1)
    return agg


async def down() -> dict:
    """Delete every court ruling this connector ingested (publisher 'Soud:%') and its provisions."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            n_prov = await conn.fetchval(
                """WITH d AS (DELETE FROM provision WHERE source_document_id IN
                       (SELECT id FROM source_document WHERE publisher LIKE $1) RETURNING 1)
                   SELECT count(*) FROM d""",
                _PUBLISHER_PREFIX + "%",
            )
            n_doc = await conn.fetchval(
                """WITH d AS (DELETE FROM source_document WHERE publisher LIKE $1 RETURNING 1)
                   SELECT count(*) FROM d""",
                _PUBLISHER_PREFIX + "%",
            )
    return {"deleted_documents": n_doc, "deleted_provisions": n_prov}


def _print_stats(s: dict, *, apply: bool) -> None:
    tag = "APPLIED" if apply else "DRY-RUN (nothing written)"
    print(f"=== justice case-law connector — {tag} ===")
    print(f"  scanned records            : {s['scanned']}")
    print(f"  screened-in (cheap cite)   : {s['screened_in']}")
    print(f"  KEPT (in-corpus-citing)    : {s['kept']}")
    if apply:
        print(f"  stored                     : {s['stored']}")
    print(f"  skipped (no in-corpus cite): {s['skipped_no_incorpus_cite']}")
    print(f"  skipped (no sections)      : {s['skipped_no_sections']}")
    print(f"  errors                     : {s['errors']}")
    for smp in s["samples"]:
        print(
            f"    · {smp['ecli']}  [{smp['legal_hierarchy']}/{smp['document_type']}]  "
            f"{smp['decision_date']}  sect={smp['n_sections']} cites={smp['n_cites']} "
            f"in-corpus={smp['in_corpus_acts']} whole-act={smp['n_whole_act_cites']}"
        )


def _main() -> None:
    ap = argparse.ArgumentParser(description="MoJ justice-portal case-law connector for Pythia.")
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--month", type=int, default=3)
    ap.add_argument("--day", type=int, default=15)
    ap.add_argument("--max-docs", type=int, default=None, help="cap kept rulings (default: no cap)")
    ap.add_argument("--dry-run", action="store_true", help="preview only, write nothing (the default)")
    ap.add_argument("--apply", action="store_true", help="write to the DB (default: dry-run)")
    ap.add_argument("--down", action="store_true", help="reverse: delete all ingested court rulings")
    args = ap.parse_args()

    async def run():
        if args.down:
            res = await down()
            print(f"⏪ removed {res['deleted_documents']} rulings + {res['deleted_provisions']} provisions")
        else:
            s = await ingest_day(
                year=args.year, month=args.month, day=args.day, max_docs=args.max_docs, apply=args.apply
            )
            _print_stats(s, apply=args.apply)
        await close_pool()

    asyncio.run(run())


if __name__ == "__main__":
    _main()
