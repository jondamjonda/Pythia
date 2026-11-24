"""S5 — legal_hierarchy retrieval lane + annulled_by exclusion (DB-backed, real ingested rulings).

The N5 case-law ingest gave the live graph CASE_LAW ruling provisions + applies/applied_by edges.
The retrieval masks previously filtered only on valid_to/jurisdiction, so a STATUTE walk could land
on a ruling (via applied_by, statute → ruling) and the seed could surface a ruling — polluting a
statute answer with case law. S5 adds a legal_hierarchy LANE that, by DEFAULT, keeps statute
retrieval statute-only, with an explicit opt-in 'rulings panel' for surfacing case law alongside.

These tests use the LIVE ingested data (a real (statute §, ruling) pair drawn from the case_law
edges). The annulled_by test inserts a SYNTHETIC edge inside a rolled-back transaction so it never
mutates the corpus.

HARD RULE under test: a statute answer must not silently contain case law.
"""

from __future__ import annotations

import pytest

from pythia.db import close_pool, get_pool
from pythia.graph.cte_expand import four_corners, rulings_for_provision
from pythia.graph.relations import PANEL_INVERSE_CASE_LAW_REL_IDS

# These are live-corpus integration tests (real ingested rulings + four_corners over the live graph).
# Each passes in isolation; in a full run they share the live pythia graph + the in-RAM PPR cache with
# the other live mutators (reindex), so they are ordering-sensitive. Mark `live` so `-m "not live"` is the
# deterministic CI gate and the live set runs isolated. (Same convention as test_reindex_*.)
pytestmark = pytest.mark.live


async def _statute_seed_with_ruling() -> str:
    """A real STATUTE provision that is cited by an ingested ruling (has applied_by/interpreted_by
    edges out to CASE_LAW). This is the worst case for pollution — the seed whose four-corners walk
    WOULD pull in rulings if the lane were absent."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        sid = await conn.fetchval(
            """SELECT e.src::text
                 FROM pythia_edge e
                 JOIN provision ps ON ps.id = e.src
                 JOIN provision pd ON pd.id = e.dst
                WHERE e.rel_id = ANY($1::smallint[])
                  AND ps.legal_hierarchy = 'STATUTE'
                  AND pd.legal_hierarchy = 'CASE_LAW'
                GROUP BY e.src
                ORDER BY count(*) DESC, e.src       -- deterministic tiebreak: stable pick across runs
                LIMIT 1""",
            list(PANEL_INVERSE_CASE_LAW_REL_IDS),
        )
    assert sid, "no live (STATUTE §, ruling) pair found — N5 case-law ingest expected in the DB"
    return sid


async def _hierarchies(ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id::text AS id, legal_hierarchy FROM provision WHERE id = ANY($1::uuid[])", ids)
    return {r["id"]: r["legal_hierarchy"] for r in rows}


@pytest.mark.asyncio
async def test_default_four_corners_excludes_case_law():
    """(a) A STATUTE provision's DEFAULT four-corners returns 0 CASE_LAW provisions — even from a
    seed that IS cited by ingested rulings (so applied_by statute→ruling edges exist on it)."""
    seed = await _statute_seed_with_ruling()
    hits = await four_corners([seed], max_hops=3, k=100)
    hmap = await _hierarchies([h.provision_id for h in hits])
    case_law = [h.provision_id for h in hits if hmap.get(h.provision_id) == "CASE_LAW"]
    assert case_law == [], f"statute four-corners leaked {len(case_law)} CASE_LAW provisions: {case_law}"
    # the lane must not also annihilate ordinary statute expansion
    assert hits, "default statute four-corners returned no neighbours at all"
    await close_pool()


@pytest.mark.asyncio
async def test_opt_in_four_corners_surfaces_case_law():
    """The opt-in lane (include_case_law=True) DOES reach the rulings the default lane excludes —
    proving the exclusion is the lane, not a dead graph region."""
    seed = await _statute_seed_with_ruling()
    hits = await four_corners([seed], max_hops=3, k=100, include_case_law=True)
    hmap = await _hierarchies([h.provision_id for h in hits])
    case_law = [h.provision_id for h in hits if hmap.get(h.provision_id) == "CASE_LAW"]
    assert case_law, "opt-in four-corners surfaced no CASE_LAW even though applied_by edges exist"
    await close_pool()


@pytest.mark.asyncio
async def test_rulings_panel_surfaces_ingested_ruling():
    """(b) The rulings PANEL for a statute § actually cited by an ingested ruling surfaces that
    ruling via the INVERSE case-law relations (applied_by / interpreted_by). Every panel hit is a
    real CASE_LAW node, and exactly the (statute §, ruling) edge from the live graph is recovered."""
    seed = await _statute_seed_with_ruling()
    # the ground-truth ruling(s) for this § straight from the live case_law edges
    pool = await get_pool()
    async with pool.acquire() as conn:
        expected = {
            r["dst"]
            for r in await conn.fetch(
                """SELECT e.dst::text AS dst
                     FROM pythia_edge e JOIN provision pd ON pd.id = e.dst
                    WHERE e.src = $1::uuid AND e.rel_id = ANY($2::smallint[])
                      AND pd.legal_hierarchy = 'CASE_LAW'""",
                seed,
                list(PANEL_INVERSE_CASE_LAW_REL_IDS),
            )
        }
    panel = await rulings_for_provision(seed)
    assert panel, "rulings panel returned nothing for a § that IS cited by an ingested ruling"
    # every panel target is a genuine CASE_LAW ruling node
    hmap = await _hierarchies([p.ruling_id for p in panel])
    assert all(hmap.get(p.ruling_id) == "CASE_LAW" for p in panel), "panel returned a non-ruling node"
    # the panel uses only the inverse case-law relations, and includes applied_by (increment-1 rulings)
    rels = {p.relation for p in panel}
    assert rels, "panel produced no relations"
    assert all(p.rel_id in PANEL_INVERSE_CASE_LAW_REL_IDS for p in panel), (
        f"panel used a non-inverse-case-law relation: {rels}"
    )
    # the live ground-truth ruling(s) for this § are recovered
    got = {p.ruling_id for p in panel}
    assert expected and expected <= got, f"panel missed live ruling(s): expected ⊆ got? {expected} vs {got}"
    await close_pool()


@pytest.mark.asyncio
async def test_panel_and_default_walk_are_disjoint_on_case_law():
    """The hard rule, made concrete: the rulings the PANEL surfaces never appear in the same
    provision's DEFAULT statute four-corners (a statute answer must not silently contain case law)."""
    seed = await _statute_seed_with_ruling()
    panel_ids = {p.ruling_id for p in await rulings_for_provision(seed)}
    walk_ids = {h.provision_id for h in await four_corners([seed], max_hops=3, k=100)}
    assert panel_ids, "no panel rulings to test disjointness against"
    assert not (panel_ids & walk_ids), f"panel rulings leaked into the default statute walk: {panel_ids & walk_ids}"
    await close_pool()


@pytest.mark.asyncio
async def test_annulled_by_edge_never_appears_in_four_corners():
    """(c) An annulled_by(28) edge is a status fact, not navigation. Insert a SYNTHETIC annulled_by
    edge (with a deliberately NON-zero weight, to prove the explicit rel_id exclusion — not merely
    weight 0 — is what blocks it) from a statute seed to an UNRELATED STATUTE provision (so the
    CASE_LAW hierarchy filter is NOT what hides it), inside a rolled-back txn, and assert the target
    never surfaces as a four-corners neighbour."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # DETERMINISTIC seed (ORDER BY id). The live `provision` heap order drifts as the sibling DB-mutating
        # live test (reindex) rewrites centrality across the whole table, so an unordered `LIMIT 1` would pick
        # a different seed/target across runs — the root of this test's ordering-flakiness. A fixed pick is
        # reproducible and independent of sibling churn.
        seed = await conn.fetchval(
            "SELECT id::text FROM provision WHERE legal_hierarchy='STATUTE' AND valid_to IS NULL ORDER BY id LIMIT 1"
        )
        assert seed
        # The target must lie OUTSIDE the seed's DEFAULT walk. Excluding only the seed's 1-hop neighbours is
        # NOT enough: the walk runs at max_hops=2, so a 2-hop-reachable § surfaces via REAL traversable edges
        # and would fail the negative assertion below independently of the annulled_by edge (there are dozens
        # of such §; which one an unordered pick lands on drifts with the heap → the flake). Pick a § the
        # baseline walk does NOT already reach, so its later appearance can come ONLY from a synthetic edge.
        baseline = {h.provision_id for h in await four_corners([seed], max_hops=2, k=200, conn=conn)}
        target = await conn.fetchval(
            """SELECT id::text FROM provision
                WHERE legal_hierarchy='STATUTE' AND valid_to IS NULL
                  AND id <> $1::uuid AND id <> ALL($2::uuid[])
                ORDER BY id LIMIT 1""",
            seed,
            list(baseline),
        )
        assert target and target not in baseline

        # Run the walk on THIS connection (conn=conn) so it sees the uncommitted synthetic edge —
        # asyncpg transactions are connection-scoped; a fresh pooled connection would not see it.
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(
                """INSERT INTO pythia_edge (src, dst, rel_id, edge_type, weight, source)
                   VALUES ($1::uuid, $2::uuid, 28, 'annulled_by', 0.9, 'test_synthetic')""",
                seed,
                target,
            )
            hits = await four_corners([seed], max_hops=2, k=200, conn=conn)
            assert target not in {h.provision_id for h in hits}, (
                "annulled_by(28) edge leaked into the four-corners walk (must be non-traversable)"
            )
        finally:
            await tx.rollback()  # never mutate ruling/edge data

        # control: with a traversable references(1) edge instead, the SAME target IS reached —
        # confirms the target is genuinely reachable, so the exclusion above is relation-specific
        # (the rel_id NOT IN (27,28) clause), not an artefact of an unreachable node.
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(
                """INSERT INTO pythia_edge (src, dst, rel_id, edge_type, weight, source)
                   VALUES ($1::uuid, $2::uuid, 1, 'references', 0.9, 'test_synthetic')""",
                seed,
                target,
            )
            reached = {h.provision_id for h in await four_corners([seed], max_hops=2, k=200, conn=conn)}
            assert target in reached, "control references(1) edge was not reached — test setup invalid"
        finally:
            await tx.rollback()
    await close_pool()
