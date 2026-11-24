"""N4-B3 — Sbírka PP ÚSC connector: pure parsing helpers (no network/DB)."""

from __future__ import annotations

from datetime import date

from pythia.ingest.esbirka import ProvisionDraft
from pythia.ingest.sbirkapp import _eid, _parse_cz_date, _unique_eids, parse_body, segment


def test_parse_cz_date_dotted_ddmmyyyy():
    # real Sbírka PP listing format, confirmed live (e.g. SPPYDBPWXCEMQ77Q = '01.07.2026')
    assert _parse_cz_date("01.07.2026") == date(2026, 7, 1)
    assert _parse_cz_date("27.05.2026") == date(2026, 5, 27)


def test_parse_cz_date_tolerates_spaces_and_iso_fallback():
    assert _parse_cz_date("1. 7. 2026") == date(2026, 7, 1)
    assert _parse_cz_date("2026-07-01") == date(2026, 7, 1)  # ISO fallback if the registry changes format


def test_parse_cz_date_returns_none_never_fabricates():
    # HARD CONSTRAINT: unknown/formula/empty → NULL, not an invented date
    for s in (None, "", "dnem vyhlášení", "15. dnem po vyhlášení", "not-a-date"):
        assert _parse_cz_date(s) is None


def test_parse_cz_date_rejects_impossible_calendar():
    assert _parse_cz_date("31.02.2026") is None  # Feb 31 → ValueError → None
    assert _parse_cz_date("32.01.2026") is None


def test_parse_cz_date_returns_date_object_for_asyncpg():
    assert isinstance(_parse_cz_date("01.07.2026"), date)  # never an ISO string


def test_segment_on_article_skeleton():
    lines = [
        "Obecně závazná vyhláška města X č. 1/2024",
        "Zastupitelstvo se usneslo …",
        "Čl. 1",
        "Předmět",
        "(1) Tato vyhláška upravuje …",
        "Čl. 2",
        "Účinnost",
        "Tato vyhláška nabývá účinnosti …",
    ]
    provs = segment(lines)
    eids = [p.eid for p in provs]
    assert eids == ["preambule", "cl_1", "cl_2"]  # preamble + 2 articles
    assert provs[1].label.startswith("Čl. 1") and "Předmět" in provs[1].text


def test_segment_handles_paragraph_marker_and_no_markers():
    assert [p.eid for p in segment(["§ 1", "text", "§ 2a", "more"])] == ["par_1", "par_2a"]
    flat = segment(["just a short ordinance with no article markers"])
    assert len(flat) == 1 and flat[0].eid == "text"


def test_segment_rejects_citation_lines():
    # D1 prereq: a footnote/enabling-clause citation that STARTS with "§ N" must NOT spawn a duplicate
    # article (this was the ÚSC label-collision bug).
    lines = [
        "§ 2",
        "Vymezení pojmů",
        "(1) Pro účely tohoto nařízení…",
        "§ 2 odst. 5 a 6 a § 3 vyhlášky č. 246/2001 Sb., o požární prevenci",  # a citation, not a header
        "§ 3",
        "Účinnost",
    ]
    eids = [p.eid for p in segment(lines)]
    assert eids == ["par_2", "par_3"]  # the citation line did not create a third provision
    assert eids.count("par_2") == 1  # and no duplicate § 2


def test_unique_eids_guard_suffixes_duplicates():
    out = _unique_eids(
        [ProvisionDraft("par_1", "§ 1", "par_1", 0, "a"), ProvisionDraft("par_1", "§ 1", "par_1", 1, "b")]
    )
    assert [p.eid for p in out] == ["par_1", "par_1_2"]  # the version-key (doc,label) is never duplicated


def test_eid_from_label():
    assert _eid("Čl. 1") == "cl_1"
    assert _eid("§ 14") == "par_14"
    assert _eid("Čl. 5a") == "cl_5a"


def test_parse_body_rejects_unknown_format():
    # the old OLE2 .doc magic (\xd0\xcf) is not DOCX/ODT/PDF → must raise, not silently mis-parse
    import pytest

    with pytest.raises(RuntimeError):
        parse_body(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest")
