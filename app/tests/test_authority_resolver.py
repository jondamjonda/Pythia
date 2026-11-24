"""B3 Pillar P3 / AQ P2 — the PURE competent-authority RESOLVER.

Fully offline (no DB, no network): all tests run against the committed DRAFT
sample fixtures under ``tests/fixtures/authority/``. Covers:
  1. topic-map parse: normalisation via the shared vocab, unknown-type drop,
     dedup preserving priority, malformed shape → {}.
  2. index build: territorial buckets + national bucket, DB-free.
  3. resolve: topic→authority; municipality→okres/kraj territorial match via the
     gazetteer row; finest-grain wins; national authority matches everywhere;
     unknown topic / unknown municipality ⇒ [] or national-only (honest).
  4. resolve is TOTAL — never raises on junk input.
  5. flag guard: maybe_resolve_authorities OFF ⇒ [] (inert / byte-identical).
"""

from __future__ import annotations

import json
from pathlib import Path

from regsuper.analysis import authority_resolver as ar
from regsuper.ingest.authority_registry import parse_seed

_FIX = Path(__file__).parent / "fixtures" / "authority"


def _index():
    seed = json.loads((_FIX / "authority_registry.sample.json").read_text("utf-8"))
    return ar.build_index(parse_seed(seed))


def _topics():
    return ar.load_topic_map(_FIX / "topic_authority.sample.json")


def _gaz():
    data = json.loads((_FIX / "municipality_gazetteer.sample.json").read_text("utf-8"))
    return {r["code"]: r for r in data}


# ── 1. topic map ─────────────────────────────────────────────────────────────
def test_topic_map_parses_and_normalises():
    tm = _topics()
    assert tm["dan_z_prijmu"] == ("financni_urad",)
    assert tm["gdpr"] == ("uoou",)
    # priority order preserved, all canonical tags.
    assert tm["zamestnavani"] == ("ossz", "oip", "financni_urad")


def test_topic_map_drops_unknown_types_and_dedups_priority():
    raw = {
        "topics": {
            "t1": ["finanční úřad", "financni_urad", "nonsense", "OSSZ"],  # label+alias+dup+bad
            "t2": ["who-knows"],  # all invalid → topic dropped
            "t3": "not-a-list",  # bad shape → dropped
            "": ["financni_urad"],  # blank key → dropped
        }
    }
    tm = ar.parse_topic_map(raw)
    assert tm["t1"] == ("financni_urad", "ossz")  # deduped, label→tag, bad dropped, order kept
    assert "t2" not in tm and "t3" not in tm and "" not in tm


def test_topic_map_malformed_shape_is_empty_not_raise():
    assert ar.parse_topic_map({}) == {}
    assert ar.parse_topic_map({"topics": None}) == {}
    assert ar.parse_topic_map({"nope": 1}) == {}


# ── 2. index ─────────────────────────────────────────────────────────────────
def test_index_has_territorial_and_national_buckets():
    idx = _index()
    # KHS-JMK is stamped at kraj CZ064.
    assert any(a["code"] == "KHS-JMK" for a in idx.by_area.get(("kraj", "CZ064"), []))
    # ÚOOÚ carries no jurisdiction → national bucket for its type.
    assert any(a["code"] == "UOOU-CR" for a in idx.national_by_type.get("uoou", []))
    # A national authority is NOT also in a territorial bucket.
    all_area_codes = {a["code"] for rows in idx.by_area.values() for a in rows}
    assert "UOOU-CR" not in all_area_codes


# ── 3. resolve — the core mappings ───────────────────────────────────────────
def test_resolve_topic_to_authority_via_municipality():
    idx, tm, gaz = _index(), _topics(), _gaz()
    brno = gaz["582786"]
    got = ar.resolve("dan_z_prijmu", brno, idx, tm)
    codes = [r.code for r in got]
    assert codes == ["FU-JMK-BRNO-VENKOV"]
    r = got[0]
    assert r.authority_type == "financni_urad"
    assert r.match_kind == "okres"  # finest grain that matched (CZ0642 beats kraj CZ064)
    assert r.area_code == "CZ0642"
    assert r.is_draft is True


def test_resolve_uses_kraj_when_no_okres_jurisdiction():
    idx, tm, gaz = _index(), _topics(), _gaz()
    plzen = gaz["554791"]
    # FU-PLK-PLZEN is stamped only at kraj CZ032.
    got = ar.resolve("dph", plzen, idx, tm)
    assert [r.code for r in got] == ["FU-PLK-PLZEN"]
    assert got[0].match_kind == "kraj"
    assert got[0].area_code == "CZ032"


def test_resolve_hygiene_topic_matches_khs_at_kraj():
    idx, tm, gaz = _index(), _topics(), _gaz()
    got = ar.resolve("hygiena_provozovny", gaz["582786"], idx, tm)
    assert [r.code for r in got] == ["KHS-JMK"]
    assert got[0].match_kind == "kraj"


def test_resolve_national_authority_matches_any_municipality():
    idx, tm, gaz = _index(), _topics(), _gaz()
    # ÚOOÚ is national → competent for a GDPR topic regardless of municipality,
    # and even with NO municipality supplied.
    for muni in (gaz["554791"], None):
        got = ar.resolve("gdpr", muni, idx, tm)
        assert [r.code for r in got] == ["UOOU-CR"]
        assert got[0].match_kind == "national"
        assert got[0].area_code is None


def test_resolve_multi_type_topic_returns_all_competent_offices():
    idx, tm, gaz = _index(), _topics(), _gaz()
    # 'zamestnavani' → (ossz, oip, financni_urad); Brno has OSSZ + FÚ (no OIP in seed).
    got = ar.resolve("zamestnavani", gaz["582786"], idx, tm)
    codes = {r.code for r in got}
    assert codes == {"OSSZ-BRNO-VENKOV", "FU-JMK-BRNO-VENKOV"}


# ── honest empties ───────────────────────────────────────────────────────────
def test_resolve_unknown_topic_is_empty():
    idx, tm, gaz = _index(), _topics(), _gaz()
    assert ar.resolve("neexistujici_tema", gaz["582786"], idx, tm) == []


def test_resolve_unknown_municipality_falls_back_to_national_only():
    idx, tm = _index(), _topics()
    # No municipality row → only national offices; a territorial-only topic ⇒ [].
    assert ar.resolve("dan_z_prijmu", None, idx, tm) == []
    assert [r.code for r in ar.resolve("gdpr", None, idx, tm)] == ["UOOU-CR"]


def test_resolve_municipality_outside_registry_coverage_is_empty():
    idx, tm, gaz = _index(), _topics(), _gaz()
    # Praha (CZ010 / CZ0100) has NO FÚ office in the sample seed → honest empty.
    assert ar.resolve("dan_z_prijmu", gaz["554782"], idx, tm) == []


# ── 4. total / never-raises ──────────────────────────────────────────────────
def test_resolve_is_total_on_junk_input():
    idx, tm = _index(), _topics()
    assert ar.resolve(None, None, idx, tm) == []
    assert ar.resolve("", None, idx, tm) == []
    assert ar.resolve("dan_z_prijmu", {"okres_code": ""}, idx, tm) == []  # blank codes
    assert ar.resolve("dan_z_prijmu", {"garbage": 1}, idx, tm) == []  # no grain keys


# ── 5. offline convenience + flag guard ──────────────────────────────────────
def test_resolve_from_fixtures_offline():
    got = ar.resolve_from_fixtures("dan_z_prijmu", "582786")
    assert [r.code for r in got] == ["FU-JMK-BRNO-VENKOV"]
    # unknown municipality_code → treated as no-municipality (national-only).
    assert ar.resolve_from_fixtures("dan_z_prijmu", "000000") == []
    assert [r.code for r in ar.resolve_from_fixtures("gdpr", None)] == ["UOOU-CR"]


def test_flag_guard_off_is_inert():
    idx, tm, gaz = _index(), _topics(), _gaz()
    # OFF (default) ⇒ no-op regardless of a real match being available.
    assert ar.maybe_resolve_authorities("dan_z_prijmu", gaz["582786"], idx, tm, enabled=False) == []
    # ON ⇒ delegates to resolve.
    on = ar.maybe_resolve_authorities("dan_z_prijmu", gaz["582786"], idx, tm, enabled=True)
    assert [r.code for r in on] == ["FU-JMK-BRNO-VENKOV"]
    # ON but missing index/map ⇒ still [] (defensive).
    assert ar.maybe_resolve_authorities("dan_z_prijmu", gaz["582786"], None, tm, enabled=True) == []
    assert (
        ar.maybe_resolve_authorities("dan_z_prijmu", gaz["582786"], idx, None, enabled=True) == []
    )


def test_default_flag_is_off():
    from regsuper.config import Settings

    assert Settings().authority_registry_enabled is False
