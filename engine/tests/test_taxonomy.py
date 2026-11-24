"""NT1 — the taxonomy vocabulary + classifier (pure, no DB)."""

from __future__ import annotations

from datetime import date, timedelta

from pythia.graph import taxonomy as tx


def test_controlled_vocab_is_consistent():
    # every doc-type's defaults reference valid axis values
    for d in tx.DOC_TYPES:
        assert tx.is_valid("legal_hierarchy", d.tier), d.key
        assert tx.is_valid("bindingness", d.bindingness), d.key
    assert tx.is_valid("legal_hierarchy", "STATUTE")
    assert not tx.is_valid("legal_hierarchy", "NOT_A_TIER")
    assert tx.is_valid("bindingness", None)  # None = unset is always valid


def test_eu_instruments_classify_from_title():
    # N4-B1: EU breadth ingests carry English instrument titles; classify must recognize them as EU
    # secondary law from the TITLE even when the legacy label is already normalized or wrong.
    d = tx.classify(
        title="Directive 2013/36/EU on access to the activity of credit institutions",
        jurisdiction="EU",
        legacy_doctype="eu_smernice",
        legacy_hierarchy="STATUTE",
    )
    assert (d.document_type, d.legal_hierarchy, d.territorial_scope) == ("eu_smernice", "EU_SECONDARY", "EU")
    r = tx.classify(
        title="Regulation (EU) No 575/2013 on prudential requirements", jurisdiction="EU", legacy_doctype="zakon"
    )
    assert (r.document_type, r.legal_hierarchy) == ("eu_narizeni", "EU_SECONDARY")
    # regression: a Czech statute title is unaffected by the new EU cues
    z = tx.classify(
        title="Zákon č. 256/2004 Sb., o podnikání na kapitálovém trhu", jurisdiction="CZ", legacy_doctype="act"
    )
    assert (z.document_type, z.legal_hierarchy, z.territorial_scope) == ("zakon", "STATUTE", "NATIONAL")


def test_municipal_regional_classification():
    # N4-B3: OZV / nařízení ÚSC must classify as ozv / narizeni_usc with the right territorial scope,
    # NOT as the generic 'vyhlaska' (the cue-ordering bug) or 'zakonik' (a "…tržní řád" nařízení).
    ozv = tx.classify(title="Obecně závazná vyhláška města Brna č. 1/2023, o místním poplatku", jurisdiction="CZ")
    assert (ozv.document_type, ozv.legal_hierarchy, ozv.territorial_scope) == (
        "ozv",
        "REGIONAL_MUNICIPAL_PREDPIS",
        "MUNICIPAL",
    )
    nar = tx.classify(title="Nařízení obce Psáry č. 1/2024, kterým se vydává tržní řád", jurisdiction="CZ")
    assert (nar.document_type, nar.territorial_scope) == ("narizeni_usc", "MUNICIPAL")
    kraj = tx.classify(title="Nařízení Středočeského kraje č. 3/2022", jurisdiction="CZ")
    assert (kraj.document_type, kraj.territorial_scope) == ("narizeni_usc", "REGIONAL")
    # regression guards: ČNB vyhláška + nařízení vlády unaffected; a Czech-titled EU reg is NOT stolen
    assert tx.classify(title="Vyhláška ČNB č. 163/2014 Sb.").document_type == "vyhlaska_cnb"
    assert tx.classify(title="Nařízení vlády č. 567/2006 Sb.").document_type == "narizeni_vlady"
    assert tx.classify(title="Nařízení Evropského parlamentu a Rady (EU) 2016/679").document_type != "narizeni_usc"


def test_domain_cues_cover_the_b2_domains():
    # N4-B2 added tax/criminal/civil-procedure/procurement/administrative/labour to the corpus;
    # the domain classifier must recognise each from its canonical title (and the financial fixes hold).
    assert "tax" in tx._domains("Zákon č. 586/1992 Sb., o daních z příjmů")
    assert "criminal" in tx._domains("Zákon č. 40/2009 Sb., trestní zákoník")
    assert "civil-procedure" in tx._domains("Zákon č. 99/1963 Sb., občanský soudní řád")
    assert "procurement" in tx._domains("Zákon č. 134/2016 Sb., o zadávání veřejných zakázek")
    assert "administrative" in tx._domains("Zákon č. 500/2004 Sb., správní řád")
    assert "labour" in tx._domains("Zákon č. 262/2006 Sb., zákoník práce")
    # audit-flagged financial inflection fix
    assert "financial-market" in tx._domains("Zákon č. 6/1993 Sb., o České národní bance")
    # a financial act must NOT be mis-tagged tax/criminal/labour
    assert tx._domains("Zákon č. 21/1992 Sb., o bankách") == ["financial-market"]


def test_legacy_tier_vocab_is_out_of_band():
    # NT5 cascade invariant: the pre-NT provision-grain values are NOT in the NT vocab (so the
    # conformance guard would catch any that survive), while the cascade targets ARE. Guards against
    # someone re-admitting a legacy literal into the vocab and silently masking the two-grain gap.
    for legacy in ("cz-act", "eu-regulation", "eu-directive"):
        assert not tx.is_valid("legal_hierarchy", legacy), legacy
    for nt in ("STATUTE", "EU_SECONDARY"):
        assert tx.is_valid("legal_hierarchy", nt), nt


def test_classifies_current_corpus_shapes():
    s = tx.classify(title="Zákon č. 21/1992 Sb., o bankách", legacy_doctype="act", jurisdiction="CZ")
    assert (s.legal_hierarchy, s.bindingness, s.document_type) == ("STATUTE", "HARD_LAW_ERGA_OMNES", "zakon")
    assert "financial-market" in s.legal_domain and s.territorial_scope == "NATIONAL"

    code = tx.classify(title="Zákon č. 89/2012 Sb., občanský zákoník", legacy_doctype="act")
    assert code.document_type == "zakonik"  # codes distinguished from bare statutes

    d = tx.classify(title="Council Directive 2011/61/EU", legacy_doctype="directive", jurisdiction="EU")
    assert (d.legal_hierarchy, d.territorial_scope) == ("EU_SECONDARY", "EU")


def test_ready_for_future_instruments():
    # the foundation must already classify what N4/N5 will bring (bottom-up readiness)
    oop = tx.classify(title="Opatření obecné povahy ČNB — kapitálová rezerva")
    assert (oop.legal_hierarchy, oop.bindingness) == ("MEASURE_GENERAL_NATURE", "BINDING_OOP")

    soft = tx.classify(title="Úřední sdělení ČNB k obecným pokynům EBA")
    assert (soft.legal_hierarchy, soft.bindingness) == ("SOFT_LAW", "SOFT_LAW")
    assert soft.bindingness_contested  # soft-law bindingness is a gradient → flagged

    case = tx.classify(title="Nález Ústavního soudu Pl. ÚS 27/09")
    assert case.legal_hierarchy == "CASE_LAW" and case.bindingness == "INTERPRETIVE_CASE_LAW"

    vyhl = tx.classify(title="Vyhláška ČNB č. 163/2014 Sb.")
    assert vyhl.document_type == "vyhlaska_cnb" and vyhl.issuing_authority == "ČNB"


def test_temporal_status_from_validity():
    repealed = tx.classify(
        title="Zákon č. 591/1992 Sb.", legacy_doctype="act", valid_to=date(2013, 1, 1), as_of=date(2026, 6, 13)
    )
    assert repealed.temporal_status == "REPEALED"
    future = tx.classify(
        title="Zákon č. 999/2030 Sb.", legacy_doctype="act", valid_from=date(2030, 1, 1), as_of=date(2026, 6, 13)
    )
    assert future.temporal_status == "NOT_YET_EFFECTIVE"


# ─────────────────────────── N5 (case law) Wave 0 — court-title classification ───────────────────────────
def test_court_rulings_classify_as_case_law():
    # GAP (verified today): the SCC judgment, the ÚS resolution, and the unifying opinion all
    # mis-classify as zakon/STATUTE — only 'Nález … Ústavního soudu' worked. These must read as
    # CASE_LAW / INTERPRETIVE_CASE_LAW (each title was a FAILING assertion before the cue fix).
    scc = tx.classify(title="Rozsudek Nejvyššího správního soudu č. j. 1 As 1/2020-50")
    assert (scc.legal_hierarchy, scc.bindingness) == ("CASE_LAW", "INTERPRETIVE_CASE_LAW")
    assert scc.document_type == "rozsudek"

    us = tx.classify(title="Usnesení Ústavního soudu sp. zn. I. ÚS 1/2020")
    assert (us.legal_hierarchy, us.bindingness) == ("CASE_LAW", "INTERPRETIVE_CASE_LAW")
    assert us.document_type == "usneseni_us"

    sjedn = tx.classify(title="Sjednocující stanovisko Nejvyššího soudu sp. zn. Cpjn 1/2020")
    assert (sjedn.legal_hierarchy, sjedn.bindingness) == ("CASE_LAW", "INTERPRETIVE_CASE_LAW")
    assert sjedn.document_type == "sjednocujici_stanovisko"

    # the existing nález cue still works (no regression)
    nalez = tx.classify(title="Nález Ústavního soudu sp. zn. Pl. ÚS 1/2020")
    assert (nalez.legal_hierarchy, nalez.document_type) == ("CASE_LAW", "nalez_us")


def test_soft_law_opinions_are_not_stolen_by_case_law_cues():
    # REGRESSION: bare/výkladové 'stanovisko' and 'metodický pokyn' stay SOFT_LAW — the new
    # 'sjednocující stanovisko' case-law cue must NOT capture them.
    vykl = tx.classify(title="Výkladové stanovisko ČNB k pravidlům jednání")
    assert (vykl.legal_hierarchy, vykl.bindingness) == ("SOFT_LAW", "SOFT_LAW")
    assert vykl.document_type == "vykladove_stanovisko"

    metod = tx.classify(title="Metodický pokyn k ochraně osobních údajů")
    assert (metod.legal_hierarchy, metod.bindingness) == ("SOFT_LAW", "SOFT_LAW")
    assert metod.document_type == "metodicky_pokyn"

    stan = tx.classify(title="Stanovisko Ministerstva financí k aplikaci zákona")
    assert (stan.legal_hierarchy, stan.bindingness) == ("SOFT_LAW", "SOFT_LAW")


def test_temporal_status_without_as_of_uses_today():
    # Without an explicit as_of the status is judged against today, not a frozen date: a ruling
    # decided yesterday is in force, and an act repealed yesterday is repealed.
    yesterday = date.today() - timedelta(days=1)
    assert tx._temporal_status(yesterday, None, None) == "IN_FORCE"
    assert tx._temporal_status(date(2000, 1, 1), yesterday, None) == "REPEALED"
    assert tx._temporal_status(date.today() + timedelta(days=1), None, None) == "NOT_YET_EFFECTIVE"
