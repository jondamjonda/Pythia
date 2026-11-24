"""Diff two versions of a provision: text changes and changes in its deontic content.

The version table (provision_version) keeps every version's text, but while a provision has only one
version `version_diff` returns None. The diff logic is pure and tested on synthetic versions, so it
is ready once amendments produce second versions.

  - diff_text(old, new): a line-level difflib delta. Handles insertion (old is None) and repeal
    (new is None) without raising.
  - deontic_delta(old, new): which of modal, actor, action, condition and section_kind changed
    between two snapshots. A changed deadline, penalty or modal is what a business most needs to
    know about an amendment.
  - version_diff(conn, ...): read the latest two versions for one (document, label) key and
    assemble {old_text, new_text, text_diff, deontic_delta}. Read-only.

As part of the graph implementation package, this module may query the database directly.
"""

from __future__ import annotations

import difflib
from typing import Any

# Fields compared by deontic_delta: an obligation's modal, actor, action and condition, plus the
# section_kind that drives its trust tier. The order is the report order.
DEONTIC_FIELDS: tuple[str, ...] = (
    "deontic_modal",
    "deontic_actor",
    "deontic_action",
    "deontic_condition",
    "section_kind",
)


def _norm(text: str | None) -> list[str]:
    """Split text into lines with trailing whitespace removed; None or "" gives an empty list."""
    if not text:
        return []
    return [ln.rstrip() for ln in text.splitlines()]


def diff_text(old_text: str | None, new_text: str | None) -> dict[str, Any]:
    """Return the line-level difference between two provision texts. Never raises.

    Returns a dict with:
      - added, removed: whole lines present only in the new or only in the old text.
      - changed: {old, new} pairs for replaced line blocks (a real edit, not a pure addition or
        removal).
      - is_insertion: old_text is None (a new provision; everything is added).
      - is_repeal: new_text is None (the provision is gone; everything is removed). Both None counts
        as a degenerate repeal of nothing.

    The None cases are the two ends of a version lineage: the first version and a closed head with
    no successor. Between two real versions both flags are False and the lists carry the edit.
    """
    is_insertion = old_text is None
    is_repeal = new_text is None
    old_lines = _norm(old_text)
    new_lines = _norm(new_text)

    added: list[str] = []
    removed: list[str] = []
    changed: list[dict[str, str]] = []

    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            added.extend(new_lines[j1:j2])
        elif tag == "delete":
            removed.extend(old_lines[i1:i2])
        elif tag == "replace":
            # A real edit: pair the old and new runs of lines (the visible wording change).
            changed.append({"old": "\n".join(old_lines[i1:i2]), "new": "\n".join(new_lines[j1:j2])})

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "is_repeal": is_repeal,
        "is_insertion": is_insertion,
    }


def deontic_delta(old: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, tuple[Any, Any]]:
    """Compare the deontic and structural fields of two provision snapshots. Never raises.

    Returns {field: (old_value, new_value)} for each field in DEONTIC_FIELDS that changed. A flipped
    modal means an obligation became a permission (or the reverse); a new deontic_condition means a
    deadline or condition was added or changed. A missing snapshot (None) counts as all None, so an
    insertion reports every populated field as new and a repeal reports each one as lost.
    """
    old = old or {}
    new = new or {}
    delta: dict[str, tuple[Any, Any]] = {}
    for field in DEONTIC_FIELDS:
        ov = old.get(field)
        nv = new.get(field)
        if ov != nv:
            delta[field] = (ov, nv)
    return delta


async def version_diff(conn, source_document_id, provision_label: str) -> dict[str, Any] | None:
    """Diff the latest two versions of one (document, provision_label) key. Read-only.

    Returns None when fewer than two provision_version rows exist (no prior version to compare).
    Otherwise returns {old_text, new_text, text_diff, deontic_delta}, where text_diff comes from
    `diff_text`. provision_version rows carry no deontic columns, so deontic_delta compares an empty
    old snapshot with the live provision row for the label.
    """
    rows = await conn.fetch(
        """SELECT version_no, valid_from, valid_to, text, text_hash, text_format, is_head
             FROM provision_version
            WHERE source_document_id = $1 AND provision_label = $2
            ORDER BY version_no DESC
            LIMIT 2""",
        source_document_id,
        provision_label,
    )
    if rows is None or len(rows) < 2:
        return None

    new_row, old_row = rows[0], rows[1]  # sorted descending, so rows[0] is the newer version
    old_text = old_row["text"]
    new_text = new_row["text"]

    # provision_version rows do not carry the deontic_* columns, so read the current deontic fields
    # from the live provision row for this label. Once versions store their own deontic fields, this
    # can compare per-version values instead.
    new_snap = await _deontic_snapshot(conn, source_document_id, provision_label)

    return {
        "old_text": old_text,
        "new_text": new_text,
        "text_diff": diff_text(old_text, new_text),
        "deontic_delta": deontic_delta(None, new_snap),
    }


async def _deontic_snapshot(conn, source_document_id, provision_label: str) -> dict[str, Any]:
    """Read the DEONTIC_FIELDS for one label from the live provision row (all None if absent)."""
    row = await conn.fetchrow(
        """SELECT deontic_modal, deontic_actor, deontic_action, deontic_condition, section_kind
             FROM provision
            WHERE source_document_id = $1 AND label = $2
            LIMIT 1""",
        source_document_id,
        provision_label,
    )
    if row is None:
        return dict.fromkeys(DEONTIC_FIELDS)
    return {field: row[field] for field in DEONTIC_FIELDS}
