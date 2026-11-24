"""Deterministic claim segmentation (the atomizer) for claim verification.

`atomize` cuts a drafted answer into typed, verbatim claim spans. Together with the non-normative
spans (headings, blank lines, disclaimer boilerplate) the claims tile the draft exactly, and each
grounded citation chip is assigned to exactly one claim (the same span-containment rule sections.py
uses). Pure: no LLM, database, clock or randomness.
"""

from __future__ import annotations

import re

from pythia.contract import DraftPlan, Section
from pythia.reasoning.claim_ledger import Claim

# Default claim type per slot. Covers the three-heading legal sections (sections.py kinds), the
# seven-slot structured action-plan names, and the implicit 'answer' tile of a draft without
# headings. Unknown slots fall back to 'legal_rule', the neutral normative default.
_SLOT_DEFAULT_TYPE: dict[str, str] = {
    # sections.py kinds (three-heading legal draft)
    "legislative": "legal_rule",
    "case_law": "legal_rule",
    "assessment": "situation_application",
    # implicit whole-draft tile when the draft has no headings
    "answer": "legal_rule",
    # seven-slot structured action-plan names (best-effort mapping)
    "situace": "situation_application",
    "vase_situace": "situation_application",
    "ramec": "legal_rule",
    "povinnosti": "legal_rule",
    "kroky": "procedure_step",
    "lhuty": "deadline",
    "sankce": "sanction",
    "urady": "authority",
    "hodnoceni": "situation_application",
}
_DEFAULT_TYPE = "legal_rule"

# Regex refinements: cheap signals that a sentence is of a more specific type than its slot default.
# The Czech keywords match with or without diacritics and in any case. The patterns only classify;
# they never change the text.
_RE_DEADLINE = re.compile(
    r"\b(?:do\s+\d|ve\s+lh[ůu]t[ěe]|nejpozd[ěe]ji|do\s+\d+\s*(?:dn[ůií]|m[ěe]s[íi]c|let|rok)"
    r"|lh[ůu]t[ay]|kalend[áa][řr]n[íi]ch\s+dn)",
    re.IGNORECASE,
)
_RE_AMOUNT = re.compile(
    r"(?:\d[\d\s ]*\s*(?:K[čc]|EUR|€|%|korun|Euro)\b|\b(?:pokut[au]\s+až|výše\s+\d))",
    re.IGNORECASE,
)
_RE_SANCTION = re.compile(
    r"\b(?:pokut|sankc|p[řr]estupek|spr[áa]vn[íi]\s+delikt|trest|poru[šs]en[íi]\s+z[áa]kona\s+lze\s+ulo[žz])",
    re.IGNORECASE,
)
_RE_AUTHORITY = re.compile(
    r"\b(?:[Čc]NB|[Čc]esk[áa]\s+n[áa]rodn[íi]\s+banka|[Úu][řr]ad|ministerstv|finan[čc]n[íi]\s+[úu][řr]ad"
    r"|[Úu]OO[Úu]|[Čc]OI|SÚIP|[žz]ivnostensk[ýy]\s+[úu][řr]ad|soud|nab[íi]dnout\s+[úu][řr]adu)",
    re.IGNORECASE,
)
_RE_PROCEDURE = re.compile(
    r"\b(?:pod[eě]jte|pod[áa]n[íi]|zaregistr|ohl[áa][šs]|oznam|vypl[ňn]|p[řr]edlo[žz]|za[žz][áa]dejte"
    r"|krok|postup|nejd[řr][íi]ve\s+mus)",
    re.IGNORECASE,
)
_RE_GAP = re.compile(
    r"\b(?:nen[íi]\s+(?:v\s+)?(?:na[šs]em\s+)?korpusu|nem[áa]me\s+podklad|nelze\s+z\s+dostupn[ýy]ch"
    r"|chyb[íi]\s+podklad|bez\s+opory\s+v\s+textu|DRAFT\b|mezera\b)",
    re.IGNORECASE,
)
# situation_application: restating the asker's facts ("ve vašem případě", "jako s.r.o.", "vy jako").
_RE_SITUATION = re.compile(
    r"\b(?:ve\s+va[šs]em\s+p[řr][íi]pad[ěe]|va[šs][ei]\s+situac|vy\s+jako|jako\s+(?:s\.?\s?r\.?\s?o|a\.?\s?s)"
    r"|va[šs]e\s+spole[čc]nost)",
    re.IGNORECASE,
)

# Refinement order (first match wins): the most specific, highest-risk types first. deadline and
# amount carry facts that the fact and calculation verifiers check; sanction, authority and
# procedure_step are the practical types; situation_application restates the asker's facts; gap is
# an honest no-coverage line.
_REFINEMENTS: list[tuple[str, re.Pattern]] = [
    ("gap", _RE_GAP),
    ("deadline", _RE_DEADLINE),
    ("amount", _RE_AMOUNT),
    ("sanction", _RE_SANCTION),
    ("authority", _RE_AUTHORITY),
    ("procedure_step", _RE_PROCEDURE),
    ("situation_application", _RE_SITUATION),
]

# Abbreviation guard. A '.' that closes one of these tokens is not a sentence boundary, so '§ 6
# odst. 1 č. 455/1991 Sb.' stays one claim. Matched case-insensitively against the
# whitespace-delimited token immediately before the '.'; the set lists both diacritic and plain
# spellings.
_ABBREVS: frozenset[str] = frozenset(
    {
        "odst",
        "písm",
        "pism",
        "č",
        "c",
        "sb",
        "zák",
        "zak",
        "násl",
        "nasl",
        "čl",
        "cl",
        "písmeno",
        "tj",
        "tzn",
        "atd",
        "apod",
        "resp",
        "např",
        "napr",
        "mj",
        "vč",
        "vc",
        "pozn",
        "str",
        "odd",
        "písm",
        "bod",
        "věta",
        "veta",
        "hl",
        "část",
        "cast",
        "kap",
        "art",
        "nn",
        "cca",
        "mil",
        "tis",
    }
)
# A '.' after a bare integer (e.g. "§ 6 odst. 1.") is not a boundary either.
_NUM_BEFORE_DOT = re.compile(r"\d+\.$")

# Disclaimer and boilerplate lines are non-normative even when they read like a sentence.
_DISCLAIMER_RE = re.compile(
    r"^\s*(?:tato\s+odpov[ěe][ďd]|toto\s+nen[íi]\s+pr[áa]vn[íi]\s+rada|informativn[íi]|upozorn[ěe]n[íi]"
    r"|disclaimer|nejde\s+o\s+pr[áa]vn[íi]\s+slu[žz]b)",
    re.IGNORECASE,
)
# A Markdown heading line: one to six '#' followed by whitespace.
_HEADING_LINE = re.compile(r"^\s*#{1,6}\s")

# Boundary candidate: terminal punctuation, whitespace, then an upper-case letter, digit or '§'.
_BOUNDARY = re.compile(r"[.!?]\s+(?=[A-ZÁ-Ž0-9§])")


def _fold(tok: str) -> str:
    """Case-fold and strip a token for abbreviation matching; Czech diacritics are kept."""
    return tok.strip().lower().strip("([{\"'")


def _is_abbrev_dot(text: str, dot_pos: int) -> bool:
    """Return True if the '.' at `dot_pos` closes a known abbreviation or a bare number.

    Such a dot is not a sentence boundary. Looks at the token between the previous whitespace and
    the dot.
    """
    # Find the start of the token that ends at dot_pos.
    j = dot_pos
    while j > 0 and not text[j - 1].isspace():
        j -= 1
    tok = text[j:dot_pos]  # the run of non-space chars before the dot
    if not tok:
        return False
    if _fold(tok) in _ABBREVS:
        return True
    if tok.isdigit():  # bare number before the dot, as in "odst. 1."
        return True
    return False


def _split_line_sentences(text: str, base: int) -> list[tuple[int, int]]:
    """Split one line (no embedded newline) into sentences, skipping abbreviation dots.

    Returns absolute (start, end) spans, offset by `base`, that tile `text` with no gaps or
    overlaps.
    """
    spans: list[tuple[int, int]] = []
    seg_start = 0
    for m in _BOUNDARY.finditer(text):
        dot_pos = m.start()  # position of the terminal punctuation
        if text[dot_pos] == "." and _is_abbrev_dot(text, dot_pos):
            continue  # abbreviation dot: do not split here
        cut = m.end()  # cut after the trailing whitespace so nothing is lost
        spans.append((base + seg_start, base + cut))
        seg_start = cut
    if seg_start < len(text):
        spans.append((base + seg_start, base + len(text)))
    elif not spans:
        spans.append((base, base + len(text)))  # whole line is one (possibly empty) span
    return spans


def _split_sentences(text: str, base: int) -> list[tuple[int, int]]:
    """Split a tile body into spans that tile it without gaps or overlaps.

    Newlines are hard boundaries (the trailing '\\n' belongs to the line it ends), so a heading line
    and each blank line become their own spans, which `_is_normative` then marks non-normative. Each
    non-blank line is then sentence-split with the abbreviation guard. No '§ … Sb.' phrase spans a
    newline, so the guard keeps working, and a '## Heading' line stays separate from the prose
    beneath it.
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    n = len(text)
    while pos < n:
        nl = text.find("\n", pos)
        if nl == -1:
            line_end = n  # last line, no trailing newline
        else:
            line_end = nl + 1  # include the '\n' in this line's span
        line = text[pos:line_end]
        if line.strip() == "":
            spans.append((base + pos, base + line_end))  # a blank line is its own span
        else:
            spans.extend(_split_line_sentences(line, base + pos))
        pos = line_end
    if not spans:
        spans.append((base, base + n))
    return spans


def _refine_type(sentence: str, slot_default: str) -> tuple[str, str]:
    """Return (type, type_source) for a sentence.

    The first matching regex refinement wins, with source 'regex'. With no match, the slot default
    is returned with source 'slot_default'.
    """
    for typ, pat in _REFINEMENTS:
        if pat.search(sentence):
            return typ, "regex"
    return slot_default, "slot_default"


def _is_normative(sentence: str) -> bool:
    """Return False for spans that carry no assertion: blank, heading or disclaimer lines."""
    s = sentence.strip()
    if not s:
        return False
    if _HEADING_LINE.match(sentence):
        return False
    if _DISCLAIMER_RE.match(sentence):
        return False
    return True


def _tiles_from_sections(draft: str, sections: list[Section]) -> list[tuple[int, int, str]]:
    """Return the outer tiles (start, end, slot) that a sentence split must stay inside.

    Uses the Section ranges, which already tile [0, len(draft)]. With no sections (a draft without
    headings) the whole draft is one 'answer' tile. Defensively, any gap around or between the
    ranges is filled with an 'answer' tile so the tiles always cover the whole draft.
    """
    n = len(draft)
    if not sections:
        return [(0, n, "answer")]
    ranges: list[tuple[int, int, str]] = []
    for s in sections:
        if len(s.range) == 2 and 0 <= s.range[0] <= s.range[1] <= n:
            ranges.append((s.range[0], s.range[1], s.kind))
    ranges.sort(key=lambda t: t[0])
    # Fill gaps so the tiles cover the whole draft (defensive; the sections normally tile it fully).
    tiles: list[tuple[int, int, str]] = []
    cursor = 0
    for st, en, kind in ranges:
        if st > cursor:
            tiles.append((cursor, st, "answer"))
        tiles.append((st, en, kind))
        cursor = en
    if cursor < n:
        tiles.append((cursor, n, "answer"))
    if not tiles:
        tiles.append((0, n, "answer"))
    return tiles


def _tiles_from_plan(draft: str, plan: "DraftPlan") -> list[tuple[int, int, str]]:
    """Return the same outer tiles as `_tiles_from_sections`, read from a typed `DraftPlan`.

    `plan_from_sections` copies each Section's `range` and `kind` verbatim into a DraftSlot, so
    running the same validate, sort and gap-fill steps over the slots yields exactly the same tiles,
    and therefore the same claims. This lets the draft planner and the atomizer share one structural
    source of truth. An empty or None plan degrades to one 'answer' tile, like the empty-sections
    path.
    """
    n = len(draft)
    slots = plan.slots if plan is not None else []
    if not slots:
        return [(0, n, "answer")]
    ranges: list[tuple[int, int, str]] = []
    for slot in slots:
        r = slot.range
        if len(r) == 2 and isinstance(r[0], int) and isinstance(r[1], int) and 0 <= r[0] <= r[1] <= n:
            ranges.append((r[0], r[1], slot.kind))
    ranges.sort(key=lambda t: t[0])
    # Fill gaps so the tiles cover the whole draft (same defensive logic as _tiles_from_sections).
    tiles: list[tuple[int, int, str]] = []
    cursor = 0
    for st, en, kind in ranges:
        if st > cursor:
            tiles.append((cursor, st, "answer"))
        tiles.append((st, en, kind))
        cursor = en
    if cursor < n:
        tiles.append((cursor, n, "answer"))
    if not tiles:
        tiles.append((0, n, "answer"))
    return tiles


def atomize(
    draft: str, sections: list[Section], citations_map: list[dict], plan: "DraftPlan | None" = None
) -> list["Claim"]:
    """Cut `draft` into typed, verbatim claim spans and assign each citation chip to one of them.

    Each tile (one section, or the whole draft when there are no sections) is split on Czech
    sentence boundaries: '.', '!' or '?' followed by whitespace and an upper-case letter or digit,
    unless the '.' closes a known legal abbreviation or a bare number ('odst', 'písm', 'č', 'Sb',
    'zák', 'tj', ...), so '§ 6 odst. 1 č. 455/1991 Sb.' stays one claim. Heading lines, blank lines
    and disclaimer boilerplate become non-normative spans: they tile the draft but carry no
    verifiable assertion.

    A claim's type is its slot's default type (a data table covering both the three-heading and the
    seven-slot drafts), refined by regex; `type_source` records which of the two applied.

    The outer partition is read from `plan` when one is given (draft planner enabled) and is derived
    from `sections` otherwise. The plan copies each section's range and kind, so both give the same
    tiles and the same claims; an equivalence test pins this.

    Invariants (property-tested):

    - `draft[c.span[0]:c.span[1]] == c.text` for every claim (verbatim spans).
    - Normative and non-normative spans together are disjoint, sorted and cover [0, len(draft)].
    - Every chip whose [start, end] lies in the draft is assigned to exactly one span.
    - A sentence split never crosses a section boundary: each claim lies within one tile.
    """
    if not draft:
        return []
    tiles = _tiles_from_plan(draft, plan) if plan is not None else _tiles_from_sections(draft, sections)

    # 1. Build the ordered list of spans, normative and non-normative, that tiles the draft.
    all_spans: list[tuple[int, int, str, bool]] = []  # (start, end, slot, normative)
    for tstart, tend, slot in tiles:
        body = draft[tstart:tend]
        for s, e in _split_sentences(body, tstart):
            piece = draft[s:e]
            all_spans.append((s, e, slot, _is_normative(piece)))
    all_spans.sort(key=lambda t: t[0])

    # 2. Assign each chip to the first span that contains its [start, end].
    chips_for: dict[int, list[dict]] = {}  # all_spans index to the chips it contains
    for c in citations_map:
        cs, ce = c.get("start"), c.get("end")
        if not (isinstance(cs, int) and isinstance(ce, int)):
            continue
        for idx, (s, e, _slot, _norm) in enumerate(all_spans):
            if s <= cs and ce <= e:
                chips_for.setdefault(idx, []).append(c)
                break

    # 3. Emit a Claim for every span so the claim list tiles the draft. The orchestrator routes
    #    only normative claims; non-normative ones get normative=False and type 'gap' (inert).
    claims: list[Claim] = []
    for idx, (s, e, slot, normative) in enumerate(all_spans):
        text = draft[s:e]
        slot_default = _SLOT_DEFAULT_TYPE.get(slot, _DEFAULT_TYPE)
        if normative:
            typ, tsrc = _refine_type(text, slot_default)
        else:
            typ, tsrc = "gap", "slot_default"  # non-normative: inert, routed to no check
        claims.append(
            Claim(
                id=f"c{idx}",
                span=(s, e),
                text=text,
                slot=slot,
                type=typ,
                type_source=tsrc,
                chips=chips_for.get(idx, []),
                normative=normative,
            )
        )
    return claims
