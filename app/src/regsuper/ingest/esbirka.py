r"""e-Sbírka (Collection of Laws) ingester.

Fetches consolidated Czech acts as Linked Data from `opendata.eselpoint.gov.cz`, rebuilds each
act's fragment tree and writes one GFM Markdown provision row per section (`par_*` fragment).
The source data model and the Markdown mapping are described in the comments below.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import httpx
from lxml import html as lhtml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from regsuper.ingest.storage import save_raw
from regsuper.observability.events import emit
from regsuper.observability.logging_config import get_logger
from regsuper.observability.resource_guard import gate_storage
from regsuper.storage.audit import log_event
from regsuper.storage.database import session_scope
from regsuper.storage.models import CrossReference, Provision, SourceDocument

_log = get_logger(__name__)

# Source data model
#
# Each act is identified by its ELI:
#
#     https://opendata.eselpoint.gov.cz/esel-esb/eli/cz/sb/{year}/{number}
#
# The act node references one or more versions (`znění`), each scoped to a date range
# (`účinnost-znění-od` to `účinnost-znění-do`). A version lists flat fragment URIs
# (`má-fragment-znění`) which, with their parent and ordering attributes, rebuild the tree:
#
#     cast_N / hlava_M / dil_K / oddil_J / par_X / odst_Y / pism_Z / bod_W
#
# Each addressed fragment references a content fragment (`obsahuje-fragment`) whose
# `text-fragmentu` holds that node's literal text as a small HTML snippet. A section's full text
# is its own snippet followed by its descendants' snippets in document order.
#
# Markdown mapping (provision.text is GFM; see amendments/01-markdown-ingestion.md):
#
#     | Fragment kind | Markdown                                          |
#     |---------------|---------------------------------------------------|
#     | `cast`        | `# Část N — {název}` (ancestor, not a row)        |
#     | `hlava`       | `## Hlava N — {název}` (ancestor)                 |
#     | `dil`         | `### Díl N — {název}` (ancestor)                  |
#     | `oddil`       | `#### Oddíl N` (ancestor)                         |
#     | `par`         | `##### § N`, one provision row each               |
#     | `odst`        | `**(N)**` inline at the start of a body paragraph |
#     | `pism`        | `a) …` list item                                  |
#     | `bod`         | `1. …` numbered list item                         |
#
# A section row holds its heading line plus the descendant odst, pism and bod tree rendered
# inline. Ancestor headings live in `hierarchy_path` (ltree), from which the source viewer
# rebuilds the full breadcrumb at render time.


# ---------------------------------------------------------------------------
# Target acts
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
    CzechActSpec(
        2004, 256, "ZPKT", "Zákon č. 256/2004 Sb., o podnikání na kapitálovém trhu", "cz-act", "act"
    ),
    CzechActSpec(2012, 89, "Civil Code", "Zákon č. 89/2012 Sb., občanský zákoník", "cz-act", "act"),
    CzechActSpec(
        2008,
        253,
        "AML Act",
        "Zákon č. 253/2008 Sb., o některých opatřeních proti legalizaci výnosů z trestné činnosti",
        "cz-act",
        "act",
    ),
    CzechActSpec(
        2012,
        90,
        "BCA",
        "Zákon č. 90/2012 Sb., o obchodních společnostech a družstvech",
        "cz-act",
        "act",
    ),
)


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------
_OPENDATA_BASE = "https://opendata.eselpoint.gov.cz"
_USER_AGENT = "regsuper-mvp/0.0.1 (+https://example.invalid)"
_ACCEPT_LD_JSON = "application/ld+json"
_FETCH_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_FETCH_LIMITS = httpx.Limits(max_connections=64, max_keepalive_connections=32)
_FETCH_CONCURRENCY = 32  # eselpoint is a small public endpoint


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
# Walker: pure functions over pre-fetched nodes
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
    detected_by: str  # 'esbirka-anchor' | 'regex'
    confidence: float


_SEG_KIND_RE = re.compile(r"^([a-z]+)_(.*)$")


def _split_kind(seg: str) -> tuple[str, str]:
    """Split a segment such as 'par_506a' into ('par', '506a'); return (seg, '') if no match."""
    m = _SEG_KIND_RE.match(seg)
    return (m.group(1), m.group(2)) if m else (seg, "")


# ---------------------------------------------------------------------------
# HTML to inline Markdown conversion, applied to each text-fragmentu snippet
# ---------------------------------------------------------------------------
_VAR_LABEL_RE = re.compile(r"^\s*<var[^>]*>(.*?)</var>\s*", flags=re.DOTALL | re.IGNORECASE)


def _strip_leading_var(html_snippet: str) -> tuple[str, str]:
    """Pull the inline `<var>label</var>` off the front of a snippet.

    The label ("§ N", "(1)", "a)") is captured separately in `FragmentNode.label`, so the body
    is rendered without it. Returns (label_text, rest_html).
    """
    m = _VAR_LABEL_RE.match(html_snippet or "")
    if not m:
        return "", html_snippet or ""
    label = (
        re.sub(
            r"\s+",
            " ",
            lhtml.fragment_fromstring(f"<span>{m.group(1)}</span>", create_parent="span")
            .text_content()
            .strip(),
        )
        if m.group(1)
        else ""
    )
    rest = html_snippet[m.end() :]
    return label, rest


def _html_to_markdown_inline(snippet: str) -> str:
    """Convert a small HTML fragment to inline Markdown.

    Handles `<a>`, `<var>`, `<i>`, `<b>`, `<strong>`, `<em>`, `<sup>`, `<sub>`, `<br>`, `<p>`
    and `<div>`; anything else is rendered as its text content.
    """
    if not snippet or not snippet.strip():
        return ""
    # lxml.html needs a wrapper element for fragments.
    try:
        node = lhtml.fragment_fromstring(snippet, create_parent="div")
    except Exception:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", snippet)).strip()
    out: list[str] = []
    _walk_inline(node, out)
    text = "".join(out)
    # Collapse runs of spaces and trim, keeping intentional newlines from <br>.
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text


def _walk_inline(el, out: list[str]) -> None:
    """Append the Markdown rendering of `el`'s children into `out`."""
    if el.text:
        out.append(el.text)
    for child in el:
        tag = (child.tag or "").lower() if isinstance(child.tag, str) else ""
        if tag == "var":
            # The leading label var is already stripped. A var mid-text is unusual but occurs
            # with nested numbering; render it bold.
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
                # Internal cross-reference: the anchor target (data-odkaz-id) is opaque, so
                # emit only the text. cross_reference rows come from the regex pass instead.
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
# Section (paragraf) renderer
# ---------------------------------------------------------------------------
def _render_paragraf(
    par: FragmentNode,
    children_index: dict[tuple[str, ...], list[FragmentNode]],
) -> str:
    """Render one `par_*` fragment plus its descendant tree as a GFM block."""
    # Heading line: `##### ` plus the citation label, or `§ N` when there is none.
    heading = par.label or f"§ {par.designator}"
    lines: list[str] = [f"##### {heading}"]

    # Body: render the descendants in document order.
    sub_lines = _render_children(par, children_index, base_indent=0)
    if sub_lines:
        lines.append("")
        lines.extend(sub_lines)
    else:
        # Some sections carry their text directly, with no odst, pism or bod children.
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
    """Render `parent`'s direct children and their descendants as Markdown lines.

    Line shapes, indented by two spaces per nesting level:
      - odst: `**(N)** body…`
      - pism: `a) body…`
      - bod: `1. body…`
      - frag: plain paragraph
    """
    direct = children_index.get(parent.path, [])
    if not direct:
        return []
    lines: list[str] = []
    indent = "  " * base_indent
    for ch in direct:
        _, body_html = _strip_leading_var(ch.text_html)
        body_md = _html_to_markdown_inline(body_html)
        # Re-flow newlines so list items do not break across Markdown blocks.
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
            # Unknown sub-kind under a section: render it as a plain paragraph.
            if body_inline:
                lines.append(f"{indent}{body_inline}")

        nested = _render_children(ch, children_index, base_indent=base_indent + 1)
        if nested:
            lines.extend(nested)
    return lines


# ---------------------------------------------------------------------------
# Walker entry point: turn the fetched fragment graph into provision drafts
# ---------------------------------------------------------------------------
_INT_REF = re.compile(
    r"§\s*(\d+[a-z]?)(?:\s*odst\.\s*(\d+))?(?:\s*písm\.\s*([a-z]))?", flags=re.IGNORECASE
)
_EXT_ACT = re.compile(r"(?:zákon|vyhlášk|nařízen)\w*\s+č\.\s*(\d+/\d+\s*Sb\.)", flags=re.IGNORECASE)


def walk_act(nodes: list[FragmentNode]) -> tuple[list[ProvisionDraft], list[CrossRefDraft]]:
    """Convert a flat fragment list into provision and cross-reference drafts."""
    children: dict[tuple[str, ...], list[FragmentNode]] = defaultdict(list)
    for n in nodes:
        if n.parent_path is not None:
            children[n.parent_path].append(n)
    for plist in children.values():
        plist.sort(key=lambda x: x.order_key)

    pars = [n for n in nodes if n.kind == "par"]
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
                label=par.label or f"§ {par.designator}",
                hierarchy_path=hierarchy_path,
                ordinal=ordinal,
                text=md,
                parent_eid=None,
            )
        )
        _mine_xrefs(md, eid, xrefs)
    return provisions, xrefs


def _mine_xrefs(text: str, from_eid: str, sink: list[CrossRefDraft]) -> None:
    """Extract internal § references and external Sbírka act citations."""
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
    for m in _EXT_ACT.finditer(text):
        ref = m.group(0)
        key = f"ext:{ref}"
        if key in seen:
            continue
        seen.add(key)
        sink.append(
            CrossRefDraft(
                from_eid=from_eid,
                to_external_ref=ref,
                ref_type="external",
                detected_by="regex",
                confidence=0.55,
            )
        )


# ---------------------------------------------------------------------------
# Quality scoring
# ---------------------------------------------------------------------------
def score_quality(provision_md: str) -> float:
    """Return a heuristic quality score from 0 to 1, comparable to fmx4.score_quality.

    e-Sbírka is structured Linked Data, so the baseline is 1.0 and points are deducted only for
    observable problems (replacement characters, suspiciously short bodies).
    """
    score = 1.0
    if not provision_md or len(provision_md) < 60:
        score -= 0.3
    if "\ufffd" in provision_md:
        score -= 0.3
    # Heading-only sections (no body) hint at missing sub-fragments.
    if provision_md.count("\n") < 1:
        score -= 0.2
    return max(0.0, score)


# ---------------------------------------------------------------------------
# Fetcher: resolve act, then version, then fragments and their content snippets
# ---------------------------------------------------------------------------
def _short_path(uri: str) -> str:
    """Strip the opendata base URL: `https://.../esel-esb/eli/...` becomes `esel-esb/eli/...`."""
    if uri.startswith(_OPENDATA_BASE + "/"):
        return uri[len(_OPENDATA_BASE) + 1 :]
    return uri


def _fragment_path_segs(frag_uri: str) -> tuple[str, ...] | None:
    """Extract the (cast_N, hlava_M, …) tuple from a fragment URI.

    Returns None for non-norma URIs (e.g. /preambule, /postface).
    """
    short = _short_path(frag_uri)
    marker = "/dokument/norma/"
    if marker not in short:
        return None
    tail = short.split(marker, 1)[1]
    segs = tuple(p for p in tail.split("/") if p)
    return segs or None


async def _resolve_version(
    client: httpx.AsyncClient, year: int, number: int, on_date: date | None
) -> tuple[str, dict]:
    """Pick the act version whose `účinnost-znění` range covers `on_date` (default: today).

    Returns (version_short_path, version_json).
    """
    on_date = on_date or date.today()
    act_url = f"{_OPENDATA_BASE}/esel-esb/eli/cz/sb/{year}/{number}"
    act = await _get_json(client, act_url)
    raw_versions = act.get("má-znění") or []
    if isinstance(raw_versions, str):
        raw_versions = [raw_versions]
    if not raw_versions:
        raise RuntimeError(f"act {year}/{number} has no versions")

    # Fetch each version's metadata concurrently; each document is small (under 2 KB).
    async def _vmeta(uri: str) -> tuple[str, dict]:
        full = uri if uri.startswith("http") else f"{_OPENDATA_BASE}/{uri}"
        return uri, await _get_json(client, full)

    metas = await asyncio.gather(*[_vmeta(u) for u in raw_versions])

    # Some acts (e.g. 89/2012) ship version nodes without `účinnost-znění-od`; the date is
    # then taken from the URI suffix.
    def _od_for(uri: str, meta: dict) -> date | None:
        d = _parse_date(meta.get("účinnost-znění-od"))
        if d is not None:
            return d
        m = re.search(r"/(\d{4}-\d{2}-\d{2})$", uri)
        return _parse_date(m.group(1)) if m else None

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
        # Fall back to the most recent past version regardless of validity.
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


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


async def _fetch_fragment_graph(
    client: httpx.AsyncClient, version_meta: dict
) -> list[FragmentNode]:
    """Resolve every fragment URI listed in `version_meta` into a FragmentNode.

    Works in two passes: first each addressed fragment's JSON-LD (label, parent, ordering),
    then each content fragment's JSON-LD (the `text-fragmentu` HTML snippet).
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
                _log.warning("esb_addr_fetch_failed", uri=uri, error=repr(exc))
                return uri, None

    addr_results = await asyncio.gather(*[_fetch_addr(u) for u in frag_uris])

    # Build the FragmentNodes now and fetch their bodies in the second pass.
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
        label = (
            data.get("l-sgov-dat-sbirka-pojem:citace-označení-fragmentu-znění-právního-aktu") or ""
        )
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
                _log.warning("esb_body_fetch_failed", uri=body_uri, error=repr(exc))
                return
        node.text_html = data.get("l-sgov-dat-sbirka-pojem:text-fragmentu") or ""

    body_tasks = [_fetch_body(n, u) for n, u in pending if u]
    if body_tasks:
        await asyncio.gather(*body_tasks)

    return [n for n, _ in pending]


# ---------------------------------------------------------------------------
# Top-level fetch helper
# ---------------------------------------------------------------------------
async def fetch_act(year: int, number: int) -> tuple[list[FragmentNode], dict, str]:
    """Fetch the fragment graph of an act's current version.

    Returns (nodes, version_meta_summary, version_path). The summary omits the bulky fragment
    list so callers can archive a small JSON manifest.
    """
    async with httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT, limits=_FETCH_LIMITS, follow_redirects=True
    ) as client:
        version_path, version_meta = await _resolve_version(client, year, number, None)
        nodes = await _fetch_fragment_graph(client, version_meta)
    summary = {
        "version_path": version_path,
        "ucinnost_od": version_meta.get("účinnost-znění-od"),
        "ucinnost_do": version_meta.get("účinnost-znění-do"),
        "typ_zneni": version_meta.get("má-typ-znění-právního-aktu"),
        "n_fragments_listed": len(version_meta.get("má-fragment-znění") or []),
        "n_fragments_fetched": len(nodes),
    }
    return nodes, summary, version_path


# ---------------------------------------------------------------------------
# Database insertion
# ---------------------------------------------------------------------------
async def ingest_act(spec: CzechActSpec, *, session: AsyncSession | None = None) -> dict:
    """Ingest one act from the Czech Collection of Laws end to end."""
    own_session = session is None
    ctx = session_scope() if own_session else _passthrough(session)
    async with ctx as sess:
        return await _ingest_act_inner(spec, sess)


class _passthrough:
    def __init__(self, sess: AsyncSession):
        self._sess = sess

    async def __aenter__(self) -> AsyncSession:
        return self._sess

    async def __aexit__(self, *_exc) -> None:
        return None


async def _ingest_act_inner(spec: CzechActSpec, session: AsyncSession) -> dict:
    external_id = f"{spec.number}/{spec.year}"
    await emit(
        "ingest.document.started",
        publisher="Sbírka zákonů",
        external_id=external_id,
        name=spec.short,
    )

    nodes, summary, version_path = await fetch_act(spec.year, spec.number)
    if not nodes:
        raise RuntimeError(f"no fragments fetched for {external_id}")

    # Persist the version manifest, with every parsed fragment, as the raw artefact; it is the
    # source of truth for re-walks. The individual JSON-LD responses are not stored.
    raw_blob = json.dumps(
        {
            "version_path": version_path,
            "summary": summary,
            "fragments": [_node_dict(n) for n in nodes],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    # Refuse before the raw write if it would push free disk below the floor: a clean refusal
    # is better than a disk-full crash halfway through ingestion.
    await gate_storage(required_bytes=len(raw_blob))
    artefact = save_raw(
        "esbirka",
        f"{spec.year}-{spec.number}",
        "json",
        raw_blob,
    )

    provs, xrefs = walk_act(nodes)
    if not provs:
        raise RuntimeError(f"walker produced 0 provisions for {external_id}")

    await _delete_existing(session, external_id)

    sd = SourceDocument(
        external_id=external_id,
        title=spec.title,
        publisher="Sbírka zákonů",
        jurisdiction="CZ",
        legal_hierarchy=spec.legal_hierarchy,
        document_type=spec.document_type,
        language="cs",
        raw_storage_uri=artefact.uri,
        raw_format="esbirka-jsonld",
        raw_checksum=artefact.sha256,
        fetched_at=datetime.now(UTC),
        last_checked_at=datetime.now(UTC),
        valid_from=_parse_date(summary.get("ucinnost_od")),
        valid_to=_parse_date(summary.get("ucinnost_do")),
        status="ingested",
        metadata_={
            "short": spec.short,
            "version_path": version_path,
            **{k: v for k, v in summary.items() if k != "version_path"},
        },
    )
    session.add(sd)
    await session.flush()  # need sd.id

    # Every e-Sbírka provision is a section row, so parent_eid is always None. A single batch
    # insert keeps acts with thousands of rows fast.
    eid_to_id: dict[str, UUID] = {}
    rows: list[Provision] = []
    for p in provs:
        row_id = uuid4()
        eid_to_id[p.eid] = row_id
        rows.append(_provision_row(p, sd, row_id))
    session.add_all(rows)
    await session.flush()

    # Cross-references: only internal references resolve against this act's eids; the rest
    # stay as `to_external_ref` text for a later resolution pass.
    ref_inserts = 0
    for xref in xrefs:
        from_id = eid_to_id.get(xref.from_eid)
        if not from_id:
            continue
        # The walker encodes to_external_ref as e.g. 'par_506.odst_2'. Match it as a suffix of
        # the full eid, which also carries the cast and hlava prefixes. Exactly one match
        # resolves the reference; otherwise it stays as text.
        to_id = None
        if xref.ref_type == "internal":
            target = xref.to_external_ref.replace(".", "_")
            matches = [eid for eid in eid_to_id if eid.endswith(target)]
            if len(matches) == 1:
                to_id = eid_to_id[matches[0]]
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
        actor_id="ingest.esbirka",
        action="ingest.document.complete",
        target_type="source_document",
        target_id=str(sd.id),
        payload={
            "external_id": external_id,
            "publisher": "Sbírka zákonů",
            "n_provisions": len(provs),
            "n_cross_refs": ref_inserts,
            "raw_uri": artefact.uri,
            "checksum": artefact.sha256,
            "version": version_path,
        },
    )
    result = {
        "external_id": external_id,
        "source_document_id": str(sd.id),
        "n_provisions": len(provs),
        "n_cross_refs": ref_inserts,
        "raw_uri": artefact.uri,
        "version_path": version_path,
    }
    await emit(
        "ingest.document.complete",
        publisher="Sbírka zákonů",
        external_id=external_id,
        n_provisions=len(provs),
    )
    return result


def _node_dict(n: FragmentNode) -> dict:
    return {
        "path": list(n.path),
        "kind": n.kind,
        "designator": n.designator,
        "label": n.label,
        "order_key": n.order_key,
        "text_html": n.text_html,
        "parent_path": list(n.parent_path) if n.parent_path else None,
    }


def _provision_row(p: ProvisionDraft, sd: SourceDocument, row_id: UUID) -> Provision:
    return Provision(
        id=row_id,
        source_document_id=sd.id,
        parent_id=None,
        hierarchy_path=p.hierarchy_path,
        ordinal=p.ordinal,
        label=p.label,
        eid=p.eid,
        text=p.text,
        text_format="gfm",
        ingestion_quality=score_quality(p.text),
        text_hash=hashlib.sha256(p.text.encode("utf-8")).hexdigest(),
        language=sd.language,
        legal_hierarchy=sd.legal_hierarchy,
        jurisdiction=sd.jurisdiction,
        metadata_={"source_eid": p.eid},
    )


async def _delete_existing(session: AsyncSession, external_id: str) -> None:
    existing = (
        (
            await session.execute(
                select(SourceDocument).where(
                    SourceDocument.external_id == external_id,
                    SourceDocument.publisher == "Sbírka zákonů",
                )
            )
        )
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
async def _ingest_all(act_keys: list[str] | None = None) -> list[dict]:
    targets = MVP_ACTS
    if act_keys:
        wanted = set(act_keys)
        targets = tuple(a for a in MVP_ACTS if f"{a.number}/{a.year}" in wanted)
        if len(targets) != len(wanted):
            missing = wanted - {f"{a.number}/{a.year}" for a in MVP_ACTS}
            raise SystemExit(f"unknown act IDs: {sorted(missing)}")
    results: list[dict] = []
    for spec in targets:
        try:
            r = await ingest_act(spec)
            print(
                f"OK  {spec.number}/{spec.year}  {spec.short:12s}  "
                f"provisions={r['n_provisions']:>4d}  xrefs={r['n_cross_refs']:>4d}  "
                f"({r['version_path']})"
            )
            results.append(r)
        except Exception as exc:
            print(f"ERR {spec.number}/{spec.year}  {spec.short:12s}  {exc!r}")
            results.append({"external_id": f"{spec.number}/{spec.year}", "error": repr(exc)})
    return results


def main() -> None:
    from regsuper.observability.logging_config import configure_logging

    configure_logging()
    p = argparse.ArgumentParser(description="Ingest e-Sbírka acts into the regsuper DB.")
    p.add_argument(
        "--act",
        action="append",
        default=None,
        help="One or more act IDs in NUMBER/YEAR form (omit for the MVP set).",
    )
    args = p.parse_args()
    asyncio.run(_ingest_all(args.act))


def iter_fragments(nodes: Iterable[FragmentNode]) -> Iterable[FragmentNode]:
    return iter(nodes)


if __name__ == "__main__":
    main()
