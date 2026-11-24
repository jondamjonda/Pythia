"""Deterministic splitter that turns a section-headed draft into ``PythiaAnswer.sections``.

Each Section carries a slice of the answer's prose and the citations that fall inside it. The essay
format has three legal sections: Legislativní rámec (legislative), Judikatura (case_law) and
Hodnocení (assessment); the structured format adds the seven action-plan slots. The output is
purely additional: ``drafted_answer``, the flat ``citations_map``, the verdict and the confidence
are untouched.

Partition guarantee:
  * The sections tile the whole draft contiguously. Ordered by ``range``, the first starts at 0, the
    last ends at ``len(draft)``, and each ends where the next begins (no gap, no overlap). Each
    section's ``text == draft[range[0]:range[1]]``, including its own '##' heading line.
  * Because the ranges cover the whole draft, every citation chip whose span lies in the draft falls
    inside exactly one section. The sections' chips are therefore a true partition of the flat
    ``citations_map``: none lost, none duplicated. A chip in a preamble before the first heading
    belongs to the first section; a chip under a merged heading belongs to the section it was merged
    into.

Offsets:
  * Chip ``start`` and ``end`` stay relative to the whole draft. A section's chips are equal by
    value to the flat ``citations_map`` entries (never re-based or mutated; Pydantic copies each
    dict into the Section), so a chip's offsets slice its span out of ``drafted_answer`` directly.
  * Each Section also has a draft-relative ``range = [start, end]``, so a consumer can slice the
    section's prose with ``drafted_answer[start:end]`` and assign a chip to the section where
    ``range.start <= chip.start < chip.end <= range.end``.

Merge rather than drop, so a grounded chip is never lost:
  * A Judikatura (case_law) section is emitted only if it carries a grounded case-law citation (a
    chip whose provision has ``legal_hierarchy == 'CASE_LAW'``). When the LLM writes a
    '## Judikatura' heading without citing a ruling, that section's range and chips are merged into
    the previous retained section (or the next one, if it is first). A statute-only answer therefore
    has no standalone Judikatura section yet loses no chip. If the only section left is a chip-less
    Judikatura, it is relabelled 'legislative' and covers the whole draft.
  * Any other section with a blank body and no chip is merged the same way, so no empty section is
    emitted and contiguity holds.

A draft with no parseable ``## <heading>`` markers (the LLM ignored the format) yields ``[]``; the
caller then relies on ``drafted_answer``, which is always the complete answer.
"""

from __future__ import annotations

import re

from pythia.contract import CitedProvision, Section

# Heading lines the drafting prompts emit: a line starting with '##' followed by a known section
# name. The name is matched regardless of case and diacritics, but the '##' at line start is
# required so prose that mentions "judikatura" mid-sentence never causes a split.
_HEADINGS: list[tuple[str, str, str]] = [
    # (regex of accepted heading spellings, canonical kind, canonical heading)
    (r"legislativn[íi]\s+r[áa]mec", "legislative", "Legislativní rámec"),
    (r"judikatura", "case_law", "Judikatura"),
    (r"hodnocen[íi]", "assessment", "Hodnocení"),
    # The seven slots of the structured action plan (used when structured_answer_enabled is on).
    # Only _GEN_PROMPT_STRUCTURED emits these exact Czech headings; the essay prompt never does, and
    # the legacy schema below excludes them (test_default_off_never_emits_structured_kinds checks
    # this). They come after the essay rows and share no heading word with them, so they cannot
    # reclassify an essay heading.
    (r"va[šs]e\s+situace", "situation", "Vaše situace"),
    (r"co\s+se\s+na\s+v[áa]s\s+vztahuje", "applies", "Co se na vás vztahuje"),
    (r"konkr[ée]tn[íi]\s+povinnosti\s+a\s+lh[ůu]ty", "obligations", "Konkrétní povinnosti a lhůty"),
    (r"postup\s+krok\s+za\s+krokem", "steps", "Postup krok za krokem"),
    (r"kam\s+se\s+obr[áa]tit", "where", "Kam se obrátit"),
    (r"sankce\s+a\s+rizika", "risks", "Sankce a rizika"),
    (r"co\s+korpus\s+nepokr[ýy]v[áa]", "gaps", "Co korpus nepokrývá"),
]
_HEADING_RE = re.compile(
    # '##' at line start and a known heading name, followed by any trailing text on the heading line
    # (some models echo the prompt, e.g. '## Legislativní rámec — the statute provisions...'). The
    # '\b' stops 'rámec' matching inside a longer word; '[^\n]*$' absorbs an optional ': ...' or '—
    # ...' tail.
    r"(?im)^[ \t]*#{1,6}[ \t]*(?:" + "|".join(alt for alt, _, _ in _HEADINGS) + r")\b[^\n]*$"
)

# Legacy (essay) headings: only the three essay kinds. An essay draft is split with this set, so it
# can never match a structured-slot heading, whatever the LLM writes. The structured set is all ten
# rows of `_HEADINGS` with `_HEADING_RE`; the caller chooses with `schema=`.
_LEGACY_HEADINGS: list[tuple[str, str, str]] = _HEADINGS[:3]
_LEGACY_HEADING_RE = re.compile(
    r"(?im)^[ \t]*#{1,6}[ \t]*(?:" + "|".join(alt for alt, _, _ in _LEGACY_HEADINGS) + r")\b[^\n]*$"
)


def _kind_for(heading_text: str, headings: "list[tuple[str, str, str]]" = _HEADINGS) -> tuple[str, str] | None:
    """Return the canonical ``(kind, heading)`` for a matched heading line, or None if none matches."""
    for alt, kind, canonical in headings:
        if re.search(alt, heading_text, re.I):
            return kind, canonical
    return None


class _Tile:
    """A working section during tiling: a contiguous ``[start, end)`` draft span.

    Each tile carries its kind, heading and chips. Tiles first cover ``[0, len(draft)]``
    contiguously (the first tile absorbs any preamble). Tiles that should not stand alone are then
    merged into a neighbour, so the emitted sections still tile the draft and carry every grounded
    chip exactly once.
    """

    __slots__ = ("kind", "heading", "start", "end", "chips")

    def __init__(self, kind: str, heading: str, start: int, end: int):
        self.kind = kind
        self.heading = heading
        self.start = start
        self.end = end
        self.chips: list[dict] = []


def split_into_sections(
    draft: str | None,
    citations_map: list[dict],
    cited_provisions: list[CitedProvision] | None = None,
    *,
    schema: str = "structured",
) -> list[Section]:
    """Split a section-headed draft into Sections that tile it contiguously.

    Pure: works only on string offsets and the supplied chips and provisions. The sections cover
    ``[0, len(draft)]`` with no gap or overlap, so every grounded chip lands in exactly one section.
    A chip-less case_law section or a blank section is merged into a retained neighbour, never
    dropped. Returns ``[]`` when there are no heading markers; the caller keeps ``drafted_answer``.

    ``schema`` selects the heading vocabulary: 'legacy' (the essay format) recognises only the three
    essay headings, so an essay draft never splits on a structured-slot heading; 'structured' (the
    default) recognises all ten. The answer pipeline sets it from ``structured_answer_enabled``.
    """
    headings, heading_re = (_LEGACY_HEADINGS, _LEGACY_HEADING_RE) if schema == "legacy" else (_HEADINGS, _HEADING_RE)
    text = draft or ""
    chips = citations_map or []

    # Provision ids whose legal_hierarchy is CASE_LAW. A chip pointing to one is a grounded case-law
    # citation.
    case_law_ids: set[str] = {
        cp.provision_id for cp in (cited_provisions or []) if (cp.legal_hierarchy or "").upper() == "CASE_LAW"
    }

    # 1. Find every heading line. Without markers there is nothing to split on, so return [] and let
    #    the caller keep the complete drafted_answer.
    matches = list(heading_re.finditer(text))
    if not matches:
        return []

    # 2. Build contiguous tiles. A tile starts at its heading line (so the heading is inside the
    #    tile and text == draft[range]) and runs to the next heading or the end of the draft. The
    #    first tile is extended back to 0 to absorb any preamble, so the tiles cover [0, len(text)].
    #    A heading that matches no known name cannot start a section and is folded into the previous
    #    tile.
    tiles: list[_Tile] = []
    for i, m in enumerate(matches):
        span_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        resolved = _kind_for(m.group(0), headings)
        if resolved is None:
            # An unknown '##' line is not a section start. Extend the previous tile over it; before
            # any known heading, it stays in the preamble that the first known tile absorbs.
            if tiles:
                tiles[-1].end = span_end
            continue
        kind, heading = resolved
        start = matches[i].start()
        if not tiles:
            start = 0  # extend the first tile back to absorb any preamble
        tiles.append(_Tile(kind, heading, start, span_end))

    if not tiles:
        # Markers existed but none was a known heading, so there is nothing to split on.
        return []

    # 3. Assign each chip to the one tile that contains it. The tiles cover the draft contiguously,
    #    so every well-formed chip lands in exactly one. Chips keep their draft-relative offsets.
    for c in chips:
        s, e = c.get("start"), c.get("end")
        if not (isinstance(s, int) and isinstance(e, int)):
            continue
        for t in tiles:
            if t.start <= s and e <= t.end:
                t.chips.append(c)
                break

    # 4. Merge rather than drop. A tile that should not stand alone (a case_law tile without a
    #    grounded case-law chip, or another tile with a blank body and no chip) is folded into a
    #    retained neighbour, preferably the previous one. Folding left in one pass handles
    #    consecutive merges: the merged tile's span and chips move onto the survivor, keeping the
    #    tiling and every chip. Leading tiles with no survivor before them wait in `pending` until
    #    the first survivor, whose start then extends over them.
    def _is_standalone(t: _Tile) -> bool:
        if t.kind == "case_law":
            return any(c.get("provision_id") in case_law_ids for c in t.chips)
        # Any other tile is merged only when it has no chip and a blank body (heading only).
        body = text[_body_start(text, t, heading_re) : t.end]
        return bool(body.strip()) or bool(t.chips)

    kept: list[_Tile] = []
    pending: list[_Tile] = []  # leading tiles to merge, waiting for the first survivor
    for t in tiles:
        if _is_standalone(t):
            if pending:
                # The first survivor absorbs the buffered leading tiles: extend its start, take
                # their chips.
                t.start = pending[0].start
                for p in pending:
                    t.chips = p.chips + t.chips
                pending = []
            kept.append(t)
        elif kept:
            # Merge left into the previous retained tile, which takes over the span and chips (no
            # gap).
            prev = kept[-1]
            prev.end = t.end
            prev.chips.extend(t.chips)
        else:
            # No survivor yet: buffer this leading tile for the next survivor.
            pending.append(t)

    # Leading tiles were absorbed above. If every tile was to be merged (`kept` is empty), handle it
    # below.
    if not kept:
        # 5. Edge case: every tile was to be merged (e.g. only '## Judikatura' over statute prose,
        #    or only blank sections). Turn the whole draft into one section so no grounded chip is
        #    lost. A lone case_law tile is relabelled 'legislative' so no empty Judikatura section
        #    is shown; otherwise it keeps its kind.
        whole = _Tile(tiles[0].kind, tiles[0].heading, 0, len(text))
        whole.chips = [
            c
            for c in chips
            if isinstance(c.get("start"), int)
            and isinstance(c.get("end"), int)
            and 0 <= c["start"]
            and c["end"] <= len(text)
        ]
        if whole.kind == "case_law":
            whole.kind, whole.heading = "legislative", "Legislativní rámec"
        kept = [whole]

    # 6. The retained tiles still cover [0, len(text)] (merges only extend a survivor's start or
    #    end). Emit Sections with text == draft[range].
    return [
        Section(
            kind=t.kind,
            heading=t.heading,
            text=text[t.start : t.end],
            range=[t.start, t.end],
            citations_map=t.chips,
        )
        for t in kept
    ]


def _body_start(text: str, t: _Tile, heading_re: "re.Pattern" = _HEADING_RE) -> int:
    """Return the offset where the tile's body (the prose after its heading line) begins.

    Used only to decide whether a tile is heading-only. The heading line is located by regex within
    ``[t.start, t.end)``, which also works for the first tile whose span includes a preamble; the
    body starts after that line's newline.
    """
    m = heading_re.search(text, t.start, t.end)
    if m is None:
        return t.start
    nl = text.find("\n", m.end())
    return (nl + 1) if (nl != -1 and nl < t.end) else t.end
