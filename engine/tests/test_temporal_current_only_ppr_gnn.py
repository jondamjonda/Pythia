"""TC-4c — current_only completed on PPR + GNN (additive, gated default-OFF, fail-open).

TC-4/TC-4b filter candidates + the seed/cte retrieve path; TC-4c finishes coverage on the two alternate
expansion engines: the PPR edge-view (the fail-open pit predicate on BOTH ps/pd endpoints, with the fingerprint
SQL wrapping the SAME select so the cache stamp tracks the flag) and the in-memory GNN walk (a post-retrieval
drop, since the walk has no SQL mask). DB-free: dual-alias clause + pure PPR-SQL string checks + a fake-conn
GNN-filter test. (The row-drop semantics of the shared predicate are already proven by TC-4's candidates test.)
"""

from __future__ import annotations

import asyncio


import pythia.graph.ppr as ppr
import pythia.graph.seed as seed
import pythia.ports as ports
from pythia.config import PythiaSettings


def _patch_seed(monkeypatch, *, enabled: bool):
    s = PythiaSettings()
    s.temporal_current_only_enabled = enabled
    monkeypatch.setattr(seed, "get_settings", lambda: s, raising=True)


class _FakeConn:
    def __init__(self, has_col: bool):
        self._has = has_col

    async def fetchval(self, *_a):
        return self._has


# ── the shared clause is dual-alias-capable (default 'p' unchanged; ('ps','pd') for PPR) ──────────
def test_clause_default_alias_is_single_p(monkeypatch):
    _patch_seed(monkeypatch, enabled=True)
    frag = asyncio.run(seed.current_only_clause(_FakeConn(True)))
    assert frag.strip() == seed._CURRENT_ONLY_PRED  # byte-identical to the TC-4b single-p form
    assert frag.count("pit_status IS NULL") == 1


def test_clause_dual_alias_emits_both_endpoints(monkeypatch):
    _patch_seed(monkeypatch, enabled=True)
    frag = asyncio.run(seed.current_only_clause(_FakeConn(True), aliases=("ps", "pd")))
    assert "ps.pit_status IS NULL OR ps.pit_status = 'IN_FORCE'" in frag
    assert "pd.pit_status IS NULL OR pd.pit_status = 'IN_FORCE'" in frag
    assert frag.count("pit_status IS NULL") == 2  # one predicate per endpoint


def test_clause_off_is_empty_for_any_aliases(monkeypatch):
    _patch_seed(monkeypatch, enabled=False)
    assert asyncio.run(seed.current_only_clause(_FakeConn(True), aliases=("ps", "pd"))) == ""


# ── PPR edge-select / fingerprint builders ───────────────────────────────────────────────────────
def test_ppr_edge_select_off_has_no_pit_predicate():
    off = ppr._ppr_edge_select("")
    assert "pit_status" not in off  # OFF (empty clause) ⇒ byte-identical baseline
    assert "ps.valid_from" in off and "pd.valid_from" in off  # the existing dual-endpoint mask is intact


def test_ppr_edge_select_on_appends_both_endpoint_predicates():
    pred = "\n   AND (ps.pit_status IS NULL OR ps.pit_status = 'IN_FORCE')\n   AND (pd.pit_status IS NULL OR pd.pit_status = 'IN_FORCE')"
    on = ppr._ppr_edge_select(pred)
    assert "ps.pit_status = 'IN_FORCE'" in on and "pd.pit_status = 'IN_FORCE'" in on


def test_ppr_fingerprint_wraps_the_same_edge_select():
    # the cache-correctness keystone: the fingerprint SQL contains the EXACT edge-select for the same clause, so
    # a current_only flip that changes the edge-select also changes the fingerprint (cache rebuilds the filtered graph).
    for clause in ("", "\n   AND (ps.pit_status IS NULL OR ps.pit_status = 'IN_FORCE')"):
        assert ppr._ppr_edge_select(clause) in ppr._ppr_fingerprint_sql(clause)


# ── GNN post-retrieval filter (NexusPort._current_only_hits) ─────────────────────────────────────
class _FakePool:
    def __init__(self, conn):
        self._c = conn

    def acquire(self):
        c = self._c

        class _CM:
            async def __aenter__(self):
                return c

            async def __aexit__(self, *_a):
                return False

        return _CM()


class _GnnConn:
    def __init__(self, has_col: bool, stale: list[str]):
        self._has = has_col
        self._stale = stale

    async def fetchval(self, *_a):  # _pit_status_col_present probe
        return self._has

    async def fetch(self, *_a):  # the stale-id lookup
        return [{"id": s} for s in self._stale]


def _hit(pid):
    return ports.NeighborHit(pid, hops=1, score=0.5, relation_path=["gnn"])


def _patch_ports(monkeypatch, *, enabled: bool, conn=None):
    s = PythiaSettings()
    s.temporal_current_only_enabled = enabled
    monkeypatch.setattr(ports, "get_settings", lambda: s, raising=True)
    if conn is not None:

        async def _fake_pool():
            return _FakePool(conn)

        monkeypatch.setattr(ports, "get_pool", _fake_pool, raising=True)


def test_gnn_filter_off_returns_unchanged(monkeypatch):
    # OFF short-circuits BEFORE any pool access — a get_pool that explodes proves it is never touched.
    async def _boom():
        raise AssertionError("pool must not be acquired when current_only is OFF")

    monkeypatch.setattr(ports, "get_pool", _boom, raising=True)
    _patch_ports(monkeypatch, enabled=False)
    hits = [_hit("a"), _hit("b")]
    out = asyncio.run(ports.NexusPort()._current_only_hits(hits))
    assert [h.provision_id for h in out] == ["a", "b"]


def test_gnn_filter_on_drops_known_stale_keeps_rest(monkeypatch):
    conn = _GnnConn(has_col=True, stale=["b"])  # 'b' is SUPERSEDED/NOT_YET; 'a','c' are IN_FORCE/NULL/unknown
    _patch_ports(monkeypatch, enabled=True, conn=conn)
    out = asyncio.run(ports.NexusPort()._current_only_hits([_hit("a"), _hit("b"), _hit("c")]))
    assert [h.provision_id for h in out] == ["a", "c"]  # fail-open: only the known-stale 'b' dropped


def test_gnn_filter_on_column_absent_returns_unchanged(monkeypatch):
    conn = _GnnConn(has_col=False, stale=[])  # pre-activation corpus → no filtering
    _patch_ports(monkeypatch, enabled=True, conn=conn)
    out = asyncio.run(ports.NexusPort()._current_only_hits([_hit("a"), _hit("b")]))
    assert [h.provision_id for h in out] == ["a", "b"]


def test_gnn_filter_empty_hits(monkeypatch):
    _patch_ports(monkeypatch, enabled=True)
    assert asyncio.run(ports.NexusPort()._current_only_hits([])) == []
