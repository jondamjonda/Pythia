"""Upload a statute from a Markdown or plain-text file into the corpus.

The text is split into provisions at "§ N", "Čl. N" or "Article N" headers; internal citations
become cross_reference rows, citations of other acts are recorded and resolved where the act is in
the corpus, and the graph is rebuilt. No LLM or API key is needed: retrieval uses Postgres
full-text search until embeddings are backfilled, and the GNN is inductive, so it picks up the new
provisions on its next load without retraining.

Run (from engine/):
  .venv/bin/python -m pythia.ingest.statute STATUTE.md --id "999/2024" --title "Act on X"
  (optional: --jurisdiction CZ --language cs --legal-hierarchy statute --publisher upload)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import pathlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from pythia.db import close_pool, get_pool

# A provision header at the start of a line, optionally as a Markdown heading: "§ 297a", "Article 31",
# or "Čl. 1" / "Článek 1" for constitutional Czech acts, which are divided into articles (články).
_HEADER = re.compile(
    r"^[ \t]{0,3}#{0,6}[ \t]*(§[ \t]*\d+[a-z]?|Čl\.[ \t]*\d+[a-z]?|Článek[ \t]+\d+[a-z]?|Article[ \t]+\d+[a-z]?)\b",
    re.MULTILINE | re.IGNORECASE,
)
# A citation appearing inside a provision's body.
_CITE = re.compile(
    r"(§[ \t]*\d+[a-z]?|Čl\.[ \t]*\d+[a-z]?|Článek[ \t]+\d+[a-z]?|Article[ \t]+\d+[a-z]?)", re.IGNORECASE
)


def _norm_label(s: str) -> str:
    s = re.sub(r"\s+", " ", s.strip())
    low = s.lower()
    if low.startswith("article"):
        return "Article " + s.split()[1]
    # Normalize Czech article forms ("Čl. 1", "Článek 1") to "Čl. N" so a header and a body citation of
    # the same article produce the same label (the internal-link map is keyed on it).
    if low.startswith("čl.") or low.startswith("článek"):
        m = re.search(r"(\d+[a-z]?)", s)
        return "Čl. " + (m.group(1) if m else s)
    return "§ " + s.replace("§", "").strip()


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "x"


@dataclass
class Prov:
    label: str
    text: str
    ordinal: int


def parse_provisions(raw: str) -> list[Prov]:
    """Split the text into provisions, one per span between consecutive headers."""
    marks = list(_HEADER.finditer(raw))
    out: list[Prov] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(raw)
        out.append(Prov(_norm_label(m.group(1)), raw[m.start() : end].strip(), i))
    return out


# Mentions of other acts in a provision body. They are recorded as unresolved external references
# and later linked by resolve_xrefs.py to a real provision if that act is in the corpus. The surface
# forms match what resolve_xrefs.parse_target() understands ("zákona č. 240/2013 Sb.",
# "Directive 2014/65").
_EXT_CZ = re.compile(r"(?:zákon\w*|vyhlášk\w*|nařízení(?:\s+vlády)?)\s+č\.\s*\d+/\d+\s*Sb\.", re.IGNORECASE)
_EXT_EU = re.compile(r"(?:Directive|Regulation|Decision)\s*(?:\(EU\))?\s*(?:No\.?\s*)?\d{1,4}/\d{1,4}", re.IGNORECASE)


def find_external_citations(text: str) -> list[str]:
    """Find references to other acts, de-duplicated, in citation surface form."""
    out: list[str] = []
    seen: set[str] = set()
    for rx in (_EXT_CZ, _EXT_EU):
        for m in rx.finditer(text):
            s = re.sub(r"\s+", " ", m.group(0).strip())
            if s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
    return out


async def ingest_statute(
    raw_text: str,
    *,
    external_id: str,
    title: str,
    jurisdiction: str = "CZ",
    language: str = "cs",
    publisher: str = "upload",
    legal_hierarchy: str = "statute",
    rematerialize: bool = True,
    link_external: bool = True,
) -> dict:
    """Store a statute's provisions and citations, then optionally rebuild the graph.

    Returns a summary dict with the new document id and counts of provisions, internal
    cross-references and external citations.

    Raises:
        ValueError: if no provision headers are found in `raw_text`.
    """
    provs = parse_provisions(raw_text)
    if not provs:
        raise ValueError("no provisions detected — need lines starting with '§ N' or 'Article N'")
    docslug = "d_" + _slug(external_id)
    now = datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            doc_id = await conn.fetchval(
                """INSERT INTO source_document
                       (external_id, title, publisher, jurisdiction, legal_hierarchy, language,
                        document_type, raw_storage_uri, raw_checksum, fetched_at, last_checked_at, metadata)
                   VALUES ($1,$2,$3,$4,$5,$6,'statute',$7,$8,$9,$9,
                           jsonb_build_object('source','pythia-upload'))
                   RETURNING id""",
                external_id,
                title,
                publisher,
                jurisdiction,
                legal_hierarchy,
                language,
                f"upload://{external_id}",
                hashlib.sha256(raw_text.encode()).hexdigest(),
                now,
            )
            label_to_id: dict[str, uuid.UUID] = {}
            for p in provs:
                pid = uuid.uuid4()
                await conn.execute(
                    """INSERT INTO provision
                           (id, source_document_id, hierarchy_path, ordinal, label, text,
                            text_format, text_hash, language, legal_hierarchy, jurisdiction)
                       VALUES ($1,$2,$3::ltree,$4,$5,$6,'gfm',$7,$8,$9,$10)""",  # tsv is generated
                    pid,
                    doc_id,
                    f"{docslug}.p_{_slug(p.label)}",
                    p.ordinal,
                    p.label,
                    p.text,
                    hashlib.sha256(p.text.encode()).hexdigest(),
                    language,
                    legal_hierarchy,
                    jurisdiction,
                )
                label_to_id[p.label] = pid
            n_xref = 0
            for p in provs:
                body = p.text.split("\n", 1)[1] if "\n" in p.text else ""  # skip the header line
                seen: set[str] = set()
                for cm in _CITE.finditer(body):
                    tgt = _norm_label(cm.group(1))
                    if tgt in label_to_id and tgt != p.label and tgt not in seen:
                        seen.add(tgt)
                        await conn.execute(
                            """INSERT INTO cross_reference
                                   (from_provision, to_provision, ref_type, detected_by, confidence, detected_at)
                               VALUES ($1,$2,'internal','pythia-ingest',0.9, now())""",
                            label_to_id[p.label],
                            label_to_id[tgt],
                        )
                        n_xref += 1
            n_ext = 0
            if link_external:  # record citations of other acts (resolved below)
                for p in provs:
                    for ext in find_external_citations(p.text):
                        await conn.execute(
                            """INSERT INTO cross_reference
                                   (from_provision, to_provision, to_external_ref, ref_type,
                                    detected_by, confidence, detected_at)
                               VALUES ($1, NULL, $2, 'external', 'pythia-ingest', 0.6, now())""",
                            label_to_id[p.label],
                            ext,
                        )
                        n_ext += 1
    res = {
        "doc_id": str(doc_id),
        "external_id": external_id,
        "provisions": len(provs),
        "internal_xrefs": n_xref,
        "external_citations": n_ext,
    }
    if link_external and n_ext:  # link those to real provisions where we hold the act
        from pythia.ingest.resolve_xrefs import resolve_external_xrefs

        await resolve_external_xrefs(rematerialize=False)
        res["external_linked"] = await pool.fetchval(
            """SELECT count(*) FROM cross_reference cr JOIN provision p ON p.id = cr.from_provision
               WHERE p.source_document_id = $1 AND cr.ref_type = 'external'
                 AND cr.to_provision IS NOT NULL""",
            doc_id,
        )
    if rematerialize:
        from pythia.graph.materialize import materialize_layer_a

        res["graph_edges"] = (await materialize_layer_a())["total_edges"]
        # Refresh the graph-intelligence layer (centrality and community) after the edges are rebuilt,
        # so the new statute's provisions are scored. It is a no-op when the graph fingerprint has not
        # changed. The court, ČNB and EUR-Lex CLIs only store rows and leave materialization to the
        # operator, who should run the same hook afterwards: reindex_intelligence(), or
        # `python -m pythia.graph.intelligence --apply`.
        from pythia.graph.intelligence import reindex_intelligence

        intel = await reindex_intelligence()
        res["intelligence_nodes"] = intel["n"]
    return res


def _main() -> None:
    ap = argparse.ArgumentParser(description="Upload a statute into the Pythia corpus.")
    ap.add_argument("file", help="path to the statute as Markdown or plain text")
    ap.add_argument("--id", required=True, help="document id, e.g. '999/2024' or a CELEX code")
    ap.add_argument("--title", required=True)
    ap.add_argument("--jurisdiction", default="CZ")
    ap.add_argument("--language", default="cs")
    ap.add_argument("--legal-hierarchy", default="statute")
    ap.add_argument("--publisher", default="upload")
    a = ap.parse_args()
    raw = pathlib.Path(a.file).read_text(encoding="utf-8")

    async def run():
        res = await ingest_statute(
            raw,
            external_id=a.id,
            title=a.title,
            jurisdiction=a.jurisdiction,
            language=a.language,
            legal_hierarchy=a.legal_hierarchy,
            publisher=a.publisher,
        )
        print("ingested:", res)
        await close_pool()

    asyncio.run(run())


if __name__ == "__main__":
    _main()
