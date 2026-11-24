r"""Convert FORMEX 4 XML from EUR-Lex into GitHub-Flavored Markdown provisions.

EUR-Lex publishes the consolidated text of EU acts as FORMEX 4 (`formex-05.55-20141201.xd`).
Akoma Ntoso would be the preferred format, but none of the target acts are available in it, so
FMX4 is walked natively. The element-to-Markdown mapping is listed below the imports.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from lxml import etree

# Element mapping (provision.text is GFM; see amendments/01-markdown-ingestion.md):
#
#     | FMX4 element                       | Markdown                                |
#     |------------------------------------|-----------------------------------------|
#     | `<ARTICLE IDENTIFIER="005">`       | `## Article 5` plus a `*subtitle*` line |
#     | `<PARAG IDENTIFIER="005.001">`     | `### Paragraph 1`                       |
#     | `<NO.PARAG>`                       | folded into the heading                 |
#     | `<ALINEA>`                         | transparent block container             |
#     | `<P>`                              | one Markdown paragraph                  |
#     | `<LIST TYPE="alpha">`              | unordered list, labels kept literally   |
#     | `<ITEM>` / `<NP><NO.P>(a)</NO.P>`  | `- (a) …`                               |
#     | `<TXT>`                            | inline text                             |
#     | `<HT TYPE="ITALIC">`               | `*…*`                                   |
#     | `<HT TYPE="BOLD">`, `"UC"`         | `**…**`                                 |
#     | `<QUOT.START/>…<QUOT.END/>`        | `‘…’` (Unicode quotes from CODE attrs)  |
#     | `<NOTE TYPE="FOOTNOTE">`           | `[^id]` reference                       |
#     | `<REF.DOC.OJ>`                     | inline text, mined later by regex       |
#     | `<TBL>`                            | GFM pipe table (best effort)            |
#     | `<?PAGE NO="N"?>` PI               | dropped                                 |

# Unicode code-point lookup for QUOT.START and QUOT.END
_QUOT_CODE = {
    "2018": "\u2018",  # left single
    "2019": "\u2019",  # right single
    "201C": "\u201c",  # left double
    "201D": "\u201d",  # right double
}


def _localname(el: etree._Element) -> str | None:
    """Return the tag's local name for real elements, or None for comments and PIs."""
    tag = el.tag
    if not isinstance(tag, str):
        return None
    return etree.QName(tag).localname


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------
@dataclass
class ProvisionDraft:
    """Pre-insert representation of one provision row."""

    eid: str  # 'art_5' or 'art_5__para_1'
    label: str  # 'Article 5' or 'Article 5(1)'
    hierarchy_path: str  # ltree text, e.g. 'art_5.para_1'
    ordinal: int  # 0-based within source document
    text: str  # GFM markdown
    parent_eid: str | None = None  # 'art_5' for paragraphs
    metadata: dict = field(default_factory=dict)


@dataclass
class CrossRefDraft:
    """Pre-insert representation of one cross_reference row."""

    from_eid: str
    to_external_ref: str  # textual citation
    ref_type: str  # 'internal' | 'external'
    detected_by: str  # 'regex' | 'fmx4'
    confidence: float


# ---------------------------------------------------------------------------
# Inline renderer (FMX4 to Markdown for prose runs)
# ---------------------------------------------------------------------------
def _render_inline(node: etree._Element) -> str:
    """Render the text content of an inline-mode FMX4 subtree to Markdown."""
    out: list[str] = []
    if node.text:
        out.append(node.text)
    for child in node:
        tag = _localname(child)
        if tag is None:
            if child.tail:
                out.append(child.tail)
            continue
        if tag == "HT":
            inner = _render_inline(child)
            style = (child.get("TYPE") or "").upper()
            if style in {"ITALIC", "I"}:
                out.append(f"*{inner}*")
            elif style in {"BOLD", "B", "UC"}:
                out.append(f"**{inner}**")
            elif style in {"SUB", "SUP"}:
                # Markdown has no native sub/sup; keep inline.
                out.append(inner)
            else:
                out.append(inner)
        elif tag in {"QUOT.START", "QUOT.END"}:
            code = child.get("CODE", "")
            out.append(_QUOT_CODE.get(code.upper(), "'"))
        elif tag == "REF.DOC.OJ":
            # Inline citation to another OJ document: emit plain text. cross_reference rows
            # are mined separately.
            out.append(_render_inline(child))
        elif tag == "NOTE":
            note_id = child.get("NOTE.ID") or f"fn{len(out)}"
            out.append(f"[^{note_id}]")
        elif tag in {"DATE", "FT"}:
            out.append(_render_inline(child))
        else:
            # Unknown inline element: render its text content transparently.
            out.append(_render_inline(child))
        if child.tail:
            out.append(child.tail)
    return "".join(out)


# ---------------------------------------------------------------------------
# Block renderer
# ---------------------------------------------------------------------------
def _render_table(node: etree._Element) -> str:
    """Convert an FMX4 <TBL> into a GFM pipe table, on a best-effort basis.

    FMX4 tables are CALS-flavoured: <TBL><TITLE>?<CORPUS><ROW><CELL>…</CELL>…</ROW>…
    Cell content is flattened with the inline renderer.
    """
    rows: list[list[str]] = []
    for row in node.iter("ROW"):
        cells = []
        for cell in row.iter("CELL"):
            content = _render_inline(cell).strip().replace("\n", " ")
            cells.append(content or " ")
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [" "] * (width - len(r)) for r in rows]
    header = rows[0]
    sep = ["---"] * width
    body = rows[1:]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(sep) + " |"]
    for r in body:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def _render_list(node: etree._Element) -> str:
    """Render a <LIST> with its <ITEM> and <NP> children."""
    lines: list[str] = []
    for item in node.findall("ITEM"):
        np = item.find("NP")
        if np is not None:
            no_p = np.findtext("NO.P", default="").strip()
            txt_el = np.find("TXT")
            body = _render_inline(txt_el).strip() if txt_el is not None else ""
            label = f"{no_p} " if no_p else ""
            lines.append(f"- {label}{body}")
        else:
            body = _render_inline(item).strip()
            lines.append(f"- {body}")
    return "\n".join(lines)


def _render_block(node: etree._Element) -> str:
    """Render a block-level FMX4 element to Markdown."""
    tag = _localname(node)
    if tag is None:
        return ""

    if tag == "P":
        return _render_inline(node).strip()

    if tag == "ALINEA":
        parts = []
        # ALINEA can contain plain inline text, <P>, <LIST>, <TBL>, etc.
        if node.text and node.text.strip():
            parts.append(node.text.strip())
        for child in node:
            ct = _localname(child)
            if ct is None:
                continue
            if ct == "P":
                parts.append(_render_inline(child).strip())
            elif ct == "LIST":
                parts.append(_render_list(child))
            elif ct == "TBL":
                parts.append(_render_table(child))
            elif ct == "NP":
                no_p = child.findtext("NO.P", default="").strip()
                txt_el = child.find("TXT")
                body = _render_inline(txt_el).strip() if txt_el is not None else ""
                parts.append(f"{no_p} {body}".strip())
            elif ct == "FORMULA":
                parts.append("```\n" + (child.text or "").strip() + "\n```")
            else:
                inline = _render_inline(child).strip()
                if inline:
                    parts.append(inline)
            if child.tail and child.tail.strip():
                parts.append(child.tail.strip())
        # Drop empty parts and join the rest as separate Markdown paragraphs.
        return "\n\n".join(p for p in parts if p)

    if tag == "LIST":
        return _render_list(node)

    if tag == "TBL":
        return _render_table(node)

    if tag == "NP":
        no_p = node.findtext("NO.P", default="").strip()
        txt_el = node.find("TXT")
        body = _render_inline(txt_el).strip() if txt_el is not None else ""
        return f"{no_p} {body}".strip()

    # Fallback: treat as a paragraph of inline text.
    return _render_inline(node).strip()


def _article_subtitle(article: etree._Element) -> str:
    sti = article.find("STI.ART")
    return (sti.text or "").strip() if sti is not None and sti.text else ""


def _article_title(article: etree._Element) -> str:
    ti = article.find("TI.ART")
    return (ti.text or "").strip() if ti is not None and ti.text else ""


def _parag_number(parag: etree._Element) -> str:
    no = parag.findtext("NO.PARAG", default="").strip()
    return no.rstrip(".") if no else ""


# ---------------------------------------------------------------------------
# Walker: top-level entry point
# ---------------------------------------------------------------------------
def walk_act(root: etree._Element) -> tuple[list[ProvisionDraft], list[CrossRefDraft]]:
    """Walk a FORMEX 4 <ACT> tree and emit provision and cross_reference drafts.

    Each <ARTICLE> becomes a provision with eid and hierarchy `art_N`, and each <PARAG> within
    it a provision with eid `art_N__para_M` and hierarchy `art_N.para_M`. Both carry full GFM
    text: the article row holds the entire article (heading and all paragraphs), the paragraph
    row only that paragraph. The duplication is intentional: retrieval can hit at either
    resolution and the reranker disambiguates.
    """
    provisions: list[ProvisionDraft] = []
    xrefs: list[CrossRefDraft] = []
    ordinal = 0

    for article in root.iter("ARTICLE"):
        ident = article.get("IDENTIFIER", "").lstrip("0") or article.get("IDENTIFIER", "")
        if not ident:
            continue
        art_eid = f"art_{ident}"
        ti = _article_title(article)
        sti = _article_subtitle(article)

        # Build article-level markdown by rendering each paragraph block.
        body_blocks: list[str] = []
        heading = f"## {ti}" if ti else f"## Article {ident}"
        body_blocks.append(heading)
        if sti:
            body_blocks.append(f"*{sti}*")

        parags = article.findall("PARAG")
        if parags:
            for parag in parags:
                parag_ident = parag.get("IDENTIFIER", "")
                num = _parag_number(parag) or parag_ident.split(".")[-1].lstrip("0")
                # Render the paragraph body: all ALINEA, LIST and TBL children.
                parag_body_parts: list[str] = []
                for child in parag:
                    ct = _localname(child)
                    if ct is None or ct == "NO.PARAG":
                        continue
                    rendered = _render_block(child)
                    if rendered:
                        parag_body_parts.append(rendered)
                parag_body = "\n\n".join(parag_body_parts).strip()

                parag_heading = f"### Paragraph {num}" if num else "### Paragraph"
                article_section = f"{parag_heading}\n\n{parag_body}".strip()
                body_blocks.append(article_section)

                # Emit the PARAG-level provision row.
                para_eid = f"{art_eid}__para_{num}"
                provisions.append(
                    ProvisionDraft(
                        eid=para_eid,
                        label=f"Article {ident}({num})",
                        hierarchy_path=f"{art_eid}.para_{num}",
                        ordinal=ordinal,
                        text=f"{parag_heading}\n\n{parag_body}".strip(),
                        parent_eid=art_eid,
                    )
                )
                ordinal += 1
                _mine_xrefs(parag_body, para_eid, xrefs)
        else:
            # No <PARAG> children: the article body lives in direct ALINEA or P children.
            for child in article:
                ct = _localname(child)
                if ct is None or ct in {"TI.ART", "STI.ART"}:
                    continue
                rendered = _render_block(child)
                if rendered:
                    body_blocks.append(rendered)

        article_md = "\n\n".join(b for b in body_blocks if b).strip()
        provisions.append(
            ProvisionDraft(
                eid=art_eid,
                label=ti or f"Article {ident}",
                hierarchy_path=art_eid,
                ordinal=ordinal,
                text=article_md,
                parent_eid=None,
            )
        )
        ordinal += 1
        _mine_xrefs(article_md, art_eid, xrefs)

    # Re-sort so article rows come before their paragraphs; parents are then flushed first,
    # which makes parent_id wiring during INSERT predictable.
    provisions.sort(key=lambda p: (p.parent_eid is not None, p.ordinal))
    return provisions, xrefs


# ---------------------------------------------------------------------------
# Cross-reference mining (plain-text regex, because FMX4 carries no inline markup for
# citations within the same act)
# ---------------------------------------------------------------------------
_INTERNAL_REF = re.compile(
    r"\bArticle\s+(\d+)(?:\((\d+)\))?(?:\(([a-z])\))?",
    flags=re.IGNORECASE,
)
_EXTERNAL_ACT = re.compile(
    r"\b(?:Directive|Regulation|Decision)\s+(?:\(EU\)\s+)?\d+/\d+",
    flags=re.IGNORECASE,
)


def _mine_xrefs(text: str, from_eid: str, sink: list[CrossRefDraft]) -> None:
    seen: set[str] = set()
    for m in _INTERNAL_REF.finditer(text):
        art = m.group(1)
        para = m.group(2)
        point = m.group(3)
        ref = f"art_{art}"
        if para:
            ref = f"{ref}__para_{para}"
        if point:
            ref = f"{ref}__pt_{point}"
        if ref == from_eid:
            continue  # self-references are not useful
        key = f"i:{ref}"
        if key in seen:
            continue
        seen.add(key)
        sink.append(
            CrossRefDraft(
                from_eid=from_eid,
                to_external_ref=ref,
                ref_type="internal",
                detected_by="regex",
                confidence=0.7,
            )
        )
    for m in _EXTERNAL_ACT.finditer(text):
        ref = m.group(0)
        key = f"e:{ref}"
        if key in seen:
            continue
        seen.add(key)
        sink.append(
            CrossRefDraft(
                from_eid=from_eid,
                to_external_ref=ref,
                ref_type="external",
                detected_by="regex",
                confidence=0.6,
            )
        )


# ---------------------------------------------------------------------------
# Quality scoring
# ---------------------------------------------------------------------------
def score_quality(provision_md: str) -> float:
    """Return a heuristic ingestion quality score from 0 to 1 for one provision's Markdown.

    FMX4 is structured XML, so the baseline is 1.0 and points are deducted only for observable
    decoding failures or empty bodies.
    """
    score = 1.0
    if not provision_md or len(provision_md) < 40:
        score -= 0.3
    if "\ufffd" in provision_md:
        score -= 0.3
    if re.search(r"\|\s*\|\s*\|", provision_md):
        score -= 0.1  # suspect empty table cells
    return max(0.0, score)


# ---------------------------------------------------------------------------
# Convenience iterator for tests and the CLI
# ---------------------------------------------------------------------------
def iter_articles(root: etree._Element) -> Iterable[etree._Element]:
    return root.iter("ARTICLE")
