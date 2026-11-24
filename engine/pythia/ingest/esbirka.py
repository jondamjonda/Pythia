r"""Ingest consolidated Czech acts from the e-Sbírka (Collection of Laws) Linked Data portal.

Each act is identified by its ELI on `opendata.eselpoint.gov.cz`
(https://opendata.eselpoint.gov.cz/esel-esb/eli/cz/sb/{year}/{number}). The ingester resolves the
consolidated version in force on a given date, fetches and walks its fragment tree, stores one GFM
Markdown provision row per § (or per Čl. in constitutional acts), and mines internal and external
cross-references from the text.

Run (from engine/): .venv/bin/python -m pythia.ingest.esbirka [--act NUMBER/YEAR ...]
(without --act, ingests the default set of acts in MVP_ACTS)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import httpx
from lxml import html as lhtml


import logging
import pathlib

from pythia.db import get_pool

_log = logging.getLogger("pythia.ingest.esbirka")


# ---------------------------------------------------------------------------
# Default target acts (ingested when no --act is given)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CzechActSpec:
    year: int
    number: int
    short: str
    title: str
    legal_hierarchy: str  # 'cz-act' for statutes
    document_type: str  # 'act' (zákon)


MVP_ACTS: tuple[CzechActSpec, ...] = (
    CzechActSpec(
        2013,
        240,
        "ZISIF",
        "Zákon č. 240/2013 Sb., o investičních společnostech a investičních fondech",
        "cz-act",
        "act",
    ),
    CzechActSpec(2004, 256, "ZPKT", "Zákon č. 256/2004 Sb., o podnikání na kapitálovém trhu", "cz-act", "act"),
    CzechActSpec(2012, 89, "Civil Code", "Zákon č. 89/2012 Sb., občanský zákoník", "cz-act", "act"),
    CzechActSpec(
        2008,
        253,
        "AML Act",
        "Zákon č. 253/2008 Sb., o některých opatřeních proti legalizaci výnosů z trestné činnosti",
        "cz-act",
        "act",
    ),
    CzechActSpec(2012, 90, "BCA", "Zákon č. 90/2012 Sb., o obchodních společnostech a družstvech", "cz-act", "act"),
)


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------
_OPENDATA_BASE = "https://opendata.eselpoint.gov.cz"
_USER_AGENT = "pythia-ingest/0.1 (+https://example.invalid)"
_ACCEPT_LD_JSON = "application/ld+json"
_FETCH_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_FETCH_LIMITS = httpx.Limits(max_connections=64, max_keepalive_connections=32)
_FETCH_CONCURRENCY = 32  # bounded: eselpoint is a small public endpoint


async def _get_json(client: httpx.AsyncClient, url: str) -> dict:
    """GET a JSON-LD resource, raising on non-2xx."""
    r = await client.get(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": _ACCEPT_LD_JSON,
        },
    )
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Walker — pure function over pre-fetched nodes
# ---------------------------------------------------------------------------
@dataclass
class FragmentNode:
    """One addressed fragment in the act tree."""

    path: tuple[str, ...]  # ('cast_13','hlava_5','par_506','odst_2','pism_a')
    kind: str  # 'cast'|'hlava'|'dil'|'oddil'|'par'|'odst'|'pism'|'bod'|'frag'|...
    designator: str  # '506', '2', 'a', '1'  (last path segment's tail)
    label: str  # citace e.g. '§ 506 odst. 2 písm. a)'
    order_key: str  # pořadí hex e.g. '6DB95AD580'
    text_html: str  # raw HTML snippet from text-fragmentu
    parent_path: tuple[str, ...] | None


@dataclass
class ProvisionDraft:
    eid: str
    label: str
    hierarchy_path: str
    ordinal: int
    text: str
    parent_eid: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class CrossRefDraft:
    from_eid: str
    to_external_ref: str
    ref_type: str  # 'internal' | 'external'
    detected_by: str  # 'esbirka-anchor' | 'regex' | 'named-act'
    confidence: float
    metadata: dict = field(default_factory=dict)  # e.g. {'named_art': '2079'} for name cites


_SEG_KIND_RE = re.compile(r"^([a-z]+)_(.*)$")


def _split_kind(seg: str) -> tuple[str, str]:
    """Split a path segment into (kind, designator), e.g. 'par_506a' gives ('par', '506a').

    Returns (seg, '') when the segment has no kind prefix.
    """
    m = _SEG_KIND_RE.match(seg)
    return (m.group(1), m.group(2)) if m else (seg, "")


# ---------------------------------------------------------------------------
# HTML to inline Markdown conversion (applied to each text-fragmentu snippet).
# ---------------------------------------------------------------------------
_VAR_LABEL_RE = re.compile(r"^\s*<var[^>]*>(.*?)</var>\s*", flags=re.DOTALL | re.IGNORECASE)


def _strip_leading_var(html_snippet: str) -> tuple[str, str]:
    """Pull the inline `<var>label</var>` off the front of a snippet.

    The label (§ N, (1), a)) is captured separately in `FragmentNode.label`,
    so we render the body without re-emitting it. Returns (label_text, rest_html).
    """
    m = _VAR_LABEL_RE.match(html_snippet or "")
    if not m:
        return "", html_snippet or ""
    label = (
        re.sub(
            r"\s+",
            " ",
            lhtml.fragment_fromstring(f"<span>{m.group(1)}</span>", create_parent="span").text_content().strip(),
        )
        if m.group(1)
        else ""
    )
    rest = html_snippet[m.end() :]
    return label, rest


def _html_to_markdown_inline(snippet: str) -> str:
    """Convert a small HTML fragment to inline Markdown.

    Handles `<a>`, `<var>`, `<i>`, `<b>`, `<strong>`, `<em>`, `<sup>`, `<sub>`,
    `<br>`, line wrapping. Anything else is rendered as its text content.
    """
    if not snippet or not snippet.strip():
        return ""
    # lxml.html needs a wrapper for fragments
    try:
        node = lhtml.fragment_fromstring(snippet, create_parent="div")
    except Exception:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", snippet)).strip()
    out: list[str] = []
    _walk_inline(node, out)
    text = "".join(out)
    # Collapse double spaces and trim. Preserve intentional newlines (from <br>).
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text


def _walk_inline(el, out: list[str]) -> None:
    """Append the Markdown rendering of `el`'s children into `out`."""
    if el.text:
        out.append(el.text)
    for child in el:
        tag = (child.tag or "").lower() if isinstance(child.tag, str) else ""
        if tag == "var":
            # Inline label. The leading var was already stripped; mid-text vars are
            # rare but occur in nested numbering. Render as bold.
            inner: list[str] = []
            _walk_inline(child, inner)
            out.append(f"**{''.join(inner).strip()}**")
        elif tag == "a":
            href = child.get("href", "") or ""
            cls = (child.get("class") or "").strip()
            inner = []
            _walk_inline(child, inner)
            text = "".join(inner).strip()
            if "int_odkaz" in cls:
                # Internal cross-reference: the anchor target (data-odkaz-id) is opaque,
                # so emit the text only; cross_reference rows come from the regex pass.
                out.append(text or href)
            elif href.startswith("http"):
                out.append(f"[{text or href}]({href})")
            else:
                out.append(text or href)
        elif tag in {"i", "em"}:
            inner = []
            _walk_inline(child, inner)
            out.append(f"*{''.join(inner)}*")
        elif tag in {"b", "strong"}:
            inner = []
            _walk_inline(child, inner)
            out.append(f"**{''.join(inner)}**")
        elif tag in {"sup", "sub"}:
            inner = []
            _walk_inline(child, inner)
            out.append("".join(inner))
        elif tag == "br":
            out.append("\n")
        elif tag in {"p", "div"}:
            inner = []
            _walk_inline(child, inner)
            out.append("\n\n" + "".join(inner).strip())
        else:
            _walk_inline(child, out)
        if child.tail:
            out.append(child.tail)


# ---------------------------------------------------------------------------
# Per-provision renderer
# ---------------------------------------------------------------------------
# The canonical provision.text payload is GFM Markdown:
#
#   e-Sbírka kind   Markdown
#   cast            `# Část N — {název}` (ancestor, not a row)
#   hlava           `## Hlava N — {název}` (ancestor)
#   dil             `### Díl N — {název}` (ancestor)
#   oddil           `#### Oddíl N` (ancestor)
#   par             `##### § N` (this is the provision row)
#   odst            `**(N)**` inline at the start of a body paragraph
#   pism            `a) …` list item
#   bod             `1. …` numbered list item
#
# A provision row's Markdown holds the § heading line plus the descendant odst/pism/bod tree rendered
# inline. Cast/hlava/dil/oddil headings live in `hierarchy_path` (ltree), and the source viewer
# reconstructs the full breadcrumb at render time.
#
# The addressed-fragment kinds that become a top-level provision row (one provision
# each), as opposed to structural ancestors (cast/hlava/dil/oddil/pododdil) or
# descendants (odst/pism/bod/frag). e-Sbírka ASCII-folds the Czech structural word
# for the path-segment type token, so an "Článek" (article) boundary arrives as
# `clanek_N` (older exports: `cl_N`). Constitutional-rank acts (Ústava, Listina,
# ústavní zákony) are organised in articles, not §§, and would otherwise produce no
# provisions.
_PROVISION_KINDS: frozenset[str] = frozenset({"par", "clanek", "cl"})


def _default_label(prov: FragmentNode) -> str:
    """Return a fallback heading for a fragment without a `citace` label.

    `§ N` for a paragraf, `Čl. N` for an article (clanek/cl). Mirrors browse._STRUCT_LABELS.
    """
    if prov.kind in ("clanek", "cl"):
        return f"Čl. {prov.designator}"
    return f"§ {prov.designator}"


_ANNEX_LABEL_PREFIX = re.compile(r"^Příloha\s+(?=Čl\.|§)")


def _provision_label(prov: FragmentNode) -> str:
    """Return a provision's heading: the e-Sbírka `citace` label, else a synthesised `§ N` or `Čl. N`.

    Strips a leading `Příloha ` (annex) token that e-Sbírka prepends to the articles of acts
    promulgated as a constitutional annex (the Charter, 2/1993 Sb.): a lawyer cites `Čl. 10`,
    not `Příloha Čl. 10`. The lookahead keeps genuine appendix labels (`Příloha č. 1`) intact.
    """
    label = _ANNEX_LABEL_PREFIX.sub("", (prov.label or "").strip())
    return label or _default_label(prov)


def _render_paragraf(
    par: FragmentNode,
    children_index: dict[tuple[str, ...], list[FragmentNode]],
) -> str:
    """Render one provision fragment (`par_*` or `clanek_*`) plus its descendant tree as a GFM block."""
    # 1) Heading line: `##### § N` or `##### Čl. N`, or the citace label.
    heading = _provision_label(par)
    lines: list[str] = [f"##### {heading}"]

    # 2) Body: walk descendants in document order and render each into a chunk.
    sub_lines = _render_children(par, children_index, base_indent=0)
    if sub_lines:
        lines.append("")
        lines.extend(sub_lines)
    else:
        # Some paragrafs carry text directly (no odst/pism/bod children).
        _, body_html = _strip_leading_var(par.text_html)
        body_md = _html_to_markdown_inline(body_html)
        if body_md:
            lines.append("")
            lines.append(body_md)

    return "\n".join(lines).rstrip()


def _render_children(
    parent: FragmentNode,
    children_index: dict[tuple[str, ...], list[FragmentNode]],
    *,
    base_indent: int,
) -> list[str]:
    """Render `parent`'s direct children + their descendants as markdown lines.

    Formatting rules:
      - odst:  `**(N)** body…`  (no indent)
      - pism:  `a) body…`        (2 spaces per nest level)
      - bod:   `1. body…`        (2 more spaces under pism)
      - frag:  plain paragraph
    """
    direct = children_index.get(parent.path, [])
    if not direct:
        return []
    lines: list[str] = []
    indent = "  " * base_indent
    for ch in direct:
        _, body_html = _strip_leading_var(ch.text_html)
        body_md = _html_to_markdown_inline(body_html)
        # Re-flow newlines so list items don't break across markdown blocks.
        body_inline = re.sub(r"\s*\n\s*", " ", body_md).strip()

        if ch.kind == "odst":
            prefix = f"**({ch.designator})** " if ch.designator else "**(?)** "
            lines.append(f"{indent}{prefix}{body_inline}".rstrip())
        elif ch.kind == "pism":
            prefix = f"{ch.designator}) " if ch.designator else "- "
            lines.append(f"{indent}{prefix}{body_inline}".rstrip())
        elif ch.kind == "bod":
            prefix = f"{ch.designator}. " if ch.designator else "1. "
            lines.append(f"{indent}{prefix}{body_inline}".rstrip())
        elif ch.kind == "frag":
            if body_inline:
                lines.append(f"{indent}{body_inline}")
        else:
            # Unknown sub-kind under a paragraf: render as a plain paragraph.
            if body_inline:
                lines.append(f"{indent}{body_inline}")

        nested = _render_children(ch, children_index, base_indent=base_indent + 1)
        if nested:
            lines.extend(nested)
    return lines


# ---------------------------------------------------------------------------
# Flat-body fallback for acts with no §/Čl. provision rows.
# ---------------------------------------------------------------------------
# Label for the single provision synthesised from a flat act's whole body.
_FLAT_PROVISION_LABEL = "Plné znění"


def _flat_provision_text(nodes: list[FragmentNode]) -> str:
    """Render an act with no provision rows (no `par` or `clanek` fragment) as one flat Markdown block.

    Some Sbírka instruments, chiefly ministerial *sdělení* and *oznámení* (announcements and notices),
    carry no §/Článek tree, only a short enumerated body (an intro `frag` plus `bod`/`odst`/`pism`
    items), so the structured walker yields no provisions for them. This concatenates every fragment's
    text in document order (`order_key`), keeping the enumeration markers (`1.`, `a)`, `(N)`) and
    nesting by path depth, so the announced figure (e.g. the minimum-wage amount in koruna) stays in one
    retrievable provision. Returns "" when there is no body text at all (the caller then raises).
    """
    lines: list[str] = []
    for n in sorted(nodes, key=lambda x: x.order_key):
        _, body_html = _strip_leading_var(n.text_html)
        body = re.sub(r"\s*\n\s*", " ", _html_to_markdown_inline(body_html)).strip()
        if not body:
            continue
        indent = "  " * max(0, len(n.path) - 1)
        if n.kind == "bod" and n.designator:
            lines.append(f"{indent}{n.designator}. {body}")
        elif n.kind == "odst" and n.designator:
            lines.append(f"{indent}**({n.designator})** {body}")
        elif n.kind == "pism" and n.designator:
            lines.append(f"{indent}{n.designator}) {body}")
        else:
            lines.append(f"{indent}{body}")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Walker entry point: turns the fetched fragment graph into provision drafts.
# ---------------------------------------------------------------------------
_INT_REF = re.compile(r"§\s*(\d+[a-z]?)(?:\s*odst\.\s*(\d+))?(?:\s*písm\.\s*([a-z]))?", flags=re.IGNORECASE)
# Intra-act article reference (Ústava, Listina and ústavní zákony cite "čl. N", "Čl. N odst. M"
# or "Článek N", not "§ N"). Mined into a `clanek_N` internal target so the same endswith match
# the § path uses (ingest_act) links it to the article provision. Matching on the surface form
# keeps it inert in §-only acts, which never write "Čl." or "Článek".
_INT_ART = re.compile(r"\b(?:Čl\.|Článek)\s*(\d+[a-z]?)(?:\s*odst\.\s*(\d+))?", flags=re.IGNORECASE)
_EXT_ACT = re.compile(r"(?:zákon|vyhlášk|nařízen)\w*\s+č\.\s*(\d+/\d+\s*Sb\.)", flags=re.IGNORECASE)


def walk_act(
    nodes: list[FragmentNode], own_external_id: str | None = None
) -> tuple[list[ProvisionDraft], list[CrossRefDraft]]:
    """Convert a flat fragment list into provision and cross-reference drafts.

    `own_external_id` ("N/YYYY") lets the named-act miner drop self-citations: a hit on the act's
    own name is an intra-act reference, not a cross-act one.
    """
    children: dict[tuple[str, ...], list[FragmentNode]] = defaultdict(list)
    for n in nodes:
        if n.parent_path is not None:
            children[n.parent_path].append(n)
    for plist in children.values():
        plist.sort(key=lambda x: x.order_key)

    # Provision rows are § fragments (`par`) plus, for constitutional-rank acts
    # structured in articles, article fragments (`clanek`/`cl`). A §-only act has no
    # article fragments, so the extra kind changes nothing for it; an act built solely
    # of articles (Ústava 1/1993, Listina 2/1993, ústavní zákon 110/1998) would
    # otherwise produce no provisions at all.
    pars = [n for n in nodes if n.kind in _PROVISION_KINDS]
    pars.sort(key=lambda n: n.order_key)

    provisions: list[ProvisionDraft] = []
    xrefs: list[CrossRefDraft] = []
    for ordinal, par in enumerate(pars):
        eid = "_".join(par.path)
        hierarchy_path = ".".join(par.path)
        md = _render_paragraf(par, children)
        provisions.append(
            ProvisionDraft(
                eid=eid,
                label=_provision_label(par),
                hierarchy_path=hierarchy_path,
                ordinal=ordinal,
                text=md,
                parent_eid=None,
            )
        )
        _mine_xrefs(md, eid, xrefs, own_external_id=own_external_id)

    # Fallback for a flat act with no §/Čl. provision rows (ministerial *sdělení*, *oznámení*): no
    # par/clanek fragment exists, so the loop above produced nothing. Rather than drop the act
    # (ingest_act would raise "walker produced 0 provisions"), capture its whole body, the fragment
    # text in document order, as one provision so the announced figure stays retrievable. Ordinary
    # § and Čl. acts always yield provision rows, so this never runs for them. A document with no
    # body text still returns [] and ingest_act raises.
    if not provisions:
        body = _flat_provision_text(nodes)
        if body:
            provisions.append(
                ProvisionDraft(
                    eid="full",
                    label=_FLAT_PROVISION_LABEL,
                    hierarchy_path="full",
                    ordinal=0,
                    text=body,
                    parent_eid=None,
                )
            )
            _mine_xrefs(body, "full", xrefs, own_external_id=own_external_id)
    return provisions, xrefs


def _mine_xrefs(text: str, from_eid: str, sink: list[CrossRefDraft], own_external_id: str | None = None) -> None:
    """Extract internal § and Čl. references and external Sbírka act citations into `sink`.

    External citations are mined in two forms: numbered ("č. N/YYYY Sb.", via _EXT_ACT) and
    colloquial by name ("občanského zákoníku", "trestní zákoník", "správní řád", ...) via the
    bounded, in-corpus-only NAMED_ACT_MAP. A named hit is recorded with the same to_external_ref
    shape the numbered miner uses ("zákona č. N/YYYY Sb."), so resolve_xrefs resolves both the
    same way. Named hits are skipped when they cite the act itself (own_external_id) or duplicate
    a numbered citation already mined from the same text.
    """
    seen: set[str] = set()
    for m in _INT_REF.finditer(text):
        par = m.group(1).lower()
        odst = m.group(2)
        pism = m.group(3)
        ref = f"par_{par}"
        if odst:
            ref = f"{ref}.odst_{odst}"
        if pism:
            ref = f"{ref}.pism_{pism}"
        if ref in seen or ref == from_eid:
            continue
        seen.add(ref)
        sink.append(
            CrossRefDraft(
                from_eid=from_eid,
                to_external_ref=ref,
                ref_type="internal",
                detected_by="regex",
                confidence=0.6,
            )
        )
    for m in _INT_ART.finditer(text):  # intra-act article refs (constitutional acts)
        art = m.group(1).lower()
        odst = m.group(2)
        ref = f"clanek_{art}"
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
                confidence=0.6,
            )
        )
    numbered_acts: set[str] = set()  # "N/YYYY" acts already mined in numbered form from this text
    for m in _EXT_ACT.finditer(text):
        ref = m.group(0)
        key = f"ext:{ref}"
        if key in seen:
            continue
        seen.add(key)
        nm = re.search(r"(\d+/\d+)\s*Sb\.", ref)
        if nm:
            numbered_acts.add(nm.group(1))
        sink.append(
            CrossRefDraft(
                from_eid=from_eid,
                to_external_ref=ref,
                ref_type="external",
                detected_by="regex",
                confidence=0.55,
            )
        )
    # Colloquial by-name external citations (NAMED_ACT_MAP), recorded in the numbered
    # to_external_ref shape. Skip a hit on the act's own name (an intra-act reference) and one
    # whose act was already mined in numbered form from this text (no double counting). The
    # import is local to keep importing esbirka lightweight.
    from pythia.ingest.named_acts import mine_named_acts, CONF_NAMED, DETECTED_BY as _NAMED_BY

    for hit in mine_named_acts(text):
        if own_external_id and hit.external_id == own_external_id:
            continue
        if hit.external_id in numbered_acts:
            continue
        key = f"named:{hit.external_id}"
        if key in seen:
            continue
        seen.add(key)
        sink.append(
            CrossRefDraft(
                from_eid=from_eid,
                to_external_ref=hit.to_external_ref,
                ref_type="external",
                detected_by=_NAMED_BY,
                confidence=CONF_NAMED,
                metadata={"named_art": hit.section} if hit.section else {},
            )
        )


# ---------------------------------------------------------------------------
# Quality scoring
# ---------------------------------------------------------------------------
def score_quality(provision_md: str) -> float:
    """Return a heuristic quality score in [0, 1], same shape as fmx4.score_quality.

    e-Sbírka is structured Linked Data, so the baseline is 1.0; points are only
    deducted for observable problems (replacement characters, suspiciously short bodies).
    """
    score = 1.0
    if not provision_md or len(provision_md) < 60:
        score -= 0.3
    if "\ufffd" in provision_md:
        score -= 0.3
    # Heading-only paragrafs (no body) hint at missing sub-fragments.
    if provision_md.count("\n") < 1:
        score -= 0.2
    return max(0.0, score)


# ---------------------------------------------------------------------------
# Fetcher: resolves the act, then its version, fragments and content snippets
# ---------------------------------------------------------------------------
# Source data model: the act node references one or more versions (`znění`), each scoped to an
# effective date range (`účinnost-znění-od` to `účinnost-znění-do`). Each version exposes a flat list
# of fragment URIs (`má-fragment-znění`) that, with their parent and ordering attributes, reconstruct
# the full document tree:
#
#     cast_N / hlava_M / dil_K / oddil_J / par_X / odst_Y / pism_Z / bod_W
#
# Each addressed fragment references a content fragment (`obsahuje-fragment`) holding the actual prose
# as a small HTML snippet (`text-fragmentu`). The snippet is the document's literal text for that
# boundary; a paragraph's text is its snippet followed by its descendants' snippets in document order.
def _short_path(uri: str) -> str:
    """Strip the opendata base URL: `https://.../esel-esb/eli/...` becomes `esel-esb/eli/...`."""
    if uri.startswith(_OPENDATA_BASE + "/"):
        return uri[len(_OPENDATA_BASE) + 1 :]
    return uri


def _fragment_path_segs(frag_uri: str) -> tuple[str, ...] | None:
    """Extract the (cast_N, hlava_M, …) tuple from a fragment URI.

    Returns None for non-body URIs (e.g. /preambule, /postface).

    The substantive text normally lives under /dokument/norma/. A small class of
    acts promulgated as a constitutional *annex* — chiefly the Charter of
    Fundamental Rights (2/1993 Sb.) — carry their entire body under
    /dokument/prilohy/ instead, with only the one-line promulgation clause under
    /dokument/norma/. We recognise that marker too so those acts walk correctly.
    Ordinary appendices (forms, tables) also live under /prilohy/ but are not of a
    provision kind, so the walker's _PROVISION_KINDS filter still excludes them.
    """
    short = _short_path(frag_uri)
    for marker in ("/dokument/norma/", "/dokument/prilohy/"):
        if marker in short:
            tail = short.split(marker, 1)[1]
            segs = tuple(p for p in tail.split("/") if p)
            return segs or None
    return None


async def _resolve_version(client: httpx.AsyncClient, year: int, number: int, on_date: date | None) -> tuple[str, dict]:
    """Pick the consolidated act version whose `účinnost-znění` window covers `on_date` (default today).

    Falls back to the most recent past version, then to the act's `má-poslední-znění` (latest
    version) pointer. Returns (version_short_path, version_json).
    """
    on_date = on_date or date.today()
    act_url = f"{_OPENDATA_BASE}/esel-esb/eli/cz/sb/{year}/{number}"
    act = await _get_json(client, act_url)
    raw_versions = act.get("má-znění") or []
    if isinstance(raw_versions, str):
        raw_versions = [raw_versions]
    if not raw_versions:
        raise RuntimeError(f"act {year}/{number} has no versions")

    # Fetch each version's metadata concurrently; they are small (under 2 KB).
    async def _vmeta(uri: str) -> tuple[str, dict]:
        full = uri if uri.startswith("http") else f"{_OPENDATA_BASE}/{uri}"
        return uri, await _get_json(client, full)

    metas = await asyncio.gather(*[_vmeta(u) for u in raw_versions])

    # Some acts (e.g. 89/2012) ship version nodes without `účinnost-znění-od`;
    # the date is encoded in the URI suffix instead (see _version_effect_date).
    def _od_for(uri: str, meta: dict) -> date | None:
        return _version_effect_date(uri, meta)

    # Filter to KONSOL (consolidated) versions effective on `on_date`.
    candidates: list[tuple[date, str, dict]] = []
    for uri, meta in metas:
        typ = meta.get("má-typ-znění-právního-aktu", "")
        if typ and "KONSOL" not in typ:
            continue
        od = _od_for(uri, meta)
        do = _parse_date(meta.get("účinnost-znění-do"))
        if od is None:
            continue
        if od > on_date:
            continue
        if do is not None and do < on_date:
            continue
        candidates.append((od, uri, meta))
    if not candidates:
        # Fall back to the most-recent past version regardless of validity.
        scored: list[tuple[date, str, dict]] = []
        for uri, meta in metas:
            od = _od_for(uri, meta)
            if od and od <= on_date:
                scored.append((od, uri, meta))
        if not scored:
            # Final fallback: trust the act's own `má-poslední-znění` pointer.
            last = act.get("má-poslední-znění")
            if isinstance(last, str) and last:
                for uri, meta in metas:
                    if uri.endswith(last.rsplit("/", 1)[-1]) or uri == last:
                        return _short_path(uri), meta
            raise RuntimeError(f"no effective version of {year}/{number} on {on_date}")
        scored.sort(reverse=True)
        return _short_path(scored[0][1]), scored[0][2]
    candidates.sort(reverse=True)  # most-recent `od` wins
    return _short_path(candidates[0][1]), candidates[0][2]


def _parse_date(s: str | list | None) -> date | None:
    if isinstance(s, (list, tuple)):  # some acts carry multiple účinnost entries; take the first
        s = s[0] if s else None
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


_VERSION_SUFFIX_RE = re.compile(r"/(\d{4}-\d{2}-\d{2})$")


def _version_effect_date(version_path: str | None, version_meta: dict | None) -> date | None:
    """Return the účinnost (effective-from date) of a resolved consolidated version.

    Uses the explicit 'účinnost-znění-od' field when present (most acts). Otherwise falls back to
    the date suffix of the version path: e-Sbírka encodes a consolidated version's účinnost in the
    ELI path (/eli/cz/sb/{y}/{n}/{YYYY-MM-DD}), and that suffix equals 'účinnost-znění-od' for
    every KONSOL version (verified on all 15 versions of 240/2013). For acts such as 89/2012 that
    omit the JSON property, it is the same authoritative date exposed differently, not a guess.
    The promulgation sentinel 0000-00-00, or no date at all, gives None (unknown effect date).
    """
    d = _parse_date((version_meta or {}).get("účinnost-znění-od"))
    if d is not None:
        return d
    m = _VERSION_SUFFIX_RE.search(version_path or "")
    return _parse_date(m.group(1)) if m else None


async def _fetch_fragment_graph(client: httpx.AsyncClient, version_meta: dict) -> list[FragmentNode]:
    """Resolve every fragment URI listed in `version_meta` into a FragmentNode.

    Performs two passes:
      1. Fetch each addressed-fragment JSON-LD (label, parent, ordering).
      2. Fetch each content-fragment JSON-LD (`text-fragmentu` HTML snippet).
    """
    frag_uris = version_meta.get("má-fragment-znění") or []
    if isinstance(frag_uris, str):
        frag_uris = [frag_uris]
    sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _fetch_addr(uri: str) -> tuple[str, dict | None]:
        full = uri if uri.startswith("http") else f"{_OPENDATA_BASE}/{uri}"
        async with sem:
            try:
                return uri, await _get_json(client, full)
            except Exception as exc:
                _log.warning("esb_addr_fetch_failed uri=%s error=%s", uri, repr(exc))
                return uri, None

    addr_results = await asyncio.gather(*[_fetch_addr(u) for u in frag_uris])

    # Build first-pass FragmentNodes; the body fetch is deferred.
    pending: list[tuple[FragmentNode, str | None]] = []
    for uri, data in addr_results:
        if data is None:
            continue
        path = _fragment_path_segs(uri)
        if not path:
            continue
        kind, designator = _split_kind(path[-1])
        parent_uri = data.get("má-předka")
        parent_path = _fragment_path_segs(parent_uri) if parent_uri else None
        label = data.get("l-sgov-dat-sbirka-pojem:citace-označení-fragmentu-znění-právního-aktu") or ""
        order_key = data.get("l-sgov-dat-sbirka-pojem:pořadí-fragmentu-znění-právního-aktu") or ""
        body_uri = data.get("obsahuje-fragment")
        node = FragmentNode(
            path=path,
            kind=kind,
            designator=designator,
            label=label,
            order_key=order_key,
            text_html="",
            parent_path=parent_path,
        )
        pending.append((node, body_uri))

    # Second pass: fetch content nodes for fragments that have them.
    async def _fetch_body(node: FragmentNode, body_uri: str) -> None:
        full = body_uri if body_uri.startswith("http") else f"{_OPENDATA_BASE}/{body_uri}"
        async with sem:
            try:
                data = await _get_json(client, full)
            except Exception as exc:
                _log.warning("esb_body_fetch_failed uri=%s error=%s", body_uri, repr(exc))
                return
        node.text_html = data.get("l-sgov-dat-sbirka-pojem:text-fragmentu") or ""

    body_tasks = [_fetch_body(n, u) for n, u in pending if u]
    if body_tasks:
        await asyncio.gather(*body_tasks)

    return [n for n, _ in pending]


# ---------------------------------------------------------------------------
# Top-level fetch helper
# ---------------------------------------------------------------------------
async def fetch_act(year: int, number: int, *, on_date: date | None = None) -> tuple[list[FragmentNode], dict, str]:
    """Fetch one act's fragments and return (nodes, version_meta_summary, version_path).

    The summary is the version metadata without the bulky fragment list, so callers can archive a
    small JSON manifest.

    `on_date` selects which consolidated version to fetch (the one whose účinnost window covers it);
    None means today. A future on_date pre-loads a not-yet-effective consolidation (e.g. ingest the
    2026-07-01 version today so it takes over exactly when the current one expires).
    """
    async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, limits=_FETCH_LIMITS, follow_redirects=True) as client:
        version_path, version_meta = await _resolve_version(client, year, number, on_date)
        nodes = await _fetch_fragment_graph(client, version_meta)
    _eff = _version_effect_date(version_path, version_meta)  # URI-suffix fallback for date-field-less acts
    summary = {
        "version_path": version_path,
        "ucinnost_od": _eff.isoformat() if _eff else None,
        "ucinnost_do": version_meta.get("účinnost-znění-do"),
        "typ_zneni": version_meta.get("má-typ-znění-právního-aktu"),
        "n_fragments_listed": len(version_meta.get("má-fragment-znění") or []),
        "n_fragments_fetched": len(nodes),
    }
    return nodes, summary, version_path


# ---------------------------------------------------------------------------
# Database insertion
# ---------------------------------------------------------------------------


def _node_dict(n: "FragmentNode") -> dict:
    return {
        "path": list(n.path),
        "kind": n.kind,
        "designator": n.designator,
        "label": n.label,
        "order_key": n.order_key,
        "text_html": n.text_html,
        "parent_path": list(n.parent_path) if n.parent_path else None,
    }


_RAW_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "data" / "raw" / "esbirka"


def _save_raw(year: int, number: int, blob: bytes) -> tuple[str, str]:
    _RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = _RAW_DIR / f"{year}-{number}.json"
    path.write_bytes(blob)
    return path.as_uri(), hashlib.sha256(blob).hexdigest()


async def _delete_existing(conn, external_id: str, *, keep_document: bool = False) -> None:
    """Delete an act's provisions and their cross-references.

    With keep_document=True (the non-destructive amend path) the source_document row is kept, so its id
    stays stable and the provision_version history (a foreign key to source_document with ON DELETE
    CASCADE) is not wiped. Initial ingest deletes the document too.
    """
    await conn.execute(
        """DELETE FROM cross_reference WHERE from_provision IN
               (SELECT p.id FROM provision p JOIN source_document sd ON sd.id=p.source_document_id
                WHERE sd.external_id=$1 AND sd.publisher='Sbírka zákonů')
           OR to_provision IN
               (SELECT p.id FROM provision p JOIN source_document sd ON sd.id=p.source_document_id
                WHERE sd.external_id=$1 AND sd.publisher='Sbírka zákonů')""",
        external_id,
    )
    await conn.execute(
        """DELETE FROM provision WHERE source_document_id IN
               (SELECT id FROM source_document WHERE external_id=$1 AND publisher='Sbírka zákonů')""",
        external_id,
    )
    if not keep_document:
        await conn.execute(
            "DELETE FROM source_document WHERE external_id=$1 AND publisher='Sbírka zákonů'", external_id
        )


async def ingest_act(
    spec: CzechActSpec, *, store: bool = True, amend: bool = False, on_date: date | None = None
) -> dict:
    """Fetch one Czech Sbírka act from e-Sbírka and, by default, store it in the database.

    store=False returns parsed counts without writing (a parse-fidelity dry run).

    amend=True is the non-destructive re-ingest used by refresh.py: the source_document row is kept
    (its id and provision_version history survive), only provisions are replaced, and the version
    history is reconciled. A changed provision closes its old version and appends a new one, so
    point-in-time history accumulates instead of being erased. Falls back to a fresh insert if the
    act is not stored yet.

    `on_date` selects the consolidated version to fetch (None means today). With amend=True and a
    future on_date this pre-loads the not-yet-effective version as the new head (valid_from = its
    účinnost), while ``provision_versioning.amend`` closes the prior head at that date, so the
    lineage is correct and the new text takes effect on its own date via the valid_from filter.
    """
    external_id = f"{spec.number}/{spec.year}"
    nodes, summary, version_path = await fetch_act(spec.year, spec.number, on_date=on_date)
    if not nodes:
        raise RuntimeError(f"no fragments fetched for {external_id}")
    provs, xrefs = walk_act(nodes, own_external_id=external_id)
    if not provs:
        raise RuntimeError(f"walker produced 0 provisions for {external_id}")
    if not store:
        return {
            "external_id": external_id,
            "n_provisions": len(provs),
            "n_cross_refs": len(xrefs),
            "version_path": version_path,
        }

    raw_blob = json.dumps(
        {"version_path": version_path, "summary": summary, "fragments": [_node_dict(n) for n in nodes]},
        ensure_ascii=False,
    ).encode("utf-8")
    raw_uri, raw_sha = _save_raw(spec.year, spec.number, raw_blob)
    now = datetime.now(UTC)
    meta = json.dumps(
        {
            "short": spec.short,
            "version_path": version_path,
            **{k: v for k, v in summary.items() if k != "version_path"},
        },
        ensure_ascii=False,
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            vf, vt = _parse_date(summary.get("ucinnost_od")), _parse_date(summary.get("ucinnost_do"))
            existing = (
                await conn.fetchval(
                    "SELECT id FROM source_document WHERE external_id=$1 AND publisher='Sbírka zákonů'", external_id
                )
                if amend
                else None
            )
            from pythia.ingest import provision_versioning as pvz

            prior_heads = await pvz.capture_heads(conn, external_id) if existing else {}
            await _delete_existing(conn, external_id, keep_document=bool(existing))
            if existing:  # amend in place: keep the document id and version history
                doc_id = existing
                await conn.execute(
                    """UPDATE source_document SET title=$2, raw_storage_uri=$3, raw_format='esbirka-jsonld',
                              raw_checksum=$4, fetched_at=$5, last_checked_at=$5, valid_from=$6, valid_to=$7,
                              status='ingested', metadata=$8::jsonb WHERE id=$1""",
                    doc_id,
                    spec.title,
                    raw_uri,
                    raw_sha,
                    now,
                    vf,
                    vt,
                    meta,
                )
            else:
                doc_id = await conn.fetchval(
                    """INSERT INTO source_document
                           (external_id, title, publisher, jurisdiction, legal_hierarchy, document_type,
                            language, raw_storage_uri, raw_format, raw_checksum, fetched_at, last_checked_at,
                            valid_from, valid_to, status, metadata)
                       VALUES ($1,$2,'Sbírka zákonů','CZ',$3,$4,'cs',$5,'esbirka-jsonld',$6,$7,$7,$8,$9,'ingested',$10::jsonb)
                       RETURNING id""",
                    external_id,
                    spec.title,
                    spec.legal_hierarchy,
                    spec.document_type,
                    raw_uri,
                    raw_sha,
                    now,
                    vf,
                    vt,
                    meta,
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
                        p.hierarchy_path,
                        p.ordinal,
                        p.label,
                        p.eid,
                        p.text,
                        score_quality(p.text),
                        hashlib.sha256(p.text.encode("utf-8")).hexdigest(),
                        spec.legal_hierarchy,
                        json.dumps({"source_eid": p.eid}),
                    )
                )
            await conn.executemany(
                """INSERT INTO provision
                       (id, source_document_id, hierarchy_path, ordinal, label, eid, text, text_format,
                        ingestion_quality, text_hash, language, legal_hierarchy, jurisdiction, metadata)
                   VALUES ($1,$2,$3::ltree,$4,$5,$6,$7,'gfm',$8,$9,'cs',$10,'CZ',$11::jsonb)""",
                prov_params,
            )
            ref_params = []
            for xref in xrefs:
                from_id = eid_to_id.get(xref.from_eid)
                if not from_id:
                    continue
                to_id = None
                if xref.ref_type == "internal":
                    target = xref.to_external_ref.replace(".", "_")
                    matches = [eid for eid in eid_to_id if eid.endswith(target)]
                    if len(matches) == 1:
                        to_id = eid_to_id[matches[0]]
                ref_params.append(
                    (
                        from_id,
                        to_id,
                        None if to_id else xref.to_external_ref,
                        xref.ref_type,
                        xref.detected_by,
                        xref.confidence,
                        json.dumps(xref.metadata or {}),
                    )
                )
            if ref_params:
                await conn.executemany(
                    """INSERT INTO cross_reference
                           (from_provision, to_provision, to_external_ref, ref_type, detected_by,
                            confidence, metadata, detected_at)
                       VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb, now())""",
                    ref_params,
                )
            amend_res = None
            if existing:  # reconcile version history (close and append)
                amend_res = await pvz.amend(
                    conn, source_document_id=doc_id, new_effect_date=vf, prior_heads=prior_heads
                )
                # Rebuild the provenance bundle so it tracks the updated columns (valid_from, valid_to,
                # raw_checksum, fetched_at) and re-derives temporal_status; otherwise the bundle keeps the
                # pre-amendment values and drifts from the source_document row.
                await pvz.rebuild_provenance(conn, doc_id=doc_id, valid_from=vf, recompute_status=True)
    return {
        "external_id": external_id,
        "source_document_id": str(doc_id),
        "n_provisions": len(provs),
        "n_cross_refs": len(ref_params),
        "version_path": version_path,
        "amend": amend_res,
    }


async def _ingest_all(act_keys: list[str] | None = None) -> list[dict]:
    targets = MVP_ACTS
    if act_keys:
        wanted = set(act_keys)
        targets = tuple(a for a in MVP_ACTS if f"{a.number}/{a.year}" in wanted)
    results = []
    for spec in targets:
        try:
            r = await ingest_act(spec)
            print(
                f"OK  {spec.number}/{spec.year}  {spec.short:12s}  "
                f"provisions={r['n_provisions']:>4d}  xrefs={r['n_cross_refs']:>4d}  ({r['version_path']})"
            )
            results.append(r)
        except Exception as exc:  # noqa: BLE001
            print(f"ERR {spec.number}/{spec.year}  {spec.short:12s}  {exc!r}")
    return results


def main() -> None:
    p = argparse.ArgumentParser(description="Ingest e-Sbírka acts into Pythia's DB.")
    p.add_argument("--act", action="append", default=None, help="act IDs NUMBER/YEAR (omit for MVP set)")
    args = p.parse_args()
    asyncio.run(_ingest_all(args.act))


if __name__ == "__main__":
    main()
