"""Evidence bundle and citation resolver.

Builds one `EvidenceBundle`: each selected provision's id, full text and act/§ locator. The citation
verifier, the practical and fact passes, the section splitter and the draft all read this one
carrier, so a cited § always resolves to the same text, and every span a consumer emits is an index
into it (`value_raw == bundle.text_for(pid)[start:end]`). The module also resolves the draft's
citations to bundle provisions. Used only when `evidence_bundle_enabled` is on; importing it has no
side effects.
"""

from __future__ import annotations

from pythia.config import get_settings
from pythia.contract import EvidenceBundle, EvidenceProvision, ResolvedCitation
from pythia.ports import NexusPort
from pythia.reasoning.verify import _EXT_RE, _SEC_RE, _SPAN_RE, _sec_num


def _act_of(ext: str | None) -> str:
    return (ext or "").strip()


def _norm_sec(sec: str | None) -> str | None:
    """Normalize a § number to the grain verify.py grounds on (lower-cased); None stays None.

    The paragraph suffix has already been dropped by `_sec_num` / `_SEC_RE`.
    """
    return sec.lower() if sec else None


def _label_sec(label: str | None) -> str | None:
    """Return the § number a provision label carries ("§ 297" gives "297"), via `_sec_num`."""
    return _sec_num(label)


def parse_citations(draft: str | None) -> list[ResolvedCitation]:
    """Parse every citation in the draft, unresolved, using the citation grammar from verify.py.

    Sharing verify.py's grammar keeps the resolver and the grounding verdict in agreement on what a
    citation is. A bracketed `[act § N]` yields (act, section); a bare `§ N` or `Article N` outside
    a bracket that already named an act yields (None, section). Each result carries its draft
    character span and verbatim marker text with resolved=False; `resolve_against` fills in the
    resolution. Pure; an empty draft gives [].
    """
    text = draft or ""
    out: list[ResolvedCitation] = []
    bracket_spans: list[tuple[int, int]] = []
    seen_bracket: set[tuple[str, str]] = set()
    seen_bare: set[str] = set()
    # 1. Bracketed, act-qualified citations.
    for sm in _SPAN_RE.finditer(text):
        span = sm.group(1) if sm.group(1) is not None else sm.group(2)
        em, secm = _EXT_RE.search(span), _SEC_RE.search(span)
        if not (em and secm):
            continue
        bracket_spans.append((sm.start(), sm.end()))
        act, sec = em.group(1).strip(), secm.group(1).lower()
        if (act, sec) in seen_bracket:
            continue
        seen_bracket.add((act, sec))
        out.append(
            ResolvedCitation(
                act=act, section=sec, draft_start=sm.start(), draft_end=sm.end(), label=(sm.group(0) or "").strip()
            )
        )

    # 2. Bare § / Article citations in prose that are not inside an already counted bracket.
    def _inside_bracket(pos: int) -> bool:
        return any(s <= pos < e for s, e in bracket_spans)

    for secm in _SEC_RE.finditer(text):
        if _inside_bracket(secm.start()):
            continue
        sec = secm.group(1).lower()
        if sec in seen_bare or sec in {s for _, s in seen_bracket}:
            continue
        seen_bare.add(sec)
        out.append(
            ResolvedCitation(
                act=None,
                section=sec,
                draft_start=secm.start(),
                draft_end=secm.end(),
                label=(secm.group(0) or "").strip(),
            )
        )
    return out


def _index(provisions: list[EvidenceProvision]) -> tuple[dict[tuple[str, str], str], dict[str, list[str]]]:
    """Build the resolver's lookup tables over the carrier rows: (act, §) to pid, and § to [pids].

    Mirrors `verify.ground`'s by_act_sec / sec_to_acts so resolution agrees with the grounding
    verdict.
    """
    by_act_sec: dict[tuple[str, str], str] = {}
    sec_to_pids: dict[str, list[str]] = {}
    for p in provisions:
        sec = _label_sec(p.label)
        if not sec:
            continue
        sec_to_pids.setdefault(sec, []).append(p.provision_id)
        act = _act_of(p.ext)
        if act:
            by_act_sec.setdefault((act, sec), p.provision_id)
    return by_act_sec, sec_to_pids


def _clause_snap(pid: str, by_id: dict[str, EvidenceProvision]) -> tuple[str, str | None]:
    """Snap a parent-§ resolution to a more specific sub-§ or clause row when one is present.

    A sub-§ row's parent_id points at the parent and its label extends the parent's, for example
    "§ 297 odst. 2" under "§ 297". The snap happens only when exactly one bundle row has this
    provision as its `source_pid` (a promoted child) or is a structural child of it (see
    `_child_of`). An ambiguous or absent child leaves the parent resolution unchanged, so a citation
    is never narrowed wrongly.

    Pure: reads only rows already in the bundle; sub-§ rows were promoted at build time, so there is
    no database call.

    Returns:
        (resolved_pid, snapped_from), where snapped_from is the parent pid if a snap happened, else
        None.
    """
    children = [
        p for p in by_id.values() if p.source_pid == pid or (p.provision_id != pid and _child_of(p, pid, by_id))
    ]
    if len(children) == 1:
        return children[0].provision_id, pid
    return pid, None


def _child_of(cand: EvidenceProvision, parent_pid: str, by_id: dict[str, EvidenceProvision]) -> bool:
    """Return True if `cand`'s label strictly extends the parent's label and has the same § number.

    A conservative structural child test that needs no extra fetch: the labels are already on the
    rows.
    """
    parent = by_id.get(parent_pid)
    if parent is None or not parent.label or not cand.label:
        return False
    ps, cs = _label_sec(parent.label), _label_sec(cand.label)
    if not ps or ps != cs:
        return False
    return len(cand.label) > len(parent.label) and cand.label.startswith(parent.label)


def resolve_against(cites: list[ResolvedCitation], provisions: list[EvidenceProvision]) -> list[ResolvedCitation]:
    """Resolve each parsed citation to a bundle provision id, clause-snapping where possible.

    A bracketed (act, §) resolves on the exact key. A bare § resolves only when exactly one act in
    the bundle carries it, mirroring `verify.ground`'s resolve-or-drop rule so a citation is never
    linked to the wrong act. A resolved parent § is then clause-snapped. Unresolved citations are
    returned with resolved=False so the citation verifier can demote them. Pure.
    """
    by_act_sec, sec_to_pids = _index(provisions)
    by_id = {p.provision_id: p for p in provisions}
    out: list[ResolvedCitation] = []
    for c in cites:
        pid: str | None = None
        if c.act and c.section:
            pid = by_act_sec.get((_act_of(c.act), c.section))
        elif c.section:
            pids = sec_to_pids.get(c.section, [])
            # A bare § resolves only when exactly one act carries it; otherwise it is ambiguous.
            if len({by_id[p].ext for p in pids if p in by_id}) == 1 and pids:
                pid = pids[0]
        if pid is None:
            out.append(c)  # unresolved: reported, never dropped
            continue
        snapped_pid, snapped_from = _clause_snap(pid, by_id)
        out.append(c.model_copy(update={"resolved": True, "provision_id": snapped_pid, "snapped_from": snapped_from}))
    return out


def _neighbor_sections(sec: str, fanout: int) -> list[str]:
    """Return the § numbers up to `fanout` below and above `sec`, nearest first, excluding `sec`.

    Only the numeric part counts ("297a" has base 297). Pure.
    """
    digits = "".join(ch for ch in sec if ch.isdigit())
    if not digits:
        return []
    base = int(digits)
    out: list[str] = []
    for d in range(1, fanout + 1):
        if base - d > 0:
            out.append(str(base - d))
        out.append(str(base + d))
    return out


async def _promote_neighbors(
    port: NexusPort, unresolved: list[ResolvedCitation], have: set[str], fanout: int, cap: int
) -> list[str]:
    """Look up the §s of unresolved citations, and their neighbors, in each act's § tree.

    For every unresolved citation that names an act, looks up the cited § and its neighbors (up to
    `fanout` either side) via `NexusPort.act_tree` and returns the ids not already in `have`, capped
    at `cap`. `act_tree` is called at most once per distinct act. Bounded, pure selection (no LLM,
    clock or randomness); returns [] when nothing new resolves.
    """
    wanted_by_act: dict[str, set[str]] = {}
    for c in unresolved:
        if not c.act or not c.section:
            continue
        secs = {c.section, *(_neighbor_sections(c.section, fanout))}
        wanted_by_act.setdefault(_act_of(c.act), set()).update(secs)
    promote: list[str] = []
    for act, secs in wanted_by_act.items():
        if len(promote) >= cap:
            break
        try:
            tree = await port.act_tree(act)
        except Exception:  # bad external id or missing act: skip, never crash
            continue
        for node in tree.get("nodes", []) or []:
            nid, nlabel = node.get("id"), node.get("label")
            if not nid or nid in have or nid in promote:
                continue
            if _label_sec(nlabel) in secs:
                promote.append(nid)
                if len(promote) >= cap:
                    break
    return promote


async def build_bundle(
    draft: str | None, carrier_meta: dict[str, dict], ordered_ids: list[str], *, port: NexusPort | None = None
) -> EvidenceBundle:
    """Build the evidence bundle for a draft, once.

    1. Carrier: the selected rows answer._fetch already fetched, so the base carrier costs no extra
       database call. The text is `meta['full_txt']` (the widest text, present when keep_full is
       on), else `meta['txt']` (the 700-character slice).
    2. Parse the draft's citations and resolve them against the carrier.
    3. Neighbor promotion: for act-qualified citations that are still unresolved, add the cited §
       and its immediate neighbors via `NexusPort.act_tree` and `NexusPort.fetch_provisions`,
       bounded by `evidence_neighbor_fanout` and `evidence_max_promoted`. Nexus is read only through
       NexusPort, never with raw SQL (the port-boundary test enforces this).
    4. Re-resolve against the widened carrier, so a citation of a parent § can snap to a promoted
       clause.

    An empty selection yields an empty bundle; a promotion error degrades to the unpromoted bundle.

    Args:
        draft: The drafted answer text.
        carrier_meta: answer._fetch's meta dict `{pid: {ext, label, txt, full_txt?, ...}}`.
        ordered_ids: The top-k selection in retrieval-rank order; these rows lead the bundle.
        port: Optional NexusPort for neighbor promotion (injected in tests); one is created only
            when promotion is needed.

    Returns:
        An EvidenceBundle with the selection rows first, then any promoted rows, plus the resolution
        of every citation (unresolved ones reported, never dropped).
    """
    s = get_settings()
    # Cap the internal full-text carrier so one huge provision cannot push unbounded text into the
    # verifiers. This text is never sent to the client (the JSON field serializer omits it).
    _cap = max(1, int(getattr(s, "verify_fulltext_cap", 20000)))
    # 1. Carrier from the already-fetched rows (no database call).
    provisions: list[EvidenceProvision] = []
    have: set[str] = set()
    for pid in ordered_ids:
        m = carrier_meta.get(pid)
        if not m:
            continue
        text = m.get("full_txt")
        if not isinstance(text, str) or not text:
            text = m.get("txt") or ""
        # Carry the context-window clause-snap labels when _fetch stored them (clause snapping
        # enabled and a clause matched). Otherwise None, and the field is omitted from the output.
        _clause_labels = m.get("clause_labels") or None
        provisions.append(
            EvidenceProvision(
                provision_id=pid, ext=m.get("ext"), label=m.get("label"), text=text[:_cap], clause_labels=_clause_labels
            )
        )
        have.add(pid)

    # 2. Parse citations and resolve them against the selection.
    cites = parse_citations(draft)
    resolved = resolve_against(cites, provisions)

    # 3. Neighbor promotion for unresolved, act-qualified citations (bounded, via NexusPort).
    unresolved = [c for c in resolved if not c.resolved]
    if unresolved and any(c.act for c in unresolved):
        p = port or NexusPort()
        fanout = max(0, int(getattr(s, "evidence_neighbor_fanout", 2)))
        cap = max(0, int(getattr(s, "evidence_max_promoted", 12)))
        try:
            promote_ids = await _promote_neighbors(p, unresolved, have, fanout, cap)
        except Exception:  # promotion must never break the answer path
            promote_ids = []
        if promote_ids:
            rows = await p.fetch_provisions(promote_ids)
            for r in rows:
                if r.id in have:
                    continue
                provisions.append(
                    EvidenceProvision(
                        provision_id=r.id,
                        ext=r.external_id,
                        label=r.label,
                        text=(r.text or "")[:_cap],
                        promoted=True,
                        source_pid=r.parent_id,
                    )
                )
                have.add(r.id)
            # 4. Re-resolve against the widened carrier (promoted neighbors, clause-snap children).
            resolved = resolve_against(cites, provisions)

    return EvidenceBundle(provisions=provisions, resolved=resolved)
