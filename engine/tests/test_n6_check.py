"""N6 S3 — the freshness gate's DETECTOR proof (DB integration).

n6_check must (a) be GREEN on a freshly-built layer, and (b) FAIL HARD the moment the build fingerprint goes
stale. Freshness keys on the edge-set CONTENT HASH, not just the edge count — so it also catches a
COUNT-PRESERVING mutation (a citation redirect / weight change that keeps the edge count constant yet moves
the Louvain/PageRank partition), the blind spot the mini-QC reproduced. We prove the tripwire on BOTH a
count-changing and a count-preserving stale fingerprint, plus the missing-fingerprint case.

We never delete the real fingerprint history (the layer/freshness must survive the test): a stale fingerprint
is appended as a NEWER nexus_event so last_fingerprint() reads it, then removed in teardown.
"""

from __future__ import annotations

import json

from pythia.db import get_pool
from pythia.graph.intelligence import (
    _BUILD_EVENT_KIND,
    _EVENT_DDL,
    build_intelligence,
    current_fingerprint,
)
from pythia.ingest.n6_check import check as n6_check

_STALE_TAG = "test_stale_fingerprint_sentinel"


async def _insert_stale_fingerprint(conn, *, edge_count: int, edge_hash: int) -> None:
    """Append a NEWER fingerprint event (tagged so teardown can find it)."""
    payload = {
        "edge_count": edge_count,
        "edge_hash": edge_hash,
        "node_count": 1,
        "n_communities": 1,
        "top_hub": None,
        "seed": 42,
        "_test_tag": _STALE_TAG,
    }
    await conn.execute(
        "INSERT INTO nexus_event (kind, payload, created_at) VALUES ($1, $2::jsonb, now() + interval '1 second')",
        _BUILD_EVENT_KIND,
        json.dumps(payload),
    )


async def _drop_stale_fingerprints(conn) -> None:
    await conn.execute(
        "DELETE FROM nexus_event WHERE kind = $1 AND payload->>'_test_tag' = $2", _BUILD_EVENT_KIND, _STALE_TAG
    )


async def test_n6_check_green_on_fresh_layer():
    await build_intelligence(apply=True, force=True)  # writes a fingerprint carrying the edge-set hash
    res = await n6_check()
    assert res["ok"] is True, res["checks"]
    assert res["detail"]["has_fingerprint"] is True
    assert res["detail"]["fingerprint_edge_count"] == res["detail"]["live_edge_count"]
    assert res["detail"]["fingerprint_edge_hash"] == res["detail"]["live_edge_hash"]  # content match


class _Rollback(Exception):
    """Raised at the end of a staging txn to roll it back (no committed mutation)."""


async def test_fingerprint_hash_moves_on_count_preserving_mutation():
    """The major mini-QC finding: a count-preserving edit (here, a weight change) must move the content hash
    (so the short-circuit can't skip a needed rebuild). Done in a rolled-back txn — no committed mutation."""
    await build_intelligence(apply=True, force=True)
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                before = await current_fingerprint(conn)
                # bump the weight of ONE edge in the scored set — count unchanged, content changed
                await conn.execute(
                    """UPDATE pythia_edge SET weight = weight + 0.123456 WHERE id = (
                           SELECT e.id FROM pythia_edge e JOIN provision ps ON ps.id=e.src
                             JOIN provision pd ON pd.id=e.dst
                            WHERE e.src<>e.dst AND e.rel_id NOT IN (27,28)
                              AND (ps.valid_to IS NULL OR ps.valid_to>CURRENT_DATE)
                              AND (pd.valid_to IS NULL OR pd.valid_to>CURRENT_DATE)
                            LIMIT 1)"""
                )
                after = await current_fingerprint(conn)
                assert after["edge_count"] == before["edge_count"]  # count is UNCHANGED…
                assert after["edge_hash"] != before["edge_hash"]  # …but the content hash MOVED
                raise _Rollback()
        except _Rollback:
            pass


async def test_n6_check_fails_on_stale_fingerprint_count_changed():
    await build_intelligence(apply=True, force=True)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_EVENT_DDL)
        live = await current_fingerprint(conn)
        await _insert_stale_fingerprint(conn, edge_count=live["edge_count"] - 1, edge_hash=live["edge_hash"])
    try:
        res = await n6_check()
        assert res["ok"] is False
        freshness_key = next(k for k in res["checks"] if k.startswith("N6.5"))
        assert res["checks"][freshness_key] is False
        assert all(v for k, v in res["checks"].items() if not k.startswith("N6.5"))
    finally:
        async with pool.acquire() as conn:
            await _drop_stale_fingerprints(conn)
    assert (await n6_check())["ok"] is True


async def test_n6_check_fails_on_stale_fingerprint_count_preserved():
    """Count identical, hash wrong → the freshness gate must STILL trip (the count-only gate could not)."""
    await build_intelligence(apply=True, force=True)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_EVENT_DDL)
        live = await current_fingerprint(conn)
        await _insert_stale_fingerprint(conn, edge_count=live["edge_count"], edge_hash=live["edge_hash"] ^ 1)
    try:
        res = await n6_check()
        assert res["ok"] is False, "a wrong hash at the same count must trip freshness"
        freshness_key = next(k for k in res["checks"] if k.startswith("N6.5"))
        assert res["checks"][freshness_key] is False
    finally:
        async with pool.acquire() as conn:
            await _drop_stale_fingerprints(conn)
    assert (await n6_check())["ok"] is True


async def test_n6_check_detects_missing_fingerprint_as_stale():
    await build_intelligence(apply=True, force=True)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_EVENT_DDL)
        real = await conn.fetch("SELECT id, payload, created_at FROM nexus_event WHERE kind = $1", _BUILD_EVENT_KIND)
        await conn.execute("DELETE FROM nexus_event WHERE kind = $1", _BUILD_EVENT_KIND)
    try:
        res = await n6_check()
        assert res["detail"]["has_fingerprint"] is False
        assert res["ok"] is False
    finally:
        async with pool.acquire() as conn:
            for row in real:
                p = row["payload"]
                await conn.execute(
                    "INSERT INTO nexus_event (id, kind, payload, created_at) VALUES ($1, $2, $3::jsonb, $4)",
                    row["id"],
                    _BUILD_EVENT_KIND,
                    p if isinstance(p, str) else json.dumps(p),
                    row["created_at"],
                )
    assert (await n6_check())["ok"] is True
