"""N4-B1 — the EUR-Lex Formex-4 parser (pure, no network/DB)."""

from __future__ import annotations

from datetime import date

from lxml import etree

from pythia.ingest.eurlex import (
    _EU_DATES,
    EuActSpec,
    _axioms,
    _doctype_for,
    _norm_id,
    _select_eif,
    _select_eov,
    parse_formex,
)


# A minimal CELLAR RDF notice reproducing the MiFIR trap: a {MA/PART} partial-article entry-into-force
# (2019-01-03, inserted by a later amending act) that MUST be excluded, plus the genuine {EV}/{DATPUB}
# act-level entry-into-force (2014-07-02). End-of-validity carries the 9999-12-31 sentinel → None.
_NS = (
    'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
    'xmlns:owl="http://www.w3.org/2002/07/owl#" '
    'xmlns:ann="http://publications.europa.eu/ontology/annotation#" '
    'xmlns:cdm="http://publications.europa.eu/ontology/cdm#"'
)
_EIF = "http://publications.europa.eu/ontology/cdm#resource_legal_date_entry-into-force"
_EOV = "http://publications.europa.eu/ontology/cdm#resource_legal_date_end-of-validity"
_NOTICE = (
    f"<rdf:RDF {_NS}>"
    f'<rdf:Description><owl:annotatedProperty rdf:resource="{_EIF}"/>'
    f"<owl:annotatedTarget>2019-01-03</owl:annotatedTarget>"
    f"<ann:type_of_date>{{MA/PART|x}}</ann:type_of_date>"
    f"<ann:comment_on_date>{{MA/PART|x}} {{ART}} 1</ann:comment_on_date></rdf:Description>"
    f'<rdf:Description><owl:annotatedProperty rdf:resource="{_EIF}"/>'
    f"<owl:annotatedTarget>2014-07-02</owl:annotatedTarget>"
    f"<ann:type_of_date>{{EV|x}}</ann:type_of_date>"
    f"<ann:comment_on_date>{{DATPUB}} +20 {{V}} {{ART}} 55</ann:comment_on_date></rdf:Description>"
    f"<cdm:resource_legal_date_end-of-validity>9999-12-31</cdm:resource_legal_date_end-of-validity>"
    f"</rdf:RDF>"
).encode()


def test_select_eif_excludes_partial_article_block():
    root = etree.fromstring(_NOTICE)
    # the {MA/PART} 2019-01-03 trap is dropped; the act-level {EV}/{DATPUB} 2014-07-02 wins
    assert _select_eif(list(_axioms(root, _EIF))) == date(2014, 7, 2)


def test_select_eov_sentinel_is_none():
    root = etree.fromstring(_NOTICE)
    plain = [
        el.text for el in root.iter("{http://publications.europa.eu/ontology/cdm#}resource_legal_date_end-of-validity")
    ]
    assert _select_eov(plain, list(_axioms(root, _EOV))) is None  # 9999-12-31 sentinel → still in force


def test_select_eif_ambiguous_returns_none():
    # two distinct {EV} survivors → ambiguous → None (honest unknown, never a guess)
    axioms = [("2014-07-02", "{EV}", "{DATPUB}"), ("2015-01-01", "{EV}", "{DATPUB}")]
    assert _select_eif(axioms) is None
    # a lone {EV} after excluding a {MA/PART} → that date
    assert _select_eif([("2019-01-03", "{MA/PART}", "{MA/PART}"), ("2014-07-02", "{EV}", "{DATPUB}")]) == date(
        2014, 7, 2
    )


def test_eu_dates_table_resolves_known_traps():
    # the validated table must carry the trap-resolved dates (regression lock)
    assert _EU_DATES["32014R0600"] == ("2014-07-02", None)  # MiFIR, not 2019-01-03
    assert _EU_DATES["32014R0909"] == ("2014-09-17", None)  # CSDR, not 2015-01-01
    assert _EU_DATES["32015L0849"][1] == "2027-07-09"  # AMLD4 end-of-validity


# a minimal Formex-4 ACT: one enacting Article 1 with two numbered paragraphs; the 2nd paragraph
# QUOTES an inserted "Article 12a" (an amendment block) that must NOT be counted as an enacting article.
_ACT = b"""<?xml version='1.0' encoding='UTF-8'?>
<ACT>
  <ENACTING.TERMS>
    <ARTICLE IDENTIFIER="001"><TI.ART>Article 1</TI.ART><STI.ART>Scope</STI.ART>
      <PARAG IDENTIFIER="001.001"><NO.PARAG>1.</NO.PARAG><ALINEA>This Regulation lays down rules.</ALINEA></PARAG>
      <PARAG IDENTIFIER="001.002"><NO.PARAG>2.</NO.PARAG><ALINEA>The following is inserted:
        <QUOT.START/><ARTICLE IDENTIFIER="012A"><TI.ART>Article 12a</TI.ART>
          <PARAG IDENTIFIER="012A.001"><ALINEA>quoted body</ALINEA></PARAG></ARTICLE><QUOT.END/>
      </ALINEA></PARAG>
    </ARTICLE>
  </ENACTING.TERMS>
</ACT>"""

_SPEC = EuActSpec("32099R9999", "TEST", "Regulation (EU) test", "eu_narizeni")


def test_norm_id_strips_padding_keeps_letter():
    assert _norm_id("001") == "1"
    assert _norm_id("014") == "14"
    assert _norm_id("004A") == "4a"


def test_doctype_from_celex_letter():
    assert _doctype_for("32013L0036") == "eu_smernice"  # L = directive
    assert _doctype_for("32013R0575") == "eu_narizeni"  # R = regulation


def test_parse_excludes_quoted_articles():
    provs, xrefs, meta = parse_formex(_ACT, _SPEC)
    assert meta["n_articles"] == 1  # Article 1 only — the quoted 12a is excluded
    assert meta["n_paragraphs"] == 2  # two PARAGs of Article 1
    eids = {p.eid for p in provs}
    assert eids == {"art_1", "art_1__para_1", "art_1__para_2"}
    assert not any(p.eid.startswith("art_12") for p in provs)


def test_parse_builds_parent_and_label():
    provs, _, _ = parse_formex(_ACT, _SPEC)
    by_eid = {p.eid: p for p in provs}
    assert by_eid["art_1"].parent_eid is None and by_eid["art_1"].label == "Article 1"
    assert by_eid["art_1__para_1"].parent_eid == "art_1"
    assert by_eid["art_1__para_1"].label == "Article 1(1)"
    assert by_eid["art_1__para_1"].hierarchy_path == "art_1.para_1"
