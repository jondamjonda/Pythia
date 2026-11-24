"""VL-B-10 — clause-snapped ctx windows (ctx_clause_snap_enabled), additive, byte-identical-OFF.

Pure unit tests (no live LLM, no live DB — NexusPort.clauses_for is exercised against a stub connection).
Proves:

  (a) the flag defaults OFF.
  (b) snap units — a beyond-head window snaps to its SMALLEST ENCLOSING clause (the head lede is never
      snapped; a window with no enclosing clause is kept unchanged).
  (c) a Civil-Code MULTI-CLAUSE § — a window that lands inside odst. (3) snaps to (3), not (2) or (4), and
      carries clause_label "(3)".
  (d) absent-table degrade — NexusPort.clauses_for returns {} when provision_clause does not exist (no snap,
      no raise); and build_ctx_window with clauses=None is the un-snapped VL-B-06 window.
  (e) OFF byte-identity — build_ctx_window(clauses=None) == build_ctx_window() (the un-snapped path), and
      ctx_clause_labels returns [] with no clauses.
"""

from __future__ import annotations

import pytest

from pythia.config import PythiaSettings
from pythia.ports import ClauseRow
from pythia.reasoning.answer import (
    _CTX_HEAD,
    _clause_segment,
    _clause_spans,
    _smallest_enclosing_clause,
    _snap_kept_to_clauses,
    build_ctx_window,
    ctx_clause_labels,
)


# A realistic Civil-Code §-blob: a heading line + four odst. clauses (the corpus's bold-marker markdown).
# Padded to push the clauses BEYOND the 700-char head so the ctx window actually widens onto them.
_HEADING = "§ 1747 Smlouva o úvěru a související povinnosti stran závazkového vztahu"
_PAD = "Úvodní kontext ustanovení. " * 32  # ~ pushes the clauses past char 700
_C1 = (
    "**(1)** Smlouvou o úvěru se úvěrující zavazuje, že úvěrovanému poskytne na jeho požádání "
    "peněžní prostředky do určité částky, a úvěrovaný se zavazuje poskytnuté prostředky vrátit a "
    "zaplatit úroky ve sjednané výši a lhůtě."
)
_C2 = (
    "**(2)** Úvěrující úvěrovanému poskytne peněžní prostředky v ujednané měně a v téže měně "
    "úvěrovaný prostředky vrací a platí úroky za dohodnutou dobu."
)
_C3 = (
    "**(3)** Úvěrovaný je povinen vrátit poskytnuté prostředky ve lhůtě do 30 dnů ode dne výzvy "
    "úvěrujícího, neujednají-li strany lhůtu jinou."
)
_C4 = "**(4)** Za prodlení s vrácením prostředků lze uložit smluvní pokutu do výše 50 000 Kč."
_MULTI = _HEADING + "\n" + _PAD + "\n" + _C1 + "\n" + _C2 + "\n" + _C3 + "\n" + _C4


def _clause_rows_from(text: str, heading: str) -> list[ClauseRow]:
    """Build ClauseRow rows the way clause_index would: clause_text = '<heading>\\n<segment>'. We locate each
    odst. marker's segment by hand so the fixture mirrors the stored index without importing the splitter."""
    import re

    marks = list(re.finditer(r"\*\*\([0-9]+\)\*\*", text))
    rows: list[ClauseRow] = []
    for i, m in enumerate(marks):
        start = m.start()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        seg = text[start:end].strip()
        label = m.group(0).replace("*", "")  # "**(3)**" → "(3)"
        rows.append(
            ClauseRow(
                provision_id="p1",
                clause_path=f"{i}:{label}",
                clause_label=label,
                clause_text=f"{heading}\n{seg}",
                ordinal=i,
            )
        )
    return rows


# ----- (a) flag defaults OFF -------------------------------------------------------------------------------


def test_flag_defaults_false():
    assert PythiaSettings().ctx_clause_snap_enabled is False


# ----- (d)/(e) absent / None ⇒ un-snapped, byte-identical ---------------------------------------------------


def test_clauses_none_is_byte_identical_to_unsnapped():
    terms = ["úvěr", "prostředky"]
    assert build_ctx_window(_MULTI, terms, clauses=None) == build_ctx_window(_MULTI, terms)


def test_ctx_clause_labels_empty_without_clauses():
    assert ctx_clause_labels(_MULTI, ["úvěr"], None) == []
    assert ctx_clause_labels(_MULTI, ["úvěr"], []) == []
    assert ctx_clause_labels("", ["úvěr"], _clause_rows_from(_MULTI, _HEADING)) == []


# ----- _clause_segment / _clause_spans units ---------------------------------------------------------------


def test_clause_segment_strips_heading_line():
    assert _clause_segment("§ 1 Head\n**(1)** body of clause") == "**(1)** body of clause"
    # no newline ⇒ returned as-is (heading-less)
    assert _clause_segment("**(1)** only") == "**(1)** only"


def test_clause_spans_locate_segments_verbatim():
    rows = _clause_rows_from(_MULTI, _HEADING)
    spans = _clause_spans(_MULTI, rows)
    assert len(spans) == 4
    for s, e, label in spans:
        # each located span is a verbatim slice of the provision text
        seg = _MULTI[s:e]
        assert label in {"(1)", "(2)", "(3)", "(4)"}
        assert seg and seg in _MULTI


# ----- (b) smallest-enclosing snap unit --------------------------------------------------------------------


def test_smallest_enclosing_clause_picks_the_innermost():
    # two nested candidate spans: [0,100) and [10,40); a probe span [15,30) is inside both → the smaller wins.
    cands = [(0, 100, "(outer)"), (10, 40, "(inner)")]
    enc = _smallest_enclosing_clause((15, 30), cands)
    assert enc == (10, 40, "(inner)")
    # a probe that straddles the inner boundary only fits the outer.
    assert _smallest_enclosing_clause((5, 50), cands) == (0, 100, "(outer)")
    # a probe enclosed by nothing ⇒ None.
    assert _smallest_enclosing_clause((200, 210), cands) is None


def test_head_span_is_never_snapped():
    rows = _clause_rows_from(_MULTI, _HEADING)
    spans = _clause_spans(_MULTI[:20000], rows)
    # the head lede (starts at 0) must be kept exactly, never replaced by a clause span.
    kept = [(0, _CTX_HEAD), (spans[2][0] + 5, spans[2][0] + 20)]  # head + a window inside clause (3)
    snapped, labels = _snap_kept_to_clauses(kept, _CTX_HEAD, spans, cap=100000)
    assert snapped[0] == (0, _CTX_HEAD)  # head untouched
    assert "(3)" in labels  # the beyond-head window snapped to (3)


def test_window_without_enclosing_clause_is_kept_unchanged():
    # a single clause span far away; a window that no clause encloses is returned as-is (degrade-safe).
    spans = [(500, 600, "(1)")]
    kept = [(0, 300), (900, 950)]  # head + a window with no enclosing clause
    snapped, labels = _snap_kept_to_clauses(kept, 300, spans, cap=100000)
    assert (900, 950) in snapped
    assert labels == []


# ----- (c) Civil-Code multi-clause: a window inside (3) snaps to (3) ----------------------------------------


def test_multiclause_window_snaps_to_the_right_clause():
    rows = _clause_rows_from(_MULTI, _HEADING)
    spans = _clause_spans(_MULTI, rows)
    # locate clause (3)'s span and craft a kept window that lands entirely inside it (the deadline "do 30 dnů").
    c3 = next(s for s in spans if s[2] == "(3)")
    probe = (c3[0] + 10, c3[0] + 25)
    snapped, labels = _snap_kept_to_clauses([(0, _CTX_HEAD), probe], _CTX_HEAD, spans, cap=100000)
    # exactly (3) is the label (never (2) or (4)); the clause-(3) span is present in the snapped set.
    assert labels == ["(3)"]
    assert any(s <= c3[0] and c3[1] <= e for s, e in snapped)


def test_build_ctx_window_with_clauses_carries_snapped_labels():
    terms = ["lhůtě", "pokutu"]
    rows = _clause_rows_from(_MULTI, _HEADING)
    # ON: the ctx text is built with clause-snap; the touched labels are the enclosing clauses.
    snapped_text = build_ctx_window(_MULTI, terms, clauses=rows)
    labels = ctx_clause_labels(_MULTI, terms, rows)
    assert snapped_text  # non-empty ctx
    assert labels  # at least one clause snapped to
    assert all(lbl in {"(1)", "(2)", "(3)", "(4)"} for lbl in labels)
    # the head lede is always the leading text of the ctx window (never lost to a snap).
    assert snapped_text.startswith(_MULTI[:_CTX_HEAD][:40])


# ----- (d) NexusPort.clauses_for — absent-table degrade + row shaping (stub conn, no live DB) ---------------


@pytest.mark.asyncio
async def test_clauses_for_absent_table_returns_empty(monkeypatch):
    """provision_clause missing ⇒ to_regclass IS NULL ⇒ clauses_for returns {} WITHOUT ever querying rows."""
    from pythia import ports

    class _Conn:
        async def fetchval(self, sql, *a):
            assert "to_regclass" in sql
            return None  # table absent

        async def fetch(self, *a):  # must never be reached on the absent path
            raise AssertionError("fetch() called despite absent provision_clause")

    _install_pool(monkeypatch, ports, _Conn())
    assert await ports.NexusPort().clauses_for(["p1", "p2"]) == {}


@pytest.mark.asyncio
async def test_clauses_for_empty_ids_short_circuits(monkeypatch):
    from pythia import ports

    class _Conn:
        async def fetchval(self, *a):
            raise AssertionError("no DB read for empty ids")

    _install_pool(monkeypatch, ports, _Conn())
    assert await ports.NexusPort().clauses_for([]) == {}


@pytest.mark.asyncio
async def test_clauses_for_shapes_rows_by_provision(monkeypatch):
    """A present table ⇒ rows are grouped {pid: [ClauseRow]} preserving the ordinal order."""
    from pythia import ports

    class _Conn:
        async def fetchval(self, sql, *a):
            return "provision_clause"  # present

        async def fetch(self, sql, ids):
            assert ids == ["p1"]
            return [
                {
                    "pid": "p1",
                    "clause_path": "0:(1)",
                    "clause_label": "(1)",
                    "clause_text": "§ 1\n**(1)** a",
                    "ordinal": 0,
                },
                {
                    "pid": "p1",
                    "clause_path": "1:(2)",
                    "clause_label": "(2)",
                    "clause_text": "§ 1\n**(2)** b",
                    "ordinal": 1,
                },
            ]

    _install_pool(monkeypatch, ports, _Conn())
    out = await ports.NexusPort().clauses_for(["p1"])
    assert set(out) == {"p1"}
    assert [c.clause_label for c in out["p1"]] == ["(1)", "(2)"]
    assert all(isinstance(c, ClauseRow) for c in out["p1"])


def _install_pool(monkeypatch, ports, conn):
    class _Acq:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    class _Pool:
        def acquire(self):
            return _Acq()

    async def _fake_pool():
        return _Pool()

    monkeypatch.setattr(ports, "get_pool", _fake_pool, raising=True)
