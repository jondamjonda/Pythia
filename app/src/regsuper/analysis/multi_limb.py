"""Multi-limb provisions and duplicate findings: one provision, several duties.

A single provision often carries several distinct duties ("limbs"): an "either ... or ..." capital
choice, an enumerated "(a) ... (b) ... (c) ..." list, a base duty followed by an "; unless ..."
carve-out. A cascade built to emit one finding per extracted obligation can collapse such a
provision into one finding (losing a limb the client must satisfy), or duplicate one duty across an
EU directive line and its Czech transposition (showing two obligations that are really one). A
sharper case is a mutually exclusive pair shown side by side as if both bind. The example a lawyer
flagged: EUR 125 000 initial capital for an external AIFM versus EUR 300 000 for an internally
managed AIF; a manager is one or the other, never both.

This module groups these cases so render can annotate them ("alternative to...", "duplicate
of...", "also has limb (b)..."); render uses it when ``multi_limb_enabled`` is on, and L4 uses
``limb_key`` for gap de-duplication. It is regex and string work only: no LLM, database, network,
clock, randomness or I/O.

Hiding or understating a genuinely binding duty is worse than over-reporting one, so nothing here
removes, suppresses or hides a finding:

* ``split_limbs`` only adds structure. With no marker it returns the whole statement as one limb,
  and its segments always cover every duty in the input.
* ``mutual_exclusion_group`` groups the internal and external capital findings so render can say
  "alternative to"; it never decides which applies and never removes either.
* ``dedup_groups`` marks later occurrences of an identical substance ``is_duplicate=True`` and
  records a ``keep_index``, but every input finding appears in the output. "Duplicate" is a render
  hint (collapse in the list view), never a deletion; the full audit view can show every member.

Every outcome is an annotation (a grouping over indices into the caller's list), never a filtered
list. ``test_nothing_is_ever_dropped`` checks total coverage.
"""

from __future__ import annotations

import re
import unicodedata

# Public schema versions, shared by render and the tests.
LIMB_KEY_VERSION = 1
MULTI_LIMB_ENGINE_VERSION = 1

# Markers that signal a statement carries more than one limb (alternation, enumeration, carve-out).
# Each pairs a compiled pattern with a stable canonical label; callers and tests read the label,
# which stays the same for a marker even if its regex is widened. Ordered for determinism.
_MARKER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # "either ... or ...": the canonical alternation (as in the own-funds capital choice).
    ("either_or", re.compile(r"\beither\b", re.IGNORECASE)),
    # "alternatively": an explicit alternation connector.
    ("alternatively", re.compile(r"\balternatively\b", re.IGNORECASE)),
    # "; or" / "; and" enumerated clause separators (a semicolon followed by a conjunction).
    ("semicolon_or", re.compile(r";\s*or\b", re.IGNORECASE)),
    ("semicolon_and", re.compile(r";\s*and\b", re.IGNORECASE)),
    # "(a) ... (b) ..." lettered enumeration (needs at least two markers to count as a list).
    ("enum_letters", re.compile(r"\(\s*[a-z]\s*\)", re.IGNORECASE)),
    # "(1) ... (2) ..." numbered enumeration.
    ("enum_numbers", re.compile(r"\(\s*\d+\s*\)")),
    # "unless ...": a carve-out or condition that creates a second, conditional limb.
    ("unless", re.compile(r"\bunless\b", re.IGNORECASE)),
    # A bare " or " alternation between two verb phrases (lowest-priority signal; kept last).
    ("or", re.compile(r"\bor\b", re.IGNORECASE)),
)

# Splitting boundaries for split_limbs, as (marker_label, splitter regex). A statement is split on
# the first rule whose splitter yields more than one non-empty segment, so stronger signals win. The
# patterns are word-boundary anchored, so "for", "order" or "normal" never trip the bare "or" and
# "and" splitters.
_SPLIT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # "(a) X (b) Y": split at each "(letter)" marker (non-capturing, so markers are dropped).
    ("enum_letters", re.compile(r"\s*\(\s*[a-z]\s*\)\s*", re.IGNORECASE)),
    ("enum_numbers", re.compile(r"\s*\(\s*\d+\s*\)\s*")),
    # "either A or B": drop the leading "either" and split on " or " (see the "or" rule).
    # "A; or B" and "A; and B": split on the semicolon and conjunction.
    ("semicolon_conj", re.compile(r"\s*;\s*(?:or|and)\b\s*", re.IGNORECASE)),
    # "A unless B": keep both halves as limbs (base duty and carve-out condition).
    ("unless", re.compile(r"\s*\bunless\b\s*", re.IGNORECASE)),
    # "alternatively" connector.
    ("alternatively", re.compile(r"\s*\balternatively\b\s*", re.IGNORECASE)),
    # Bare " or " and " and " (lowest priority; word-bounded).
    ("or", re.compile(r"\s+\bor\b\s+", re.IGNORECASE)),
    ("and", re.compile(r"\s+\band\b\s+", re.IGNORECASE)),
)

_PUNCT_RX = re.compile(
    r"[^\w\s]", re.UNICODE
)  # strip punctuation for the substance key (keep word characters and spaces)
_WS_RX = re.compile(r"\s+")
_EITHER_LEAD_RX = re.compile(r"^\s*either\b\s*", re.IGNORECASE)

# Mutual-exclusion detection for the EUR 125k external versus EUR 300k internal capital pair.
# Regexes recognise the cohort (an initial-capital or own-funds threshold) and its two poles
# (external manager versus internally managed). This never decides which pole applies; it only
# groups both so render can annotate "alternative to the other capital basis". Widening these
# patterns is safe: it can only group more, never hide anything.
_CAPITAL_COHORT_RX = re.compile(r"\binitial\s+capital\b|\bown\s+funds\b|\bEUR\s*\d", re.IGNORECASE)
_EXTERNAL_POLE_RX = re.compile(r"\bexternal(?:ly)?\b", re.IGNORECASE)
_INTERNAL_POLE_RX = re.compile(r"\binternal(?:ly)?\b", re.IGNORECASE)


def _fold(text: str | None) -> str:
    """Strip diacritics and lower-case, so Czech (správnost, úplnost) and EU text key stably."""
    if not text:
        return ""
    # NFKD decomposes accented characters; dropping the combining marks turns 'á' into 'a'.
    nfkd = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    return stripped.lower()


def limb_key(
    actor: str | None, action: str | None, modal: str | None = None, deadline: str | None = None
) -> str:
    """Return a canonical substance key for de-duplication: a stable identity for "this duty".

    Folds the four substance-bearing fields (who, what, deontic modal, when) into one lower-case,
    diacritic-free, punctuation-free, whitespace-collapsed string joined by ``|``. Two findings
    that restate the same duty (for example an EU directive line and an identical Czech
    transposition, once both normalise to the same actor and action) get the same key; a different
    duty gets a different key.

    Only a key: it cannot drop anything. Deterministic, so identical inputs always give an
    identical key. Empty or None fields become empty segments, so the key is always well formed.
    """
    parts = []
    for field in (actor, action, modal, deadline):
        folded = _fold(field)
        folded = _PUNCT_RX.sub(" ", folded)  # punctuation becomes a space
        folded = _WS_RX.sub(" ", folded).strip()  # collapse internal whitespace
        parts.append(folded)
    return "|".join(parts)


def extract_limb_markers(text: str | None) -> list[str]:
    """Detect the alternation, enumeration and carve-out markers present in a statement.

    Returns a deterministically ordered, de-duplicated list of canonical labels, a subset of
    ``either_or``, ``alternatively``, ``semicolon_or``, ``semicolon_and``, ``enum_letters``,
    ``enum_numbers``, ``unless`` and ``or``. An empty list means no multi-limb marker, so the
    statement is a single limb. Enumeration markers need at least two occurrences, so a lone
    cross-reference like "(c)" does not pass for a list.

    Detection only; the statement is never edited. It only decides whether to attempt a split, and
    a missed marker just leaves the whole duty as one limb.
    """
    if not text:
        return []
    found: list[str] = []
    for label, pat in _MARKER_PATTERNS:
        matches = pat.findall(text)
        if not matches:
            continue
        # Enumeration markers must appear at least twice to form a list, not a stray "(c)".
        if label in ("enum_letters", "enum_numbers") and len(matches) < 2:
            continue
        found.append(label)
    return found


def split_limbs(
    statement: str | None, supporting_text: str | None = None, markers: list[str] | None = None
) -> list[str]:
    """Split a multi-limb statement into its limb segments.

    Returns ``[whole]`` (the stripped statement) when no usable marker splits it; never an empty
    list and never a lossy split.

    ``supporting_text`` (the fuller provision body, when available) is consulted only to detect
    markers that a truncated ``statement`` may have lost; it never replaces the statement or adds
    content to a limb. ``markers`` may be passed to reuse an earlier ``extract_limb_markers``
    result; otherwise it is computed from the statement, falling back to ``supporting_text`` for
    detection only.

    The segments always cover the whole duty: the splitter only inserts boundaries and never
    deletes a clause. If a split would yield a single fragment or only blank ones, the whole
    statement is returned unchanged. Showing fewer limbs than the client must satisfy would
    understate a duty, so when in doubt the statement stays whole: one big limb is safe, a dropped
    limb is not.
    """
    whole = (statement or "").strip()
    if not whole:
        return [whole] if statement is not None else [""]

    # Detect markers from the statement, then (for detection only) from the supporting body, so a
    # marker lost to truncation can still trigger a split. Splitting always operates on `whole`.
    if markers is None:
        markers = extract_limb_markers(whole)
        if not markers and supporting_text:
            markers = extract_limb_markers(supporting_text)
    marker_set = set(markers or [])

    for label, splitter in _SPLIT_RULES:
        # Map split-rule labels to the marker labels that license them.
        licensing = {
            "enum_letters": {"enum_letters"},
            "enum_numbers": {"enum_numbers"},
            "semicolon_conj": {"semicolon_or", "semicolon_and"},
            "unless": {"unless"},
            "alternatively": {"alternatively"},
            "or": {"either_or", "or"},
            "and": {
                "semicolon_and"
            },  # a bare "and" split is deliberately not licensed by a plain "or" or a list
        }[label]
        if not (marker_set & licensing):
            continue

        candidate = whole
        if label == "or" and "either_or" in marker_set:
            candidate = _EITHER_LEAD_RX.sub(
                "", candidate
            )  # drop the leading "either" before splitting on " or "

        segments = [s.strip() for s in splitter.split(candidate)]
        segments = [s for s in segments if s]  # drop blank boundary artefacts (never content)
        if len(segments) > 1:
            return segments

    return [whole]


def mutual_exclusion_group(verdicts: list[dict]) -> list[list[int]]:
    """Group mutually exclusive findings so render can mark them "alternative to" each other.

    Each input finding dict may carry ``management_structure``, ``obligation_type`` and
    ``statement``. The one lawyer-confirmed case in scope is the AIFM initial-capital pair:
    EUR 125 000 for an external manager versus EUR 300 000 for an internally managed AIF. A manager
    is one or the other, never both, so showing both as binding overstates the requirement.

    Cohort and pole are decided deterministically:

    * cohort: the statement mentions initial capital, own funds or a EUR figure
      (``_CAPITAL_COHORT_RX``), or an explicit ``management_structure`` is present;
    * pole: ``management_structure`` if given ("external" or "internal"), otherwise inferred from
      the statement ("external(ly)" versus "internal(ly)").

    Returns a list of groups, each a sorted list of indices into ``verdicts`` spanning both poles.
    A group is emitted only when both poles are present; a lone pole is no contradiction and needs
    no annotation.

    Neither pole is ever removed and nothing decides which applies; ``verdicts`` is read-only.
    Grouping both poles is what lets render say "this is the alternative to finding X" without
    hiding either.
    """
    poles: dict[int, str] = {}  # index to "external" or "internal"
    for i, v in enumerate(verdicts):
        ms = (v.get("management_structure") or "").strip().lower() if isinstance(v, dict) else ""
        stmt = (v.get("statement") or "") if isinstance(v, dict) else ""
        in_cohort = bool(_CAPITAL_COHORT_RX.search(stmt)) or ms in (
            "external",
            "internal",
            "internally",
            "externally",
        )
        if not in_cohort:
            continue
        pole: str | None = None
        if ms.startswith("extern"):
            pole = "external"
        elif ms.startswith("intern"):
            pole = "internal"
        elif _EXTERNAL_POLE_RX.search(stmt):
            pole = "external"
        elif _INTERNAL_POLE_RX.search(stmt):
            pole = "internal"
        if pole is not None:
            poles[i] = pole

    # Only the capital pair is in scope, so there is at most one group holding both poles.
    external_idx = sorted(i for i, p in poles.items() if p == "external")
    internal_idx = sorted(i for i, p in poles.items() if p == "internal")
    groups: list[list[int]] = []
    if external_idx and internal_idx:
        groups.append(sorted(external_idx + internal_idx))
    return groups


def dedup_groups(findings: list[dict]) -> list[dict]:
    """Mark duplicate findings (same substance, same law source); annotate only, never delete.

    Each input finding dict may carry ``actor``, ``action`` (or ``statement``), ``modal``,
    ``deadline`` and ``law_source``. Substance identity is
    ``limb_key(actor, action_or_statement, modal, deadline)`` and duplicates are scoped to
    ``(limb_key, law_source)``: the same duty twice under the same law source is a duplicate, while
    an EU line and its Czech transposition (different ``law_source``) stay separate, since they are
    distinct legal hooks that only render may cross-link.

    Returns a new list with one annotation dict per input finding, in the original order, holding
    ``index``, ``limb_key``, ``law_source``, ``dedup_group``, ``is_duplicate``, ``keep_index`` and
    ``engine_version``:

    * ``dedup_group``: a stable integer id shared by every member of one group.
    * ``is_duplicate``: False for the first occurrence (the one kept), True for every later one.
    * ``keep_index``: the index of the first occurrence, which render keeps in a collapsed list.

    Every input index appears exactly once in the output, so nothing is dropped.
    ``is_duplicate=True`` is a render hint, not a deletion; the full audit view can still show every
    member. Precision is high because the key requires identical substance (after folding) and an
    identical law source, so distinct duties never collide.
    """
    out: list[dict] = []
    group_ids: dict[tuple[str, object], int] = {}
    group_keepers: dict[tuple[str, object], int] = {}
    next_group_id = 0

    for i, f in enumerate(findings):
        actor = f.get("actor")
        action = f.get("action") if f.get("action") is not None else f.get("statement")
        modal = f.get("modal")
        deadline = f.get("deadline")
        law_source = f.get("law_source")
        key = limb_key(actor, action, modal, deadline)
        gkey = (key, law_source)

        if gkey not in group_ids:
            group_ids[gkey] = next_group_id
            group_keepers[gkey] = i
            next_group_id += 1
            is_dup = False
        else:
            is_dup = True

        out.append(
            {
                "index": i,
                "limb_key": key,
                "law_source": law_source,
                "dedup_group": group_ids[gkey],
                "is_duplicate": is_dup,
                "keep_index": group_keepers[gkey],
                "engine_version": MULTI_LIMB_ENGINE_VERSION,
            }
        )

    return out
