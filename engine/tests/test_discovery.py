"""A1 (Temporal-Dimension wave 1) — pure, DB-free + network-free tests for the publication-feed watcher.

Two load-bearing surfaces are pinned here, both without a live DB or network:
  • parse_feed — the pure feed → DiscoveryCandidate parser: extracts candidates from the sbirkapp row-dict
    payload, parses the Czech effect date (unparseable → None, never fabricated), and de-dups by external id.
  • discover — its INERT default: with discovery_enabled False (the shipped default) it returns [] BEFORE
    any fetch or DB call, so the watcher is unreachable until the flag is flipped.

No fixture DB is touched and no HTTP request is made: parse_feed is pure, and the discover() test exercises
only the flag-off early-return (which precedes every fetch/DB statement).
"""

from __future__ import annotations

import asyncio
from datetime import date

import pythia.ingest.discovery as discovery
from pythia.ingest.discovery import (
    DISCOVERY_DDL,
    DiscoveryCandidate,
    discover,
    parse_feed,
)


# A small hand-built feed payload in the exact shape sbirkapp.list_issuer / parse_list return: row dicts
# with the SPP id under `spp`, the Czech effect date under `ucinnost`, and title cells `nazev`/`druh`.
_FEED = [
    {
        "spp": "SPP111",
        "druh": "Obecně závazná vyhláška",
        "cislo": "1/2026",
        "nazev": "O místním poplatku",
        "platnost": "platný",
        "ucinnost": "15. 3. 2026",
    },
    {
        "spp": "SPP222",
        "druh": "Nařízení",
        "cislo": "2/2026",
        "nazev": "Tržní řád",
        "platnost": "platný",
        "ucinnost": "01.01.2025",
    },
    # Duplicate external id (newest-first listings can repeat) — must collapse to the first occurrence.
    {
        "spp": "SPP111",
        "druh": "Obecně závazná vyhláška",
        "cislo": "1/2026",
        "nazev": "O místním poplatku (duplicate row)",
        "platnost": "platný",
        "ucinnost": "15. 3. 2026",
    },
    # Unparseable / missing date → published_on None (honest 'unknown'), candidate still produced.
    {
        "spp": "SPP333",
        "druh": "Vyhláška",
        "cislo": "3/2026",
        "nazev": "Bez data",
        "platnost": "platný",
        "ucinnost": "dnem vyhlášení",
    },
    # No external id → skipped entirely.
    {"spp": "", "druh": "Nařízení", "cislo": "9/2026", "nazev": "Bez SPP", "ucinnost": "01.06.2026"},
]


def test_parse_feed_extracts_and_dedups():
    cands = parse_feed(_FEED)
    # SPP111 (deduped), SPP222, SPP333 — the empty-id row is dropped, the duplicate collapses.
    assert [c.external_id for c in cands] == ["SPP111", "SPP222", "SPP333"]
    assert all(isinstance(c, DiscoveryCandidate) for c in cands)
    # source is tagged on every candidate (drives the discovery_queue dedup key).
    assert {c.source for c in cands} == {"sbirkapp"}


def test_parse_feed_parses_czech_dates():
    by_id = {c.external_id: c for c in parse_feed(_FEED)}
    # both Czech formats ("15. 3. 2026" with spaces and "01.01.2025") parse to real dates …
    assert by_id["SPP111"].published_on == date(2026, 3, 15)
    assert by_id["SPP222"].published_on == date(2025, 1, 1)
    # … and an unparseable formula-style date becomes None (never fabricated).
    assert by_id["SPP333"].published_on is None


def test_parse_feed_titles_and_dedup_keeps_first():
    by_id = {c.external_id: c for c in parse_feed(_FEED)}
    # title comes from `nazev`; the FIRST occurrence wins for the deduped id (not the "(duplicate row)" one).
    assert by_id["SPP111"].title == "O místním poplatku"
    assert by_id["SPP333"].title == "Bez data"


def test_parse_feed_empty_payload():
    # Defensive: None / empty payload → no candidates (no crash).
    assert parse_feed(None) == []
    assert parse_feed([]) == []


def test_ddl_is_create_if_not_exists_inert():
    # The table is created additively and idempotently — never a destructive or unconditional CREATE.
    assert "CREATE TABLE IF NOT EXISTS discovery_queue" in DISCOVERY_DDL
    assert "UNIQUE (external_id, source)" in DISCOVERY_DDL
    # the inert table carries the documented columns …
    for col in ("external_id", "published_on", "source", "status", "seen_at", "metadata"):
        assert col in DISCOVERY_DDL


def test_discover_returns_empty_when_flag_off(monkeypatch):
    # With discovery_enabled False (the shipped default) discover() must return [] BEFORE touching the
    # network or DB. We patch get_settings to a flag-off stub and assert the inert early-return; if discover
    # tried to fetch, this test would hang/error — its passing proves the flag-gate precedes every side effect.
    class _Off:
        discovery_enabled = False

    monkeypatch.setattr(discovery, "get_settings", lambda: _Off())
    out = asyncio.run(discover())
    assert out == []
