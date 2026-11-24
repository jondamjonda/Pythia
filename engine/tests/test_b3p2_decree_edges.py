"""B3-P2 — rel-11/12 delegation-edge resolver: pure resolve + row-build (no DB)."""

from __future__ import annotations

from pythia.graph.relations import BY_LABEL
from pythia.ingest.decree_edges import _SOURCE, build_edge_rows, resolve_delegation_edges


def test_resolver_emits_seat_for_present_decree():
    # a marked delegating provision names decree 296/2017; that decree IS in the corpus → one seat.
    delegating = [("prov-stat-1", "Ministerstvo stanoví vyhláškou č. 296/2017 Sb. podrobnosti.")]
    anchors = {"296/2017": "prov-decree-anchor"}
    seats = resolve_delegation_edges(delegating_provisions=delegating, decree_anchors=anchors)
    assert seats == [("prov-stat-1", "prov-decree-anchor", "296/2017")]


def test_resolver_skips_absent_decree():
    # names a decree that is NOT in the corpus → no seat (honest: cannot link to a missing node).
    delegating = [("prov-stat-1", "stanoví vyhláškou č. 999/2099 Sb.")]
    seats = resolve_delegation_edges(delegating_provisions=delegating, decree_anchors={})
    assert seats == []


def test_resolver_drops_self_edge():
    delegating = [("same", "stanoví vyhláškou č. 296/2017 Sb.")]
    anchors = {"296/2017": "same"}
    assert resolve_delegation_edges(delegating_provisions=delegating, decree_anchors=anchors) == []


def test_resolver_is_deterministic_and_idempotent():
    delegating = [
        ("p2", "stanoví vyhláškou č. 100/2020 Sb."),
        ("p1", "stanoví nařízením vlády č. 200/2020 Sb. a vyhláškou č. 100/2020 Sb."),
    ]
    anchors = {"100/2020": "a100", "200/2020": "a200"}
    first = resolve_delegation_edges(delegating_provisions=delegating, decree_anchors=anchors)
    second = resolve_delegation_edges(delegating_provisions=delegating, decree_anchors=anchors)
    assert first == second  # deterministic
    # sorted by (decree_ext, head, tail): 100/2020 seats before 200/2020
    assert first == [
        ("p1", "a100", "100/2020"),
        ("p2", "a100", "100/2020"),
        ("p1", "a200", "200/2020"),
    ]


def test_build_edge_rows_emits_rel11_forward_and_rel12_inverse():
    seats = [("stat", "decree", "296/2017")]
    rows = build_edge_rows(seats)
    assert len(rows) == 2
    fwd, inv = rows
    # forward: statute → decree, delegates_authority (rel 11)
    assert (fwd[0], fwd[1], fwd[2], fwd[3]) == (
        "stat",
        "decree",
        BY_LABEL["delegates_authority"].rel_id,
        "delegates_authority",
    )
    # inverse: decree → statute, implements (rel 12)
    assert (inv[0], inv[1], inv[2], inv[3]) == ("decree", "stat", BY_LABEL["implements"].rel_id, "implements")
    # both carry confidence 1.0 (structural) and the isolating source tag for idempotent rebuild
    assert fwd[5] == 1.0 and inv[5] == 1.0
    assert fwd[6] == _SOURCE == "b3p2_delegation"
    assert '"decree_external_id": "296/2017"' in fwd[7]
