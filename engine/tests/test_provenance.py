"""D1 — the provenance bundle validator (pure, no DB)."""

from __future__ import annotations

from datetime import date, datetime

from pythia.graph import provenance as prov


def _build(**kw):
    base = dict(
        external_id="x/1",
        title="t",
        publisher="Sbírka zákonů",
        language="cs",
        issuing_authority="Parlament",
        valid_from=None,
        valid_to=None,
        temporal_status="IN_FORCE",
        raw_checksum="c",
        raw_storage_uri="u",
        fetched_at=datetime(2026, 6, 14),
        metadata={},
    )
    base.update(kw)
    return prov.build(**base)


def test_source_of_maps_publishers():
    assert prov.source_of("Sbírka zákonů") == "esbirka"
    assert prov.source_of("EUR-Lex") == "eurlex"
    assert prov.source_of("ČNB") == "cnb"
    assert prov.source_of("ÚSC:Hlavní město Praha") == "usc"


def test_is_authoritative_is_family_aware():
    # only e-Sbírka CONSOLIDATED text is informative-only; the EU/ČNB/ÚSC primary forms are authentic
    assert prov.is_authoritative_for("esbirka", "esel/…/KONSOL") is False
    assert prov.is_authoritative_for("esbirka", "PROMULGATED") is True
    assert prov.is_authoritative_for("esbirka", None) is False  # unknown → safe consolidated default
    assert all(prov.is_authoritative_for(s, None) for s in ("eurlex", "cnb", "usc"))


def test_build_remaps_dates_per_source():
    # ČNB: the stored 'ze dne' date is the ADOPTION date, not účinnost
    c = _build(publisher="ČNB", valid_from=date(2025, 6, 5))
    assert c["date_adopted"] == "2025-06-05" and c["valid_from"] is None
    # e-Sbírka: valid_from genuinely IS účinnost
    e = _build(publisher="Sbírka zákonů", valid_from=date(2026, 1, 1))
    assert e["valid_from"] == "2026-01-01" and e["date_adopted"] is None


def test_build_derives_usc_institution_and_baseline():
    u = _build(publisher="ÚSC:Liberecký kraj", issuing_authority="neurčeno")
    assert u["institution"] == "Liberecký kraj" and u["baseline"] == "adopted" and u["source"] == "usc"
    assert _build(publisher="Sbírka zákonů")["baseline"] == "consolidated"


def test_is_valid_gate():
    assert prov.is_valid(_build())  # a well-formed bundle passes
    bad = _build()
    bad["source"] = "made_up"
    assert not prov.is_valid(bad)
    extra = _build()
    extra["surprise"] = 1
    assert not prov.is_valid(extra)  # extra key rejected
    baddate = _build()
    baddate["valid_from"] = "not-a-date"
    assert not prov.is_valid(baddate)


def test_build_emits_date_application_slot():
    # the soft-law application anchor gets its own controlled slot — None unless supplied via metadata
    assert _build()["date_application"] is None
    b = _build(publisher="ČNB", metadata={"date_application": date(2025, 10, 1)})
    assert b["date_application"] == "2025-10-01" and prov.is_valid(b)  # carried + still conformant
    # a populated date_application must be a real ISO date (gate type-checks the new key)
    bad = _build()
    bad["date_application"] = "not-a-date"
    assert not prov.is_valid(bad)


def test_cnb_oop_carries_real_ucinnost_as_valid_from():
    # a binding ČNB opatření obecné povahy DOES have a statutory účinnost → valid_from (not date_adopted);
    # the soft-law úřední sdělení keeps the 'ze dne' date in date_adopted with valid_from None.
    oop = _build(publisher="ČNB", bindingness="BINDING_OOP", valid_from=date(2026, 6, 5))
    assert oop["valid_from"] == "2026-06-05" and oop["date_adopted"] is None
    us = _build(publisher="ČNB", bindingness="SOFT_LAW", valid_from=date(2025, 6, 5))
    assert us["valid_from"] is None and us["date_adopted"] == "2025-06-05"


# ── N5 case-law Wave 0, segment S2: the 'court' source ──────────────────────────────────────────────


def test_source_of_maps_court_publishers():
    # GAP (today's verified bug): a court publisher fell through to 'esbirka', so a ruling SILENTLY
    # mis-sourced as a primary statute. Each authentic court channel must map to 'court'.
    assert prov.source_of("Ústavní soud") == "court"
    assert prov.source_of("Nejvyšší soud") == "court"
    assert prov.source_of("Nejvyšší správní soud") == "court"
    assert prov.source_of("Soud:Krajský soud v Brně") == "court"  # MoJ justice-portal convention
    # existing mappings stay intact (no regression)
    assert prov.source_of("Sbírka zákonů") == "esbirka"
    assert prov.source_of("EUR-Lex") == "eurlex"
    assert prov.source_of("ČNB") == "cnb"
    assert prov.source_of("ÚSC:Hlavní město Praha") == "usc"
    assert prov.source_of("něco neznámého") == "esbirka"  # default unchanged


def _build_court(**kw):
    base = dict(
        external_id="ÚS 1/26",
        title="Nález ÚS",
        publisher="Ústavní soud",
        language="cs",
        issuing_authority=None,
        valid_from=date(2026, 3, 10),
        valid_to=None,
        temporal_status="IN_FORCE",
        raw_checksum="c",
        raw_storage_uri="u",
        fetched_at=datetime(2026, 6, 14),
        metadata={},
    )
    base.update(kw)
    return prov.build(**base)


def test_court_bundle_is_valid_and_well_sourced():
    b = _build_court()
    assert b["source"] == "court"
    assert prov.is_valid(b)  # a court bundle is conformant…
    # …and is NOT silently sourced as an e-Sbírka statute (the verified gap)
    assert b["source"] != "esbirka" and b["baseline"] == "adopted"
    assert b["institution"] == "Ústavní soud"  # institution_of = the court (publisher)


def test_court_is_authoritative_authentic_vs_bootstrap():
    # authentic court text (NALUS/NS/NSS/justice portal) → True
    assert _build_court()["is_authoritative"] is True
    assert _build_court(publisher="Soud:Krajský soud v Brně")["is_authoritative"] is True
    # a CzCDC-bootstrap-tagged bundle (metadata flag) → False — clean hook for increment-2
    boot = _build_court(metadata={"bootstrap": True})
    assert boot["is_authoritative"] is False and prov.is_valid(boot)


def test_court_ruling_stays_retrievable_decision_date_drives_valid_from():
    # a ruling 'speaks' from its decision date: valid_from MUST be set (don't null it like ČNB soft-law),
    # or the temporal mask would drop the ruling. date_adopted == decision date; valid_to stays NULL.
    b = _build_court(valid_from=date(2026, 3, 10))
    assert b["valid_from"] == "2026-03-10"  # retrievable point-in-time
    assert b["date_adopted"] == "2026-03-10"
    assert b["valid_to"] is None
