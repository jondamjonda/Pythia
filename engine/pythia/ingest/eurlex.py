r"""Ingest EU secondary law from EUR-Lex in the Formex-4 XML format.

Fetches an act's Formex-4 enacting terms by CELEX number from CELLAR (the EU Publications Office
repository), parses the <ACT> / <ARTICLE> / <PARAG> tree into the provision model used for all EU
rows (eid `art_N` / `art_N__para_M`, label `Article N` / `Article N(M)`) and stores it like
`esbirka.ingest_act`: full replace, raw XML archived, dry run available. `transposition` builds on
these rows to link directives to the Czech acts that transpose them.

Run (from engine/):
      .venv/bin/python -m pythia.ingest.eurlex --verify 32023R1114      # compare parsed counts with stored rows
      .venv/bin/python -m pythia.ingest.eurlex --celex 32013L0036       # dry run for one act (no write)
      .venv/bin/python -m pythia.ingest.eurlex --celex 32013L0036 --apply
      .venv/bin/python -m pythia.ingest.eurlex [--apply]                # the EU_FINANCIAL registry (missing acts only)
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
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import httpx
from lxml import etree

from pythia.db import get_pool
from pythia.ingest.esbirka import CrossRefDraft, ProvisionDraft, score_quality

_log = logging.getLogger("pythia.ingest.eurlex")


# ---------------------------------------------------------------------------
# EU_FINANCIAL registry: the EU acts the Czech corpus transposes or cites most (chosen by citation
# counts). A few directives already in the corpus are not listed but can still be fetched with --celex.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EuActSpec:
    celex: str
    short: str
    title: str
    document_type: str  # 'eu_smernice' (directive) or 'eu_narizeni' (regulation)


EU_FINANCIAL: tuple[EuActSpec, ...] = (
    EuActSpec(
        "32013L0036", "CRD IV", "Directive 2013/36/EU on access to the activity of credit institutions", "eu_smernice"
    ),
    EuActSpec(
        "32013R0575",
        "CRR",
        "Regulation (EU) No 575/2013 on prudential requirements for credit institutions",
        "eu_narizeni",
    ),
    EuActSpec(
        "32014L0059",
        "BRRD",
        "Directive 2014/59/EU establishing a framework for the recovery and resolution of credit institutions",
        "eu_smernice",
    ),
    EuActSpec(
        "32009L0138",
        "Solvency II",
        "Directive 2009/138/EC on the taking-up and pursuit of the business of Insurance and Reinsurance",
        "eu_smernice",
    ),
    EuActSpec(
        "32015L2366", "PSD2", "Directive (EU) 2015/2366 on payment services in the internal market", "eu_smernice"
    ),
    EuActSpec(
        "32012R0648",
        "EMIR",
        "Regulation (EU) No 648/2012 on OTC derivatives, central counterparties and trade repositories",
        "eu_narizeni",
    ),
    EuActSpec(
        "32014L0092",
        "PAD",
        "Directive 2014/92/EU on the comparability of fees related to payment accounts",
        "eu_smernice",
    ),
    EuActSpec(
        "32014R0909",
        "CSDR",
        "Regulation (EU) No 909/2014 on improving securities settlement and central securities depositories",
        "eu_narizeni",
    ),
    # Capital-markets, AML and digital-resilience acts cited by the Czech corpus.
    EuActSpec(
        "32014R0596", "MAR", "Regulation (EU) No 596/2014 on market abuse (market abuse regulation)", "eu_narizeni"
    ),
    EuActSpec(
        "32017R1129",
        "Prospectus",
        "Regulation (EU) 2017/1129 on the prospectus to be published when securities are offered to the public or admitted to trading",
        "eu_narizeni",
    ),
    EuActSpec(
        "32009L0065",
        "UCITS",
        "Directive 2009/65/EC on undertakings for collective investment in transferable securities (UCITS)",
        "eu_smernice",
    ),
    EuActSpec(
        "32022R2554",
        "DORA",
        "Regulation (EU) 2022/2554 on digital operational resilience for the financial sector",
        "eu_narizeni",
    ),
    EuActSpec("32006L0112", "VAT Dir", "Directive 2006/112/EC on the common system of value added tax", "eu_smernice"),
    EuActSpec(
        "32016R1011",
        "BMR",
        "Regulation (EU) 2016/1011 on indices used as benchmarks in financial instruments and financial contracts",
        "eu_narizeni",
    ),
    EuActSpec(
        "32019R2088",
        "SFDR",
        "Regulation (EU) 2019/2088 on sustainability-related disclosures in the financial services sector",
        "eu_narizeni",
    ),
    EuActSpec(
        "32024R1624",
        "AMLR",
        "Regulation (EU) 2024/1624 on the prevention of the use of the financial system for the purposes of money laundering or terrorist financing",
        "eu_narizeni",
    ),
    # The European Supervisory Authorities, macro-prudential oversight and credit rating agencies. These
    # were the most-cited unresolved EU targets, cited by name across CRD, CRR, PSD2, MiFID and Solvency II
    # ("Regulation (EU) No 1093/2010"): the EBA regulation by 259 provisions, ESMA by 95.
    EuActSpec(
        "32010R1093",
        "EBA",
        "Regulation (EU) No 1093/2010 establishing a European Supervisory Authority (European Banking Authority)",
        "eu_narizeni",
    ),
    EuActSpec(
        "32010R1095",
        "ESMA",
        "Regulation (EU) No 1095/2010 establishing a European Supervisory Authority (European Securities and Markets Authority)",
        "eu_narizeni",
    ),
    EuActSpec(
        "32010R1094",
        "EIOPA",
        "Regulation (EU) No 1094/2010 establishing a European Supervisory Authority (European Insurance and Occupational Pensions Authority)",
        "eu_narizeni",
    ),
    EuActSpec(
        "32010R1092",
        "ESRB",
        "Regulation (EU) No 1092/2010 on European Union macro-prudential oversight of the financial system and establishing a European Systemic Risk Board",
        "eu_narizeni",
    ),
    EuActSpec("32009R1060", "CRA", "Regulation (EC) No 1060/2009 on credit rating agencies", "eu_narizeni"),
)

_CELLAR_CELEX = "http://publications.europa.eu/resource/celex/"
_USER_AGENT = "pythia-ingest/0.1 (Nexus legal-graph; +https://example.invalid)"
_TIMEOUT = httpx.Timeout(60.0, connect=15.0)

# Validated entry-into-force (valid_from) and end-of-validity (valid_to) dates for the EU corpus.
# Dates come from the CELLAR RDF notice (the act-level {EV} entry-into-force derived from {DATPUB},
# excluding {MA/PART} partial-article blocks) and were cross-checked against the "Date of entry into
# force" / "Date of end of validity" fields that EUR-Lex displays. This curated table is the source of
# truth for stored data. It avoids the wrong dates a naive first-block parser picks (MiFIR is 2014-07-02,
# not 2019-01-03; CSDR is 2014-09-17, not 2015-01-01), uses entry into force rather than the application
# date (e.g. GDPR, MiCA), and carries AMLD4's repeal date (2027-07-09), which the RDF leaves ambiguous.
# fetch_eu_dates() is the best-effort live parser for CELEX numbers not listed here.
# Format: CELEX -> (valid_from_iso, valid_to_iso or None).
_EU_DATES: dict[str, tuple[str, str | None]] = {
    "32009L0138": ("2010-01-06", None),  # Solvency II
    "32011L0061": ("2011-07-21", None),  # AIFMD
    "32012R0648": ("2012-08-16", None),  # EMIR
    "32013L0036": ("2013-07-17", None),  # CRD IV
    "32013R0575": ("2013-06-28", None),  # CRR
    "32014L0059": ("2014-07-02", None),  # BRRD
    "32014L0065": ("2014-07-02", None),  # MiFID II
    "32014L0092": ("2014-09-17", None),  # Payment Accounts Directive
    "32014R0600": ("2014-07-02", None),  # MiFIR  (not 2019-01-03)
    "32014R0909": ("2014-09-17", None),  # CSDR   (not 2015-01-01)
    "32015L0849": ("2015-06-25", "2027-07-09"),  # AMLD4 (repealed by AMLD6 with effect from 2027-07-09)
    "32015L2366": ("2016-01-12", None),  # PSD2
    "32016R0679": ("2016-05-24", None),  # GDPR (entry into force, not the 2018-05-25 application date)
    "32023R1114": ("2023-06-29", None),  # MiCA (entry into force, not the 2024 application date)
    # Entry into force, not the later application date, derived from each act's final-article in-force
    # clause and its Official Journal publication date. Reconfirm new values against the "Date of entry
    # into force" field on EUR-Lex before applying; fetch_eu_dates() is the conservative live fallback.
    "32014R0596": ("2014-07-02", None),  # MAR        — OJ L173 12.6.2014; Art.39 20th day. (Apply 3.7.2016.)
    "32017R1129": ("2017-07-20", None),  # Prospectus — OJ L168 30.6.2017; Art.49 20th day. (Apply 21.7.2019.)
    "32009L0065": ("2009-12-07", None),  # UCITS IV   — OJ L302 17.11.2009; Art.118 20th day (not 2010-01).
    "32022R2554": ("2023-01-16", None),  # DORA       — OJ L333 27.12.2022; Art.64 20th day. (Apply 17.1.2025.)
    "32006L0112": ("2007-01-01", None),  # VAT Dir.   — OJ L347 11.12.2006; Art.413 sets 1.1.2007 (not 20th day).
    "32016R1011": ("2016-06-30", None),  # BMR        — OJ L171 29.6.2016; Art.59 day after publication (not 20th day).
    "32019R2088": ("2019-12-29", None),  # SFDR       — OJ L317 9.12.2019; Art.20 20th day. (Apply 10.3.2021.)
    "32024R1624": (
        "2024-07-09",
        None,
    ),  # AMLR       — OJ L 2024/1624 19.6.2024; Art.86 20th day. (Most apply 10.7.2027.)
    # Required: for these acts fetch_eu_dates() returns (None, None) because their small RDF notice has no
    # in-force axiom, so without these rows they would store NULL dates and drop out of the current-law mask.
    "32010R1093": ("2010-12-16", None),  # EBA   — OJ L331 15.12.2010; Art.82 day-after-publication.
    "32010R1095": ("2010-12-16", None),  # ESMA  — OJ L331 15.12.2010; Art.82 day-after-publication.
    "32010R1094": ("2010-12-16", None),  # EIOPA — OJ L331 15.12.2010; Art.82 day-after-publication.
    "32010R1092": ("2010-12-16", None),  # ESRB  — OJ L331 15.12.2010; Art.20 day-after-publication.
    "32009R1060": ("2009-12-07", None),  # CRA   — OJ L302 17.11.2009; Art.41 20th-day-after-publication.
    # Acts first ingested before they had a row here, which left their valid_from NULL. Entry into force,
    # not the later application date. Adding a row does not change stored data by itself: a reindex or a
    # per-act re-stamp is needed to backfill valid_from.
    "32024R1689": (
        "2024-08-01",
        None,
    ),  # EU AI Act — OJ L 2024/1689 12.7.2024; Art.113 20th day. (Phased application from 2025-02-02.)
    "32018R0848": (
        "2018-06-17",
        None,
    ),  # Organic Production Reg. — OJ L150 14.6.2018; Art.61 20th day. (Apply 1.1.2022.)
    "32021R2115": (
        "2021-12-07",
        None,
    ),  # CAP Strategic Plans Reg. — OJ L435 6.12.2021; Art.156 day-after-publication. (Apply 1.1.2023.)
    "32022R2065": (
        "2022-11-16",
        None,
    ),  # Digital Services Act — OJ L277 27.10.2022; Art.93 20th day. (General application 17.2.2024.)
    "32011R1169": (
        "2011-12-12",
        None,
    ),  # Food Information to Consumers Reg. — OJ L304 22.11.2011; Art.55 20th day. (Apply 13.12.2014.)
    "32008R1272": (
        "2009-01-20",
        None,
    ),  # CLP Reg. (classification/labelling) — OJ L353 31.12.2008; Art.62 20th day. (Phased application from 2010-12-01.)
}

# CELLAR RDF notice namespaces, bound by URI because the j.N prefixes are assigned per document.
_RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
_OWL = "{http://www.w3.org/2002/07/owl#}"
_ANN = "{http://publications.europa.eu/ontology/annotation#}"
_CDM = "http://publications.europa.eu/ontology/cdm#"
_EIF_PROP = _CDM + "resource_legal_date_entry-into-force"
_EOV_PROP = _CDM + "resource_legal_date_end-of-validity"


def _axioms(root, prop: str):
    """Yield (target_date, type_of_date, comment_on_date) for every owl:Axiom annotating `prop`.

    The date is a literal in owl:annotatedTarget's text; type_of_date and comment_on_date carry codes
    such as '{EV|...}' and '{MA/PART|...}'.
    """
    for d in root.iterchildren(_RDF + "Description"):
        p = d.find(_OWL + "annotatedProperty")
        if p is None or p.get(_RDF + "resource") != prop:
            continue
        tgt = d.find(_OWL + "annotatedTarget")
        tod = d.find(_ANN + "type_of_date")
        cod = d.find(_ANN + "comment_on_date")
        yield (
            tgt.text if tgt is not None else None,
            tod.text if tod is not None else "",
            cod.text if cod is not None else "",
        )


def _select_eif(axioms) -> date | None:
    """Pick the act-level entry-into-force date from the axioms.

    Drops partial-article blocks ({MA/PART}, {MA/...}) and keeps {EV} blocks. Returns the date only if
    exactly one distinct {EV} date remains; otherwise None, since the answer is unknown or ambiguous.
    """
    ev = {
        dt
        for (dt, tod, cod) in axioms
        if dt and "{MA/PART" not in (cod or "") and "{MA/" not in (cod or "") and "{EV" in (tod or "")
    }
    return date.fromisoformat(ev.pop()) if len(ev) == 1 else None


def _select_eov(plain_ends, end_axioms) -> date | None:
    """Pick the end-of-validity date.

    The 9999-12-31 sentinel anywhere means still in force (None). Otherwise returns the single
    non-sentinel, non-partial ({...PART} excluded) end date, or None when there is not exactly one
    (a full repeal by replacement, {A/PAR}, is ambiguous in the RDF).
    """
    if "9999-12-31" in set(plain_ends):
        return None
    ends = {dt for (dt, tod, cod) in end_axioms if dt and dt != "9999-12-31" and "PART" not in (cod or "")}
    return date.fromisoformat(ends.pop()) if len(ends) == 1 else None


async def fetch_eu_dates(celex: str, *, lang: str = "eng") -> tuple[date | None, date | None]:
    """Read (valid_from, valid_to) from the CELLAR RDF notice, for CELEX numbers not in _EU_DATES.

    Conservative by design: any ambiguity yields None rather than a plausible but wrong date. Stored
    data for listed acts comes from _EU_DATES, not from this function.
    """
    headers = {"Accept": "application/rdf+xml", "Accept-Language": lang, "User-Agent": _USER_AGENT}
    async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT, headers=headers) as client:
        r = await client.get(_CELLAR_CELEX + celex)
        r.raise_for_status()
        root = etree.fromstring(r.content)
    vf = _select_eif(list(_axioms(root, _EIF_PROP)))
    plain = [el.text for el in root.iter("{" + _CDM + "}resource_legal_date_end-of-validity")]
    vt = _select_eov(plain, list(_axioms(root, _EOV_PROP)))
    return vf, vt


async def eu_dates_for(celex: str) -> tuple[date | None, date | None]:
    """Return (valid_from, valid_to) from the validated table, falling back to the live parser."""
    if celex in _EU_DATES:
        vf, vt = _EU_DATES[celex]
        return date.fromisoformat(vf), (date.fromisoformat(vt) if vt else None)
    return await fetch_eu_dates(celex)


_RAW_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "data" / "raw" / "eurlex"

_EU_CITE = re.compile(r"\b(Directive|Regulation|Decision)\s+(?:\((?:EU|EC|EEC)\)\s+)?(?:No\s+)?(\d{1,4}/\d{1,4})\b")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _local(tag) -> str:
    """Return the local element name without namespace ('{ns}ARTICLE' gives 'ARTICLE'); '' for comments/PIs.

    Elements are matched by local name because the namespace differs across Formex schema versions
    (05.00 adds an `fmx:` prefix).
    """
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _norm_ws(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _norm_id(s: str) -> str:
    """Normalise a Formex IDENTIFIER segment to a stable id: '001' to '1', '014' to '14', '004A' to '4a'."""
    m = re.match(r"0*(\d+)([A-Za-z]*)", (s or "").strip())
    return (m.group(1) + m.group(2).lower()) if m else re.sub(r"[^A-Za-z0-9]", "", (s or "").lower())


def _doctype_for(celex: str) -> str:
    """Map the CELEX type letter (index 5, e.g. 3·2014·L·0065) to a document type.

    L is a directive, R a regulation, anything else a decision.
    """
    t = celex[5:6].upper() if len(celex) > 5 else ""
    return {"L": "eu_smernice", "R": "eu_narizeni"}.get(t, "eu_rozhodnuti")


# An enacting ARTICLE is structural. An ARTICLE nested under any of these elements is quoted inside an
# amendment block ("the following Article is inserted: 'Article 12a ...'") and is not the act's own.
_CONTENT_ANCESTORS = frozenset(
    {"ALINEA", "PARAG", "P", "NP", "ITEM", "LIST", "QUOT.START", "QUOT", "TXT", "TBL", "NOTE", "TI.ART", "STI.ART"}
)


def _enacting_articles(root) -> list:
    """Return the act's own articles, excluding articles quoted inside amendment or insertion blocks."""
    return [
        art
        for art in (e for e in root.iter() if _local(e.tag) == "ARTICLE")
        if not any(_local(a.tag) in _CONTENT_ANCESTORS for a in art.iterancestors())
    ]


def _children_text(el, skip_local: set[str]) -> str:
    """Return the text of el's direct children, one block per child, skipping the named subtrees."""
    chunks = []
    for child in el:
        if not isinstance(child.tag, str):  # skip comments / processing-instructions
            continue
        if _local(child.tag) in skip_local:
            continue
        chunks.append(_norm_ws("".join(child.itertext())))
    return "\n\n".join(c for c in chunks if c)


def _mine_xrefs(text: str, from_eid: str, sink: list[CrossRefDraft]) -> None:
    """Record EU-act citations ('Directive 2014/65', 'Regulation (EU) No 600/2014') as external refs.

    resolve_xrefs.parse_target() later converts them to CELEX numbers and links the cross-corpus edges.
    """
    seen = set()
    for m in _EU_CITE.finditer(text):
        ref = f"{m.group(1)} {m.group(2)}"
        if ref in seen:
            continue
        seen.add(ref)
        sink.append(
            CrossRefDraft(
                from_eid=from_eid, to_external_ref=ref, ref_type="external", detected_by="eurlex-regex", confidence=0.8
            )
        )


# ---------------------------------------------------------------------------
# Fetch: CELLAR content negotiation for the Formex-4 <ACT> body
# ---------------------------------------------------------------------------
async def fetch_formex(celex: str, *, lang: str = "eng") -> tuple[bytes, str]:
    """Fetch an act and return (act_body_xml_bytes, provenance_uri).

    No authentication is needed: GET `http://publications.europa.eu/resource/celex/{CELEX}` with
    `Accept: application/zip;mtype=fmx4` and `Accept-Language` returns a zip of the manifestation's
    parts. The act body is the member with the most enacting <ARTICLE> elements; the Official Journal
    cover and annex parts have fewer or none.
    """
    headers = {"Accept": "application/zip;mtype=fmx4", "Accept-Language": lang, "User-Agent": _USER_AGENT}
    async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT, headers=headers) as client:
        r = await client.get(_CELLAR_CELEX + celex)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "zip" not in ctype and not r.content[:2] == b"PK":
            # Some CELEX numbers return the XML directly rather than a zip.
            return r.content, str(r.url)
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        best, best_name, best_n = None, None, -1
        for name in zf.namelist():
            low = name.lower()
            if not low.endswith(".xml") or low.endswith(".doc.xml"):
                continue
            data = zf.read(name)
            try:
                root = etree.fromstring(data)
            except etree.XMLSyntaxError:
                continue
            n = len(_enacting_articles(root))
            if n > best_n:
                best, best_name, best_n = data, name, n
        if best is None or best_n <= 0:
            raise RuntimeError(
                f"{celex}: no <ACT>/<ARTICLE> body found in the Formex zip (members: {zf.namelist()[:6]})"
            )
        return best, f"{_CELLAR_CELEX}{celex}#{best_name}"


# ---------------------------------------------------------------------------
# Parse: Formex-4 <ACT> into provisions (articles and their numbered paragraphs)
# ---------------------------------------------------------------------------
def parse_formex(xml_bytes: bytes, spec: EuActSpec) -> tuple[list[ProvisionDraft], list[CrossRefDraft], dict]:
    root = etree.fromstring(xml_bytes)
    provs: list[ProvisionDraft] = []
    xrefs: list[CrossRefDraft] = []
    for art in _enacting_articles(root):
        anum = _norm_id(art.get("IDENTIFIER") or str(len(provs) + 1))
        art_eid = f"art_{anum}"
        ti = next((c for c in art if _local(c.tag) == "TI.ART"), None)
        sti = next((c for c in art if _local(c.tag) == "STI.ART"), None)
        art_label = _norm_ws("".join(ti.itertext())) if ti is not None else f"Article {anum}"
        heading = _norm_ws("".join(sti.itertext())) if sti is not None else ""
        parags = [c for c in art if _local(c.tag) == "PARAG"]

        head_parts = [f"**{art_label}**"] + ([f"*{heading}*"] if heading else [])
        if not parags:  # single unnumbered article body
            body = _children_text(art, {"TI.ART", "STI.ART"})
            text = "\n\n".join(head_parts + ([body] if body else []))
        else:
            text = "\n\n".join(head_parts)
        provs.append(
            ProvisionDraft(
                eid=art_eid, label=art_label, hierarchy_path=art_eid, ordinal=len(provs), text=text, parent_eid=None
            )
        )
        _mine_xrefs(text, art_eid, xrefs)

        for par in parags:
            pid = par.get("IDENTIFIER") or ""
            pnum = _norm_id(pid.split(".")[-1]) if pid else str(sum(1 for p in provs if p.parent_eid == art_eid) + 1)
            par_eid = f"{art_eid}__para_{pnum}"
            no = next((c for c in par if _local(c.tag) == "NO.PARAG"), None)
            marker = _norm_ws("".join(no.itertext())) if no is not None else f"{pnum}."
            body = _children_text(par, {"NO.PARAG"})
            text = _norm_ws(f"{marker} {body}")
            provs.append(
                ProvisionDraft(
                    eid=par_eid,
                    label=f"{art_label}({pnum})",
                    hierarchy_path=f"{art_eid}.para_{pnum}",
                    ordinal=len(provs),
                    text=text,
                    parent_eid=art_eid,
                )
            )
            _mine_xrefs(text, par_eid, xrefs)

    n_art = sum(1 for p in provs if p.parent_eid is None)
    meta = {"celex": spec.celex, "short": spec.short, "n_articles": n_art, "n_paragraphs": len(provs) - n_art}
    return provs, xrefs, meta


# ---------------------------------------------------------------------------
# Store: same pattern as esbirka.ingest_act (full replace, raw archived, dry run available)
# ---------------------------------------------------------------------------
def _save_raw(celex: str, blob: bytes) -> tuple[str, str]:
    _RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = _RAW_DIR / f"{celex}.xml"
    path.write_bytes(blob)
    return path.as_uri(), hashlib.sha256(blob).hexdigest()


async def _delete_existing(conn, celex: str) -> None:
    await conn.execute(
        """DELETE FROM cross_reference WHERE from_provision IN
               (SELECT p.id FROM provision p JOIN source_document sd ON sd.id=p.source_document_id
                WHERE sd.external_id=$1 AND sd.publisher='EUR-Lex')
           OR to_provision IN
               (SELECT p.id FROM provision p JOIN source_document sd ON sd.id=p.source_document_id
                WHERE sd.external_id=$1 AND sd.publisher='EUR-Lex')""",
        celex,
    )
    await conn.execute(
        """DELETE FROM provision WHERE source_document_id IN
               (SELECT id FROM source_document WHERE external_id=$1 AND publisher='EUR-Lex')""",
        celex,
    )
    await conn.execute("DELETE FROM source_document WHERE external_id=$1 AND publisher='EUR-Lex'", celex)


async def ingest_celex(spec: EuActSpec, *, store: bool = True) -> dict:
    """Fetch and parse one EU act and, by default, store it; `store=False` is a dry run."""
    body, prov_uri = await fetch_formex(spec.celex)
    provs, xrefs, meta = parse_formex(body, spec)
    if not provs:
        raise RuntimeError(f"{spec.celex}: parser produced 0 provisions")
    if not store:
        return {
            "celex": spec.celex,
            **meta,
            "n_provisions": len(provs),
            "n_cross_refs": len(xrefs),
            "provenance": prov_uri,
        }

    raw_uri, raw_sha = _save_raw(spec.celex, body)
    now = datetime.now(UTC)
    doc_meta = json.dumps(
        {"short": spec.short, "provenance": prov_uri, "n_articles": meta["n_articles"]}, ensure_ascii=False
    )
    vf, vt = await eu_dates_for(spec.celex)  # entry into force / end of validity (date or None)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _delete_existing(conn, spec.celex)
            doc_id = await conn.fetchval(
                """INSERT INTO source_document
                       (external_id, title, publisher, jurisdiction, legal_hierarchy, document_type,
                        language, raw_storage_uri, raw_format, raw_checksum, fetched_at, last_checked_at,
                        status, metadata, valid_from, valid_to)
                   VALUES ($1,$2,'EUR-Lex','EU','EU_SECONDARY',$3,'en',$4,'xml-formex-4',$5,$6,$6,
                           'ingested',$7::jsonb,$8,$9)
                   RETURNING id""",
                spec.celex,
                spec.title,
                spec.document_type,
                raw_uri,
                raw_sha,
                now,
                doc_meta,
                vf,
                vt,
            )
            eid_to_id: dict[str, UUID] = {}
            prov_params = []
            for p in provs:
                rid = uuid4()
                eid_to_id[p.eid] = rid
                prov_params.append(
                    (
                        rid,
                        doc_id,
                        eid_to_id.get(p.parent_eid) if p.parent_eid else None,
                        p.hierarchy_path,
                        p.ordinal,
                        p.label,
                        p.eid,
                        p.text,
                        score_quality(p.text),
                        hashlib.sha256(p.text.encode()).hexdigest(),
                        json.dumps({"source_eid": p.eid}),
                    )
                )
            await conn.executemany(
                """INSERT INTO provision
                       (id, source_document_id, parent_id, hierarchy_path, ordinal, label, eid, text,
                        text_format, ingestion_quality, text_hash, language, legal_hierarchy,
                        jurisdiction, metadata)
                   VALUES ($1,$2,$3,$4::ltree,$5,$6,$7,$8,'gfm',$9,$10,'en','EU_SECONDARY','EU',$11::jsonb)""",
                prov_params,
            )
            ref_params = [
                (eid_to_id[x.from_eid], None, x.to_external_ref, x.ref_type, x.detected_by, x.confidence)
                for x in xrefs
                if x.from_eid in eid_to_id
            ]
            if ref_params:
                await conn.executemany(
                    """INSERT INTO cross_reference
                           (from_provision, to_provision, to_external_ref, ref_type, detected_by, confidence, detected_at)
                       VALUES ($1,$2,$3,$4,$5,$6, now())""",
                    ref_params,
                )
    return {
        "celex": spec.celex,
        "source_document_id": str(doc_id),
        **meta,
        "n_provisions": len(provs),
        "n_cross_refs": len(ref_params),
        "provenance": prov_uri,
    }


# ---------------------------------------------------------------------------
# Verification and CLI
# ---------------------------------------------------------------------------
async def _verify(celex: str) -> None:
    """Parse a CELEX live and compare article and paragraph counts with the stored rows (no write)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        db = await conn.fetchrow(
            """SELECT count(*) FILTER (WHERE p.eid ~ '^art_[0-9a-z]+$') n_art,
                      count(*) FILTER (WHERE p.eid ~ '__para_') n_par, count(*) tot
                 FROM provision p JOIN source_document sd ON sd.id=p.source_document_id
                WHERE sd.external_id=$1 AND sd.publisher='EUR-Lex'""",
            celex,
        )
    spec = next((s for s in EU_FINANCIAL if s.celex == celex), EuActSpec(celex, celex, celex, _doctype_for(celex)))
    body, _ = await fetch_formex(celex)
    _, _, meta = parse_formex(body, spec)
    print(f"=== verify {celex} — parser vs DB golden ===")
    print(f"  parsed : {meta['n_articles']} articles · {meta['n_paragraphs']} paragraphs")
    print(f"  DB     : {db['n_art']} articles · {db['n_par']} paragraphs ({db['tot']} total)")
    ok = db["tot"] and meta["n_articles"] == db["n_art"] and meta["n_paragraphs"] == db["n_par"]
    print(f"  {'✅ MATCH' if ok else '⚠ DIFF (review rendering; counts must match)'}")


async def _run(specs: list[EuActSpec], *, apply: bool) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        present = {
            r["external_id"]
            for r in await conn.fetch("SELECT external_id FROM source_document WHERE publisher='EUR-Lex'")
        }
    todo = [s for s in specs if s.celex not in present]
    print(
        f"=== eurlex ingest — {'APPLY' if apply else 'DRY-RUN'} · {len(todo)}/{len(specs)} to fetch "
        f"({len(specs) - len(todo)} already present) ==="
    )
    for s in todo:
        try:
            r = await ingest_celex(s, store=apply)
            tag = "OK " if apply else "dry"
            print(
                f"  {tag} {s.celex} {s.short:12s} articles={r['n_articles']:>4d} "
                f"paras={r['n_paragraphs']:>4d} xrefs={r['n_cross_refs']:>4d}"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  ERR {s.celex} {s.short:12s} {exc!r}")
    if not apply:
        print("\n  (dry-run — re-run with --apply to write; then run the integration pipeline)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Live EUR-Lex (Formex-4) ingester for Nexus.")
    ap.add_argument("--celex", help="ingest a single CELEX (else the eu_financial registry)")
    ap.add_argument("--verify", metavar="CELEX", help="parse a CELEX and compare counts to the DB golden")
    ap.add_argument("--apply", action="store_true", help="write to the DB (default: dry-run)")
    args = ap.parse_args()
    if args.verify:
        asyncio.run(_verify(args.verify))
        return
    if args.celex:
        spec = next(
            (s for s in EU_FINANCIAL if s.celex == args.celex),
            EuActSpec(args.celex, args.celex, args.celex, _doctype_for(args.celex)),
        )
        asyncio.run(_run([spec], apply=args.apply))
        return
    asyncio.run(_run(list(EU_FINANCIAL), apply=args.apply))


if __name__ == "__main__":
    main()
