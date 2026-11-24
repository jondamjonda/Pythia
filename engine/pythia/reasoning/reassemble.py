"""Assemble the final, verified-only answer text after claim verification and repair.

``reassemble(draft, states, patches, gap_lines)`` derives the final answer from the repair loop's
final verdicts, purely by editing the drafted text:
  * keep verified and repaired claims (a repaired claim carries a 'replace' patch with new text);
  * demote citation, fact, calculation and profile failures: cut the claim's whole sentence and add
    an honest gap line (from gap_demote) at the end of the slot's section;
  * drop safety failures with an 'excise' patch and no gap line (safety removals are silent);
  * leave situation, gap and non-normative spans untouched (they assert nothing verifiable).

answer.py, not this module, then reruns ``verify_answer`` and ``split_into_sections`` on the final
text, so citations, sections and ``cited_provisions`` are derived from the final text the same way
they are for a draft. No citation offsets are rebased by hand, and every citation is automatically
among the cited provisions.

Invariants (property-tested):
  * With nothing demoted, dropped or replaced, the draft is returned unchanged.
  * The text of a demoted or safety-dropped claim appears nowhere in the final text.
  * Gap lines never contain a ``[act § n]`` citation, so rerunning ``verify_answer`` cannot count a
    gap line toward the 0.34 grounding floor.

Pure and deterministic. Edits are applied right to left so earlier offsets stay valid. Gap lines are
inserted at the end of their slot's section, located against the original draft's headings, and
merged into the same right-to-left edit list.
"""

from __future__ import annotations

import re

from pythia.reasoning.claim_repair import ClaimState, Patch
from pythia.reasoning.gap_demote import GapLine

# Heading line (same line-start grammar as sections._HEADING_RE). Used to find where a section ends,
# so a gap line lands at the end of its section (before the next heading), not mid-prose.
_HEADING_LINE_RE = re.compile(r"(?im)^[ \t]*#{1,6}[ \t]+\S")


def _section_end_for_span(draft: str, span: tuple[int, int]) -> int:
    """Return the offset where the section containing ``span`` ends.

    That is the start of the next heading line, or ``len(draft)``. Gap lines for a claim in this
    section are inserted here, after the section's surviving prose.
    """
    n = len(draft)
    s = span[0]
    nxt = n
    for m in _HEADING_LINE_RE.finditer(draft):
        if m.start() > s:
            nxt = m.start()
            break
    return nxt


def _collapse_ws(text: str) -> str:
    """Tidy the whitespace a cut can leave behind.

    Removes stranded blank lines, double spaces and spaces before punctuation without disturbing
    intentional paragraph breaks.
    """
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([.,;:])", r"\1", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Empty a line that is now only whitespace.
    text = re.sub(r"(?m)^[ \t]+$", "", text)
    # Trim trailing whitespace a cut leaves at the end of a line.
    text = re.sub(r"(?m)[ \t]+$", "", text)
    return text


def reassemble(
    draft: str,
    states: list[ClaimState],
    patches: list[Patch],
    gap_lines: list[GapLine],
) -> str:
    """Apply the loop's patches and gap lines to ``draft`` and return the final text.

    With no patches and no gap lines the draft is returned unchanged. Otherwise:
      1. Build the edit list: every 'replace' or 'excise' patch (at its claim span) and every gap
         line (an insertion at its section end). Gap lines are de-duplicated on ``gap_key``, one per
         (slot, gap_source), and placed at the section end of the first demoted claim with that key.
      2. Apply the edits right to left so earlier offsets stay valid.
      3. Tidy the whitespace the cuts leave.
    """
    if not patches and not gap_lines:
        return draft  # the loop changed nothing

    # 1a. Span edits from patches (replace or excise).
    edits: list[tuple[int, int, str]] = []  # (start, end, replacement); end == start is an insertion
    for p in patches:
        s, e = p.span
        if p.kind == "replace":
            edits.append((s, e, p.new_text))
        elif p.kind == "excise":
            edits.append((s, e, ""))

    # 1b. Gap-line insertions, de-duplicated on gap_key and anchored at the claim's section end.
    # gap_key to the section-end offset of the first demoted claim with that key.
    placed: dict[tuple[str, str], int] = {}
    for st in states:
        if st.status != "demoted" or st.gap_source is None:
            continue
        key = (st.slot, st.gap_source)
        if key not in placed:
            placed[key] = _section_end_for_span(draft, st.span)
    seen_keys: set[tuple[str, str]] = set()
    for gl in gap_lines:
        if gl.gap_key in seen_keys:
            continue  # one line per (slot, gap_source); never emit twice
        seen_keys.add(gl.gap_key)
        pos = placed.get(gl.gap_key)
        if pos is None:
            # Defensive: the gap line's claim is not in `states`, so append it at the end.
            pos = len(draft)
        line = ("\n" + gl.copy.strip() + "\n") if gl.copy.strip() else ""
        if line:
            edits.append((pos, pos, line))

    # 2. Apply right to left. At the same start, a replacement (larger end) is applied before an
    #    insertion, which keeps the result stable.
    edits.sort(key=lambda t: (t[0], t[1]), reverse=True)
    out = draft
    for s, e, rep in edits:
        out = out[:s] + rep + out[e:]

    # 3. Tidy the whitespace the cuts leave.
    return _collapse_ws(out).strip("\n") + ("\n" if draft.endswith("\n") else "")
