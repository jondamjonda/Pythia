"""N4-B1b — the transposition linker's pure logic (no network/DB)."""

from __future__ import annotations

from pythia.ingest.transposition import cited_directives, to_celex


def test_to_celex():
    assert to_celex("2011", "61") == "32011L0061"
    assert to_celex("2014", "65") == "32014L0065"
    assert to_celex("2015", "2366") == "32015L2366"


def test_cited_directives_filters_to_corpus():
    in_corpus = {"32014L0065", "32013L0036"}
    txt = (
        "Tento zákon zapracovává příslušné předpisy Evropské unie, totiž směrnici 2014/65/EU "
        "a směrnici 2013/36/EU, a dále směrnici 2099/99/EU mimo korpus."
    )
    assert cited_directives(txt, in_corpus) == {"32014L0065", "32013L0036"}  # 2099/99 not in corpus


def test_cited_directives_empty_when_none_in_corpus():
    assert cited_directives("směrnice 2099/99/EU a 2098/12/EU", {"32014L0065"}) == set()


def test_cited_directives_handles_no_text():
    assert cited_directives("", {"32014L0065"}) == set()
    assert cited_directives(None, {"32014L0065"}) == set()
