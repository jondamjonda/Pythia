"""NT3 — temporal completeness + identifier dual-storage (pure, no DB)."""

from __future__ import annotations

from datetime import date

from pythia.graph import temporal as tmp


def test_is_authoritative_consolidated_is_informative_only():
    # consolidated KONSOL text is informative-only → NOT authoritative (the trust flag)
    assert tmp.is_authoritative("esel-esb/cis-esb-typ-znění/položka/KONSOL") is False
    assert tmp.is_authoritative(None) is False  # unknown → safe default
    assert tmp.is_authoritative("PROMULGATED_PDF") is True  # an authentic form would be True


def test_frag_stability_cutoff():
    assert tmp.FRAG_STABLE_AFTER == date(2026, 1, 15)
    assert tmp.frags_trustworthy(date(2026, 6, 7)) is True  # fetched after the cutoff
    assert tmp.frags_trustworthy(date(2025, 12, 1)) is False  # before → not yet stable
    assert tmp.needs_reresolution(date(2025, 12, 1)) is True
    assert tmp.needs_reresolution(date(2026, 6, 7)) is False
    assert tmp.needs_reresolution(None) is True  # unknown → re-resolve to be safe


def test_stable_locator_is_the_canonical_identifier():
    loc = tmp.stable_locator("21/1992", "§ 1", "cast_1.par_1")
    assert loc == {"act": "21/1992", "label": "§ 1", "path": "cast_1.par_1"}  # never the volatile frag


def test_eli_path():
    assert tmp.eli_path("21/1992") == "/eli/cz/sb/1992/21"
    assert tmp.eli_path("128/2000", "2025-01-01") == "/eli/cz/sb/2000/128/2025-01-01"
    assert tmp.eli_path("not-an-id") is None


def test_annulment_effect_vocab():
    assert set(tmp.ANNULMENT_EFFECT) == {"EX_NUNC", "EX_TUNC"}
