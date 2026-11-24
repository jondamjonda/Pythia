"""WP-FUSION (Phase A) — PURE unit tests for the graph⊕semantic RRF re-rank math.

No DB, no I/O, no torch. NeighborHit objects are built directly. These tests pin the LOAD-BEARING
guarantee: a legally-essential (protected) provision reached by the graph at near-zero cosine can be
RAISED by good semantics but can NEVER be pruned or demoted below its graph seat.
"""

from __future__ import annotations

from pythia.graph import fusion
from pythia.graph.cte_expand import NeighborHit
from pythia.graph.fusion import (
    FusionParams,
    apply_floor,
    fuse,
    graph_rank_score,
    is_protected,
    rrf_fuse,
)

_P = FusionParams()  # defaults: w_g=1.0, w_s=0.6, w_s_gnn=0.3, k_rrf=60


def _hit(pid: str, score: float, relation_path: list[str] | None = None) -> NeighborHit:
    return NeighborHit(pid, hops=1, score=score, relation_path=relation_path or ["references"])


# ---------------------------------------------------------------------------
# rrf_fuse — formula exactness on a tiny known case
# ---------------------------------------------------------------------------
def test_rrf_fuse_formula_exact():
    k, wg, ws = _P.k_rrf, _P.w_g, _P.w_s
    # graph_order = [a, b]; sem_order = [b, a]  → a: rg0/rs1 ; b: rg1/rs0
    got = rrf_fuse(["a", "b"], ["b", "a"], _P, w_s=ws)
    assert got["a"] == wg / (k + 0 + 1) + ws / (k + 1 + 1)
    assert got["b"] == wg / (k + 1 + 1) + ws / (k + 0 + 1)


def test_rrf_fuse_sem_absent_id_gets_tail_rank():
    # 'c' is in graph_order but absent from sem_order → treated as rank len(sem_order) (just past end).
    # Here sem_order is empty so sem_miss == 0, and the tail semantic term is ws/(k+0+1) (>0 but tiny).
    k, wg, ws = _P.k_rrf, _P.w_g, _P.w_s
    got = rrf_fuse(["c"], [], _P, w_s=ws)
    assert got["c"] == wg / (k + 0 + 1) + ws / (k + 0 + 1)

    # Non-empty sem_order: an id absent from it lands at rank len(sem_order). 'b' present, 'c' absent.
    got2 = rrf_fuse(["b", "c"], ["b"], _P, w_s=ws)
    assert got2["c"] == wg / (k + 1 + 1) + ws / (k + 1 + 1)  # graph rank 1, sem tail rank len(["b"])=1


# ---------------------------------------------------------------------------
# MONOTONE-POSITIVE — the cardinal invariant: fused >= graph seat, always
# ---------------------------------------------------------------------------
def test_monotone_positive_fused_never_below_graph_seat():
    hits = [_hit("a", 5.0), _hit("b", 3.0), _hit("c", 1.0)]
    # 'c' deliberately absent from sem_scores (worst cosine), 'a'/'b' have cosine.
    sem = {"a": 0.9, "b": 0.4}
    out = fuse(hits, sem, {h.provision_id: None for h in hits}, method="cte")
    graph_order = sorted(
        [h.provision_id for h in out], key=lambda p: -next(h.score for h in out if h.provision_id == p)
    )
    rank = {pid: i for i, pid in enumerate(graph_order)}
    for h in out:
        seat = graph_rank_score(rank[h.provision_id], _P)
        assert h.fused_score >= seat, (h.provision_id, h.fused_score, seat)


# ---------------------------------------------------------------------------
# is_protected — the #1 KeyError guard + relation/role coverage
# ---------------------------------------------------------------------------
def test_is_protected_seed_and_empty_path_do_not_raise():
    assert is_protected(["seed"], None) is False
    assert is_protected([], None) is False
    assert is_protected([""], None) is False  # '' is not a registry label → .get() → None, no KeyError


def test_is_protected_by_relation():
    assert is_protected(["parent_of"], None) is True  # rel_id 3
    assert is_protected(["child_of"], None) is True  # rel_id 4
    assert is_protected(["defines"], None) is True  # rel_id 5
    assert is_protected(["uses_term"], None) is True  # rel_id 6
    assert is_protected(["transposes"], None) is True  # rel_id 7
    assert is_protected(["transposed_by"], None) is True  # rel_id 8
    # last label is what matters — a protected last hop protects regardless of earlier hops
    assert is_protected(["references", "transposes"], None) is True
    # a non-protected relation as last hop, no roles → not protected
    assert is_protected(["references"], None) is False
    assert is_protected(["internal_reference"], []) is False


def test_is_protected_by_role():
    for role in (1, 2, 3, 7):  # definition, obligation, prohibition, sanction
        assert is_protected(["references"], [role]) is True, role
    # non-protected roles, non-protected relation → False
    assert is_protected(["references"], [4, 5, 6, 8, 9]) is False
    # role intersection wins even on a seed path
    assert is_protected(["seed"], [7]) is True


def test_is_protected_folds_in_resolved_is_rule():
    # fold (a): the resolved trust rule-kind protects a provision even with NO protected relation/role.
    assert is_protected(["references"], None, is_rule=True) is True  # plain relation, no roles
    assert is_protected(["seed"], [], is_rule=True) is True  # seed path + no roles
    assert is_protected(["references"], [9], is_rule=False) is False  # default OFF → unchanged


def test_fuse_threads_is_rule_floor():
    # A rule-kind provision the graph put BEHIND, with ~zero cosine, is floored (protected) via is_rule
    # even though its relation ('references') and roles are non-protected — so it can't sink below its seat.
    p = _hit("R", score=3.0, relation_path=["references"])  # not protected by relation/role
    n = _hit("S", score=5.0, relation_path=["references"])
    out = fuse([p, n], {"S": 0.9}, {"R": None, "S": None}, method="cte", is_rule_by_id={"R": True})
    r = next(h for h in out if h.provision_id == "R")
    assert r.why["protected"] is True
    assert r.fused_score >= graph_rank_score(r.why["graph_rank"], _P)


# ---------------------------------------------------------------------------
# THE CARDINAL CASE — w_g-dominant prior keeps the essential provision on top
# ---------------------------------------------------------------------------
def test_cardinal_protected_zero_cosine_outranks_noise_best_cosine():
    # P = a protected definition the graph put one rank AHEAD, with ~ZERO cosine (absent from sem_scores).
    # N = a non-protected noise hit one graph rank BEHIND, with the BEST cosine (sem rank 0).
    # The graph prior (w_g dominant) must NOT be overturned by N's superior semantics: P stays on top,
    # and P never falls below its graph seat.
    p = _hit("P", score=10.0, relation_path=["defines"])  # protected (rel_id 5), better graph rank
    n = _hit("N", score=5.0, relation_path=["references"])  # non-protected, worse graph rank
    sem = {"N": 0.99}  # only N has cosine; P is absent (~0)
    out = fuse([p, n], sem, {"P": None, "N": None}, method="cte")

    # P is present and sorted ABOVE N despite N winning the semantic lane outright.
    assert [h.provision_id for h in out] == ["P", "N"]
    p_out = next(h for h in out if h.provision_id == "P")
    n_out = next(h for h in out if h.provision_id == "N")
    assert p_out.fused_score > n_out.fused_score

    # P never below its graph seat (graph rank 0 — best score).
    assert p_out.why["graph_rank"] == 0
    assert p_out.fused_score >= graph_rank_score(0, _P)
    assert p_out.why["protected"] is True
    assert n_out.why["protected"] is False
    # P had no cosine → sem_score None, and it sank to the BOTTOM of the semantic lane.
    assert p_out.sem_score is None
    assert p_out.why["sem_rank"] == 1  # 2 ids, P with no cosine → last


# ---------------------------------------------------------------------------
# apply_floor — pins protected/ppr ids at >= their graph seat
# ---------------------------------------------------------------------------
def test_apply_floor_pins_protected_to_seat():
    fused = {"x": 0.001, "y": 0.5}  # 'x' artificially depressed below its seat
    graph_ranks = {"x": 0, "y": 1}
    floored = apply_floor(fused, graph_ranks, {"x"}, _P, w_s_for_method=_P.w_s)
    assert floored["x"] == graph_rank_score(0, _P)  # raised to its seat
    assert floored["y"] == 0.5  # untouched (not protected)
    # input not mutated
    assert fused["x"] == 0.001


def test_apply_floor_ppr_member_floored_without_protected_relation():
    # A ppr_ids member is floored even with a plain (non-protected) relation path / no roles.
    h = _hit("z", score=1.0, relation_path=["references"])  # NOT protected by relation/role
    out = fuse([h], {}, {"z": None}, method="ppr", ppr_ids={"z"})
    z = out[0]
    assert z.why["protected"] is True  # promoted via ppr membership
    assert z.fused_score >= graph_rank_score(0, _P)


# ---------------------------------------------------------------------------
# gnn halves the semantic weight (w_s_gnn)
# ---------------------------------------------------------------------------
def test_gnn_halves_semantic_term():
    def run(method: str):
        hits = [_hit("a", 5.0), _hit("b", 3.0)]
        out = fuse(hits, {"a": 0.9, "b": 0.1}, {"a": None, "b": None}, method=method)
        return {h.provision_id: h.fused_score for h in out}

    cte = run("cte")
    gnn = run("gnn")
    # Same graph ranks/orders in both, so the graph term is identical; the only difference is the
    # semantic-lane weight (0.6 vs 0.3). Every fused score must therefore be SMALLER under gnn.
    for pid in ("a", "b"):
        assert gnn[pid] < cte[pid], pid
    # And specifically the gap is the semantic-term shrink (w_s 0.6 → w_s_gnn 0.3).
    k = _P.k_rrf
    # 'a' is graph rank 0 / sem rank 0 in both → delta = (0.6-0.3)/(k+1)
    assert abs((cte["a"] - gnn["a"]) - (_P.w_s - _P.w_s_gnn) / (k + 1)) < 1e-12


# ---------------------------------------------------------------------------
# determinism — identical inputs → identical order AND identical fused scores
# ---------------------------------------------------------------------------
def test_determinism_repeatable():
    def build():
        return [
            _hit("a", 5.0, ["defines"]),
            _hit("b", 5.0, ["references"]),  # tie on graph score with 'a' → id tie-break must decide
            _hit("c", 3.0, ["transposes"]),
            _hit("d", 3.0, ["references"]),
        ]

    sem = {"a": 0.2, "b": 0.8, "c": 0.0, "d": 0.5}
    roles = {"a": None, "b": [2], "c": None, "d": None}
    out1 = fuse(build(), dict(sem), dict(roles), method="cte")
    out2 = fuse(build(), dict(sem), dict(roles), method="cte")
    assert [h.provision_id for h in out1] == [h.provision_id for h in out2]
    assert [h.fused_score for h in out1] == [h.fused_score for h in out2]


def test_empty_hits_returns_empty():
    assert fuse([], {}, {}, method="cte") == []


# ---------------------------------------------------------------------------
# protected-id frozensets are exactly the agreed sets
# ---------------------------------------------------------------------------
def test_protected_constant_sets():
    assert fusion.PROTECTED_REL_IDS == frozenset({3, 4, 5, 6, 7, 8})
    assert fusion.PROTECTED_ROLE_IDS == frozenset({1, 2, 3, 7})
