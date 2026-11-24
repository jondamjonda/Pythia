"""e-Sbírka ingester — pure helpers (no network/DB)."""

from __future__ import annotations

from datetime import date

from pythia.ingest.esbirka import FragmentNode, _FLAT_PROVISION_LABEL, _parse_date, _version_effect_date, walk_act


def test_parse_date_basic():
    assert _parse_date("2014-01-01") == date(2014, 1, 1)
    assert _parse_date("2014-01-01T00:00:00") == date(2014, 1, 1)
    assert _parse_date(None) is None
    assert _parse_date("not-a-date") is None


def test_version_effect_date_prefers_explicit_field():
    # most acts: the explicit účinnost-znění-od field is authoritative and wins over the path
    meta = {"účinnost-znění-od": "2014-01-01"}
    assert _version_effect_date("esel-esb/eli/cz/sb/x/no-date-here", meta) == date(2014, 1, 1)


def test_version_effect_date_falls_back_to_uri_suffix():
    # 89/2012 regression: version node carries NO date field → derive from the ELI path suffix
    # (the suffix IS účinnost-znění-od for KONSOL versions, proven on 240/2013 15/15).
    assert _version_effect_date("esel-esb/eli/cz/sb/2012/89/2026-01-01", {"@id": "x"}) == date(2026, 1, 1)


def test_version_effect_date_promulgation_sentinel_is_none():
    # the ma-vyhlášené-znění promulgated version suffix '0000-00-00' is NOT a real effect date
    assert _version_effect_date("esel-esb/eli/cz/sb/2012/89/0000-00-00", {}) is None


def test_version_effect_date_unknown_stays_none():
    # no field and no parseable suffix → honest NULL, never fabricated
    assert _version_effect_date("esel-esb/eli/cz/sb/x/whatever", {}) is None
    assert _version_effect_date(None, None) is None


def test_version_effect_date_returns_date_object_for_asyncpg():
    assert isinstance(_version_effect_date("esel-esb/eli/cz/sb/2012/89/2026-01-01", {}), date)


def test_parse_date_handles_list():
    # N4-B2 regression: some acts (e.g. 234/2014) carry multiple účinnost entries as a list — take
    # the first rather than crashing strptime with a list argument.
    assert _parse_date(["2015-01-01", "2016-01-01"]) == date(2015, 1, 1)
    assert _parse_date([]) is None


def _frag(path, kind, designator, order_key, text_html, parent_path=None):
    return FragmentNode(
        path=tuple(path),
        kind=kind,
        designator=designator,
        label="",
        order_key=order_key,
        text_html=text_html,
        parent_path=parent_path,
    )


def test_walk_act_flat_fallback_for_sdeleni():
    # Flat ministerial *sdělení* (e.g. 286/2024 Sb.) carry NO par/clanek fragment — only an intro
    # `frag` plus `bod` items. The §-walker would yield 0 provisions; the fallback must instead capture
    # the whole body as ONE provision so the announced koruna figure stays retrievable. Body order is
    # by `order_key`, the enumeration markers are preserved, and a nested `frag` lands under its `bod`.
    nodes = [
        _frag(("bod_2", "frag_2"), "frag", "", "6ADAB0", "<div>table 20 800</div>", parent_path=("bod_2",)),
        _frag(("frag_0",), "frag", "", "6AC0", "<div>Ministerstvo vyhlašuje:</div>"),
        _frag(("bod_1",), "bod", "1", "6AD6", "<div>minimální mzda 20 800 Kč pro rok 2025</div>"),
        _frag(("bod_2",), "bod", "2", "6ADA", "<div>nejnižší úrovně zaručeného platu</div>"),
    ]
    provs, _xrefs = walk_act(nodes, own_external_id="286/2024")
    assert len(provs) == 1
    p = provs[0]
    assert (p.eid, p.hierarchy_path, p.label) == ("full", "full", _FLAT_PROVISION_LABEL)
    assert "20 800 Kč" in p.text
    # document order + enumeration: intro first, then "1." / "2." with the nested table indented under bod_2
    assert p.text.index("Ministerstvo") < p.text.index("1. minimální") < p.text.index("2. nejnižší")
    assert "  table 20 800" in p.text


def test_walk_act_no_fallback_when_paragrafs_present():
    # The fallback is INERT for a normal §-act: a `par` fragment yields its own provision row, so the
    # flat-body branch never runs and the single-"full" provision is NOT synthesised.
    nodes = [_frag(("cast_1", "par_1"), "par", "1", "6AC0", "<div>tělo paragrafu</div>")]
    provs, _xrefs = walk_act(nodes, own_external_id="1/2099")
    assert len(provs) == 1
    assert provs[0].eid == "cast_1_par_1" and provs[0].hierarchy_path == "cast_1.par_1"


def test_walk_act_empty_when_no_body():
    # A genuinely body-less doc (no provision rows AND no text) must still yield [] so ingest_act keeps
    # its "walker produced 0 provisions" guard.
    nodes = [_frag(("frag_0",), "frag", "", "6AC0", "")]
    provs, _xrefs = walk_act(nodes)
    assert provs == []
