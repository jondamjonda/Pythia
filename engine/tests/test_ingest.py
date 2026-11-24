"""Parser tests for the statute uploader + cross-act resolver (pure, no DB)."""

from __future__ import annotations

from pythia.ingest.resolve_internal import candidate_labels
from pythia.ingest.resolve_xrefs import find_article, parse_target
from pythia.ingest.statute import _norm_label, find_external_citations, parse_provisions


def test_split_paragraphs():
    txt = (
        "§ 1\nPro účely zákona se widgetem rozumí zařízení.\n\n"
        "§ 2\nProvozovatel postupuje podle § 1.\n\n"
        "§ 2a\nVýjimka z § 2."
    )
    provs = parse_provisions(txt)
    assert [p.label for p in provs] == ["§ 1", "§ 2", "§ 2a"]
    assert "Provozovatel" in provs[1].text


def test_split_articles_and_markdown_headings():
    txt = "## Article 1\nScope.\n\n##### Article 2\nSubject to Article 1."
    provs = parse_provisions(txt)
    assert [p.label for p in provs] == ["Article 1", "Article 2"]


def test_norm_label():
    assert _norm_label("§   297a") == "§ 297a"
    assert _norm_label("article 31") == "Article 31"
    assert _norm_label("Article  4") == "Article 4"


def test_parse_target_eu_celex():
    # the 4-digit value is always the year, whichever side of the slash it sits on
    assert parse_target("Directive 2014/65").external_id == "32014L0065"
    assert parse_target("Regulation (EU) No 600/2014").external_id == "32014R0600"
    assert parse_target("Regulation (EU) 2023/1114").external_id == "32023R1114"
    assert parse_target("Directive (EU) 2015/849").external_id == "32015L0849"
    assert parse_target("Directive 2014/65").num_token == "2014/65"
    # 2-digit-year old directives have no real 4-digit year token -> refused (never in corpus)
    assert parse_target("Directive 95/46") is None


def test_parse_target_czech():
    t = parse_target("zákona č. 89/2012 Sb.")
    assert t is not None and t.external_id == "89/2012" and t.kind == "cz"


def test_find_article():
    eu = parse_target("Directive 2014/65")
    assert find_article("orders while complying with Article 27 of Directive 2014/65/EU at", eu) == "27"
    assert find_article("see Directive 2014/65/EU in general terms", eu) is None
    cz = parse_target("zákona č. 240/2013 Sb.")
    assert find_article("postupuje podle § 11 zákona č. 240/2013 Sb. a dále", cz) == "11"


def test_find_external_citations():
    body = (
        "Provozovatel postupuje podle § 11 zákona č. 240/2013 Sb. a podle "
        "Directive 2014/65/EU; viz též Directive 2014/65/EU."
    )
    cites = find_external_citations(body)
    assert "zákona č. 240/2013 Sb." in cites
    assert "Directive 2014/65" in cites  # /EU suffix dropped, matches resolver form
    assert len(cites) == 2  # the duplicate Directive mention is de-duplicated


def test_candidate_labels_internal():
    # EU: paragraph locator -> exact §, then article-header fallback (finest first)
    assert candidate_labels("art_10__para_1") == (["Article 10(1)", "Article 10"], 0.90)
    assert candidate_labels("art_112") == (["Article 112"], 0.90)
    # CZ: paragraph/point detail isn't modelled -> resolve to the § floor only
    assert candidate_labels("par_297.odst_3.pism_a") == (["§ 297"], 0.75)
    assert candidate_labels("par_2a.odst_1") == (["§ 2a"], 0.75)
    assert candidate_labels("nonsense") == ([], 0.0)
