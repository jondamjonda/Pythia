"""B1a-07 — pure, DB-free unit tests for clause_index.split_clauses.

split_clauses is the only pure (no DB, no torch, no network) surface of the additive clause index, and
it is the load-bearing one: it decides WHICH §-blobs become clause rows and HOW they are keyed. These
tests pin its contract so a future refactor can't silently change the chunking unit:

  • atomic / single-marker text → [] (the ~53% no-op slice the proof identified);
  • a real multi-clause § → one row per clause, keyed by the expected clause_paths;
  • the § heading (first line) is prepended to every clause_text (the minimal context that is embedded);
  • clause fragments shorter than _MIN_FRAGMENT (40 chars) are skipped.

No DB fixture is touched — split_clauses is a pure function, so this whole module runs with no live pythia.
"""

from __future__ import annotations

from pythia.ingest.clause_index import _MIN_FRAGMENT, split_clauses


# A realistic markdown §-blob: a heading line + four odst. clauses in the corpus's bold-marker format.
_HEADING = "§ 1747 Smlouva o úvěru"
_MULTI = (
    _HEADING + "\n"
    "**(1)** Smlouvou o úvěru se úvěrující zavazuje, že úvěrovanému poskytne na jeho požádání "
    "peněžní prostředky do určité částky, a úvěrovaný se zavazuje poskytnuté prostředky vrátit.\n"
    "**(2)** Úvěrující úvěrovanému poskytne peněžní prostředky v ujednané měně a v téže měně "
    "úvěrovaný prostředky vrací a platí úroky.\n"
    "**(3)** Neujednají-li strany výši úroků, platí úroky ve výši stanovené zákonem nebo nařízením "
    "vlády podle obecné úpravy úroků z prodlení.\n"
    "**(4)** Úroky se platí v měně, ve které byly poskytnuty peněžní prostředky, a to za dobu od "
    "poskytnutí prostředků do jejich vrácení úvěrujícímu."
)


def test_atomic_text_returns_empty():
    # No clause markers at all → atomic provision → no clause grain (no-op path).
    assert split_clauses("§ 1 Tento zákon upravuje vzájemná práva a povinnosti osob.") == []


def test_single_marker_returns_empty():
    # Exactly one marker (< 2) → still no clause grain, per the proof's _split_clauses guard.
    txt = _HEADING + "\n**(1)** Jediné ustanovení tohoto paragrafu bez dalších odstavců a písmen."
    assert split_clauses(txt) == []


def test_multi_clause_paths_and_count():
    rows = split_clauses(_MULTI)
    # four substantial clauses → four rows.
    assert len(rows) == 4
    clause_paths = [cp for (cp, _label, _text, _ord) in rows]
    # clause_path == "<ordinal>:<label>" — ordinal is 0-based, label is the heading-free marker.
    assert clause_paths == ["0:(1)", "1:(2)", "2:(3)", "3:(4)"]
    # ordinals are the 0-based dense sequence.
    assert [od for (_cp, _l, _t, od) in rows] == [0, 1, 2, 3]
    # labels are the cleaned markers (stars stripped).
    assert [lab for (_cp, lab, _t, _od) in rows] == ["(1)", "(2)", "(3)", "(4)"]


def test_heading_prepended_to_every_clause():
    rows = split_clauses(_MULTI)
    for _cp, _label, clause_text, _ord in rows:
        # every clause_text begins with the § heading line (the embedded minimal context) …
        assert clause_text.startswith(_HEADING + "\n"), clause_text[:60]
        # … followed by the clause's own bold marker.
        assert clause_text.splitlines()[1].startswith("**(")


def test_short_fragment_skipped():
    # A trivial fragment (< _MIN_FRAGMENT chars between two markers) must be dropped, while the
    # surrounding substantial clauses survive. The "**(2)** ok." segment is far under 40 chars.
    txt = _HEADING + "\n**(1)** " + "x" * 80 + "\n**(2)** ok.\n**(3)** " + "y" * 80
    rows = split_clauses(txt)
    labels = [lab for (_cp, lab, _t, _od) in rows]
    # the tiny (2) is gone; (1) and (3) remain.
    assert labels == ["(1)", "(3)"]
    # and every surviving segment is at least _MIN_FRAGMENT long (heading excluded from the check).
    for _cp, _label, clause_text, _ord in rows:
        seg = clause_text.split("\n", 1)[1]
        assert len(seg) >= _MIN_FRAGMENT


def test_ordinals_are_dense_after_skip():
    # After a fragment is skipped, ordinals stay a dense 0..n-1 sequence (no gap from the dropped clause).
    txt = _HEADING + "\n**(1)** " + "a" * 80 + "\n**(2)** no.\n**(3)** " + "b" * 80 + "\n**(4)** " + "c" * 80
    rows = split_clauses(txt)
    assert [od for (_cp, _l, _t, od) in rows] == [0, 1, 2]
    assert [cp for (cp, _l, _t, _od) in rows] == ["0:(1)", "1:(3)", "2:(4)"]
