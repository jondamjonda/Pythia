"""N4-B4 — Věstník ČNB connector: pure parsing helpers (no network/DB)."""

from __future__ import annotations

from datetime import date

from pythia.ingest.cnb_vestnik import (
    _FURNITURE,
    _norm_label,
    _parse_cz_date,
    parse_application,
    parse_ucinnost,
    parse_year_page,
)


def test_parse_cz_date():
    assert _parse_cz_date("17", "prosince", "2025") == date(2025, 12, 17)
    assert _parse_cz_date("1", "ledna", "2026") == date(2026, 1, 1)


def test_oop_ucinnost_text_month():
    # binding OOP statutory účinnost (live: OOP-I/2024 = 8. března 2024, OOP-I/2026 = 5. června 2026)
    assert parse_ucinnost("Toto opatření nabývá účinnosti 8. března 2024.") == date(2024, 3, 8)
    assert parse_ucinnost("Toto opatření obecné povahy nabývá účinnosti 5. června 2026.") == date(2026, 6, 5)
    assert isinstance(parse_ucinnost("nabývá účinnosti 5. června 2026."), date)  # asyncpg date object


def test_ucinnost_numeric_and_invalid():
    assert parse_ucinnost("nabývá účinnosti dnem 1. 10. 2025") == date(2025, 10, 1)
    assert parse_ucinnost("nabývá účinnosti 31. 2. 2025") is None  # 31 Feb → None


def test_ucinnost_on_publication_and_no_clause_are_none():
    # 'dnem vyhlášení' has no calendar date → must NOT fabricate; no clause → None
    assert parse_ucinnost("Toto úřední sdělení nabývá účinnosti dnem vyhlášení ve Věstníku ČNB.") is None
    assert parse_ucinnost("III. Zrušovací ustanovení …") is None


def test_application_self_referential_only():
    assert parse_application(
        "Podle tohoto úředního sdělení České národní banky se postupuje od 11. června 2025."
    ) == date(2025, 6, 11)
    assert parse_application("IV. Účinnost Podle tohoto úředního sdělení se postupuje od 31. prosince 2025.") == date(
        2025, 12, 31
    )
    assert parse_application("Podle tohoto úředního sdělení se postupuje od 1. 10. 2025.") == date(2025, 10, 1)


def test_application_ignores_repealed_reference():
    # a bare 'ze dne' reference to a REPEALED doc must NOT be read as this doc's date (no self-reference)
    txt = "Tímto úředním sdělením se ruší úřední sdělení ČNB ze dne 6. června 2024. 2. Poskytovatelé …"
    assert parse_application(txt) is None
    assert parse_ucinnost(txt) is None
    assert _parse_cz_date("5", "nonsense", "2025") is None


def test_norm_label_maps_to_trust_axis():
    assert _norm_label("Úřední sdělení") == "us"  # → SOFT_LAW
    assert _norm_label("Opatření obecné povahy") == "oop"  # → BINDING_OOP
    assert _norm_label("Metodický pokyn") == "metod"  # → SOFT_LAW


def test_furniture_strips_page_running_headers():
    assert _FURNITURE.match("Strana 1 (celkem 4)")
    assert _FURNITURE.match("Věstník ČNB částka 17/2025 ze dne …")
    assert _FURNITURE.match("Třídicí znak 2 2 2 2 5 1 8 0")
    assert not _FURNITURE.match("I. Účel")  # a real section header survives


def test_parse_year_page_extracts_docs_and_skips_obsah():
    html = (
        "<html><body>"
        '<a href="/export/sites/cnb/cs/legislativa/.galleries/Vestnik-CNB/2025/vestnik_2025_18_obsah.pdf">Obsah (pdf, 1 kB)</a>'
        '<a href="/export/sites/cnb/cs/legislativa/.galleries/Vestnik-CNB/2025/vestnik_2025_18_22325330.pdf">'
        "23. Úřední sdělení České národní banky ze dne 17. prosince 2025 (pdf, 223 kB)</a>"
        "</body></html>"
    ).encode("utf-8")
    out = parse_year_page(html, 2025)
    assert len(out) == 1  # the _obsah TOC is skipped
    doc, vf = out[0]
    assert doc.ext == "ČNB-ÚS-23/2025" and doc.kind == "us"
    assert doc.url.endswith("vestnik_2025_18_22325330.pdf")
    assert vf == date(2025, 12, 17)
