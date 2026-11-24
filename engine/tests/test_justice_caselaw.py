"""N5 Wave 1 — justice case-law connector tests.

A pure-parse golden suite (no DB) plus ONE live golden on a REAL justice-portal record: fetch a known
in-corpus-citing day, pick a record, and assert the connector parses it the way the linker depends on —
classify → CASE_LAW, external_id == ECLI, every metadata.regulations entry accounted as a cite, and an
empty-paragraphNumber entry → a whole-act (label None) citation. The live test self-skips offline.
"""

from __future__ import annotations

from datetime import date

import pytest

from pythia.ingest import justice_caselaw as jc


# ── pure parse: cheap screen + structured cite ───────────────────────────────
def test_zu_act_ids_extracts_num_year_across_predpis_kinds():
    zu = [
        "§ 14b vyhl. č. 177/1996 Sb.",
        "§ 142 z. č. 99/1963 Sb.",
        "§ 1970 z. č. 89/2012 Sb.",
        "§ 2 nař. vl. č. 351/2013 Sb.",
    ]
    assert jc.zu_act_ids(zu) == {"177/1996", "99/1963", "89/2012", "351/2013"}
    assert jc.zu_act_ids([]) == set()
    assert jc.zu_act_ids(None) == set()


def test_parse_regulation_section_and_whole_act():
    sect = jc.parse_regulation({"paragraphNumber": "142", "lexNumber": 99, "lexYear": 1963, "lexType": "PREDPIS_ZAKON"})
    assert sect.act_external_id == "99/1963" and sect.label == "§ 142"
    whole = jc.parse_regulation({"paragraphNumber": "", "lexNumber": 549, "lexYear": 1991, "lexType": "PREDPIS_ZAKON"})
    assert whole.act_external_id == "549/1991" and whole.label is None  # empty § → whole-act
    # never fabricate an act id from a missing lexNumber/lexYear
    assert jc.parse_regulation({"paragraphNumber": "5", "lexNumber": None, "lexYear": 2012}) is None
    assert jc.parse_regulation({"paragraphNumber": "5", "lexNumber": 89, "lexYear": ""}) is None


def test_parse_cites_dedupes_on_act_and_label():
    fd = {
        "metadata": {
            "regulations": [
                {"paragraphNumber": "1968", "lexNumber": 89, "lexYear": 2012, "lexType": "PREDPIS_ZAKON"},
                {"paragraphNumber": "1968", "lexNumber": 89, "lexYear": 2012, "lexType": "PREDPIS_ZAKON"},  # dup
                {"paragraphNumber": "1970", "lexNumber": 89, "lexYear": 2012, "lexType": "PREDPIS_ZAKON"},
                {"paragraphNumber": "", "lexNumber": 549, "lexYear": 1991, "lexType": "PREDPIS_ZAKON"},
            ]
        }
    }
    cites = jc.parse_cites(fd)
    keys = {c.key() for c in cites}
    assert keys == {"89/2012|§ 1968", "89/2012|§ 1970", "549/1991|*"}
    assert sum(1 for c in cites if c.label is None) == 1  # the whole-act one


# ── classify: a ruling MUST be CASE_LAW (fail-fast) ──────────────────────────
def test_build_title_and_classify_case_law():
    doc = jc.RulingDoc(
        ecli="ECLI:CZ:OSSO:2024:23.C.299.2023.3",
        soud="Okresní soud v Sokolově",
        jednaci_cislo="23 C 299/2023-32",
        decision_date=date(2024, 1, 10),
        doc_type="JUDGEMENT",
        title="",
        finaldoc_url="x",
    )
    doc.title = jc.build_title(doc.soud, doc.jednaci_cislo, doc.doc_type)
    assert doc.title.startswith("Rozsudek") and "soud" in doc.title.lower()
    cls = jc.classify_ruling(doc)  # must NOT raise
    assert cls.legal_hierarchy == "CASE_LAW"
    assert cls.bindingness == "INTERPRETIVE_CASE_LAW"


def test_classify_asserts_case_law_fail_fast():
    # a title that classifies to a statute must blow up loudly (HARD RULE 5)
    bad = jc.RulingDoc(
        ecli="x",
        soud="",
        jednaci_cislo="",
        decision_date=None,
        doc_type="",
        title="Zákon č. 1/2000 Sb., o ničem",
        finaldoc_url="",
    )
    with pytest.raises(AssertionError):
        jc.classify_ruling(bad)


def test_resolution_type_stays_case_law():
    doc = jc.RulingDoc(
        ecli="x",
        soud="Krajský soud v Brně",
        jednaci_cislo="1 To 5/2024",
        decision_date=date(2024, 2, 2),
        doc_type="RESOLUTION",
        title=jc.build_title("Krajský soud v Brně", "1 To 5/2024", "RESOLUTION"),
        finaldoc_url="",
    )
    assert doc.title.startswith("Usnesení")
    assert jc.classify_ruling(doc).legal_hierarchy == "CASE_LAW"


# ── section parsing: stable eids, reasoning flags, cites on every section ─────
def test_parse_sections_stable_eids_and_reasoning_flags():
    cites = [jc.Cite("99/1963", "§ 142", "PREDPIS_ZAKON", "regulations", "142")]
    fd = {"verdictText": "I. Žalovaný je povinen…", "justificationText": "1. V řízení…"}
    provs = jc.parse_sections(fd, cites)
    by_eid = {p.eid: p for p in provs}
    assert set(by_eid) == {"ruling_vyrok", "ruling_oduvodneni"}  # stable, idempotent eids
    assert by_eid["ruling_vyrok"].metadata["is_reasoning"] is False  # výrok = application
    assert by_eid["ruling_oduvodneni"].metadata["is_reasoning"] is True  # odůvodnění = construal
    # the structured cites are stashed on every section's metadata (the linker reads them)
    assert by_eid["ruling_oduvodneni"].metadata["cites"][0]["act"] == "99/1963"


def test_parse_sections_drops_empty_and_falls_back_to_structured():
    fd = {"verdictText": "", "justification": [{"texts": [{"text": "structured body"}]}]}
    provs = jc.parse_sections(fd, [])
    assert [p.eid for p in provs] == ["ruling_oduvodneni"]  # empty výrok dropped
    assert provs[0].text == "structured body"  # structured fallback


# ── LIVE golden on a REAL record ─────────────────────────────────────────────
@pytest.mark.live
def test_live_golden_real_record_parses_for_linker():
    """Fetch the verified in-corpus-citing day 2024-03-15, find a record citing an in-corpus act, and assert
    the connector parses it as the linker needs. Self-skips offline."""
    import httpx

    corpus = {"99/1963", "89/2012"}  # known-present big codes
    try:
        with httpx.Client(timeout=60, headers={"User-Agent": jc._UA}, follow_redirects=True) as c:
            env = c.get(
                f"{jc._HOST}/api/opendata/2024/3/15", params={"pageNumber": 0, "pageSize": jc._PAGE_SIZE}
            ).json()
            record = finaldoc = None
            for rec in env["items"]:
                if jc.zu_act_ids(rec.get("zminenaUstanoveni")) & corpus:
                    fd = c.get(rec["odkaz"]).json()
                    if {x.act_external_id for x in jc.parse_cites(fd)} & corpus:
                        record, finaldoc = rec, fd
                        break
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"justice API unreachable: {exc!r}")
    if record is None:
        pytest.skip("no in-corpus-citing record found on the probe day")

    doc = jc.build_ruling_doc(record, finaldoc)
    # external_id == ECLI (the canonical dedupe key)
    assert doc.ecli.startswith("ECLI:CZ:") and doc.ecli == (finaldoc["metadata"]["ecli"])
    # classify → CASE_LAW (fail-fast would have raised)
    cls = jc.classify_ruling(doc)
    assert cls.legal_hierarchy == "CASE_LAW"
    # valid_from is the decision date OBJECT, valid_to NULL semantics (connector passes None)
    assert isinstance(doc.decision_date, date)

    # EVERY metadata.regulations entry is accounted as a cite (none dropped except missing-act / exact dups)
    regs = finaldoc["metadata"]["regulations"]
    parseable = [r for r in regs if r.get("lexNumber") not in (None, "") and r.get("lexYear") not in (None, "")]
    cites = jc.parse_cites(finaldoc)
    cited_keys = {c.key() for c in cites}
    for r in parseable:
        act = f"{int(r['lexNumber'])}/{int(r['lexYear'])}"
        raw = str(r.get("paragraphNumber") or "").strip()
        key = f"{act}|{'§ ' + raw if raw else '*'}"
        assert key in cited_keys, f"regulations entry {r} not accounted as a cite"

    # an empty-paragraphNumber entry (if present) → a whole-act (label None) citation
    for r in parseable:
        if not str(r.get("paragraphNumber") or "").strip():
            act = f"{int(r['lexNumber'])}/{int(r['lexYear'])}"
            assert any(c.act_external_id == act and c.label is None for c in cites)

    # at least one in-corpus act is cited (the keep condition)
    assert {c.act_external_id for c in cites} & corpus
