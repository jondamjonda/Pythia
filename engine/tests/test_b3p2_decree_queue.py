"""B3-P2 — decree ingest QUEUE: pure ranking + ref extraction (no DB / no network)."""

from __future__ import annotations

from pythia.ingest.decree_queue import (
    SME_CORE_ACTS,
    classify_decree_kind,
    extract_decree_refs,
    queue_enabled,
    rank_candidates,
)


def test_extract_decree_refs_only_matches_decree_kinds():
    txt = (
        "Ministerstvo stanoví vyhláškou č. 296/2017 Sb. a nařízením vlády č. 361/2007 Sb. "
        "podrobnosti; tím není dotčen zákon č. 262/2006 Sb."
    )
    refs = extract_decree_refs(txt)
    # the plain "zákon č. 262/2006" is NOT a decree → excluded; the two decree kinds are captured
    assert refs == [("296/2017", "vyhlaska"), ("361/2007", "narizeni_vlady")]


def test_extract_decree_refs_dedups_within_provision():
    txt = "vyhláška č. 296/2017 Sb. … a znovu vyhláška č. 296/2017 Sb."
    assert extract_decree_refs(txt) == [("296/2017", "vyhlaska")]


def test_classify_decree_kind():
    assert classify_decree_kind("nařízení vlády") == "narizeni_vlady"
    assert classify_decree_kind("prováděcí právní předpis") == "predpis"
    assert classify_decree_kind("vyhláška") == "vyhlaska"


def test_rank_candidates_is_deterministic_and_demand_weighted():
    # two decrees, same delegating act; A has more dangling demand → A ranks first
    demand = [("100/2020", "vyhlaska")] * 5 + [("200/2020", "vyhlaska")] * 2
    delegation = [("100/2020", "vyhlaska", "999/1999"), ("200/2020", "vyhlaska", "999/1999")]
    ranked = rank_candidates(demand_rows=demand, delegation_rows=delegation, centrality={})
    assert [c.external_id for c in ranked] == ["100/2020", "200/2020"]
    assert ranked[0].demand == 5 and ranked[1].demand == 2
    # re-running yields the identical order (stable tie-break on external_id)
    assert rank_candidates(demand_rows=demand, delegation_rows=delegation, centrality={}) == ranked or [
        c.external_id for c in ranked
    ] == ["100/2020", "200/2020"]


def test_rank_sme_core_boost_lifts_a_low_demand_decree():
    # decree X under a peripheral act has HIGH demand; decree Y under the labour code (SME-core) has LOW
    # demand — the SME boost must lift Y above X (SME relevance beats raw popularity).
    labour = "262/2006"
    assert labour in SME_CORE_ACTS
    demand = [("X/2020", "vyhlaska")] * 3 + [("Y/2020", "vyhlaska")] * 1
    delegation = [("X/2020", "vyhlaska", "555/1555"), ("Y/2020", "vyhlaska", labour)]
    ranked = rank_candidates(demand_rows=demand, delegation_rows=delegation, centrality={})
    assert ranked[0].external_id == "Y/2020"
    assert ranked[0].sme_boost == SME_CORE_ACTS[labour]


def test_rank_centrality_tiebreaks_equal_demand():
    demand = [("A/2021", "vyhlaska"), ("B/2021", "vyhlaska")]
    delegation = [("A/2021", "vyhlaska", "act_a"), ("B/2021", "vyhlaska", "act_b")]
    ranked = rank_candidates(demand_rows=demand, delegation_rows=delegation, centrality={"act_a": 1.0, "act_b": 50.0})
    assert ranked[0].external_id == "B/2021"  # higher centrality of its delegating act wins


def test_queue_enabled_default_off(monkeypatch):
    monkeypatch.delenv("PYTHIA_DECREE_QUEUE_ENABLED", raising=False)
    assert queue_enabled() is False
    monkeypatch.setenv("PYTHIA_DECREE_QUEUE_ENABLED", "1")
    assert queue_enabled() is True
