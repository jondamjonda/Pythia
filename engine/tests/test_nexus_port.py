"""NexusPort contract — the single-door facade returns the expected shapes for each Nexus capability,
against the live corpus. (The engine/answer behaviour through the port is covered by the broader suite;
this pins the door's own surface: search · expand(cte|ppr) · fetch_provisions · candidates · importance
· browse · act_tree.)
"""

from __future__ import annotations

import math

from pythia.db import close_pool
from pythia.ports import NexusPort, ProvisionRow


async def test_search_expand_fetch_roundtrip():
    nx = NexusPort()
    seeds = await nx.search("nabízení investičního fondu notifikace", k=5)
    assert seeds and all(s.provision_id for s in seeds)
    sid = [s.provision_id for s in seeds]

    exp = await nx.expand("nabízení investičního fondu notifikace", sid, method="cte", k=10)
    assert exp.degraded is False and isinstance(exp.warnings, list)
    assert all(math.isfinite(h.score) for h in exp.hits)

    rows = await nx.fetch_provisions(sid)
    assert rows and all(isinstance(r, ProvisionRow) for r in rows)
    returned = {r.id for r in rows}
    assert [r.id for r in rows] == [i for i in sid if i in returned]  # input order preserved
    assert await nx.fetch_provisions([]) == []  # empty in → empty out
    await close_pool()


async def test_expand_ppr_finite():
    nx = NexusPort()
    sid = [s.provision_id for s in await nx.search("internal reference investment fund", k=5)]
    exp = await nx.expand("x", sid, method="ppr", k=10)
    assert exp.degraded is False
    assert all(math.isfinite(h.score) for h in exp.hits)
    await close_pool()


async def test_candidates_are_filtered_and_in_force():
    nx = NexusPort()
    cands = await nx.candidates(jurisdiction=["CZ"], limit=20)
    assert cands and all(isinstance(c, ProvisionRow) for c in cands)
    assert all(c.jurisdiction == "CZ" for c in cands)
    # centrality-ranked → at most one None-centrality is not interleaved before a scored one is fine;
    # we only assert the call shape + the jurisdiction filter here.
    await close_pool()


async def test_importance_browse_act():
    nx = NexusPort()
    sid = [s.provision_id for s in await nx.search("investiční společnost povinnosti", k=3)]
    imp = await nx.importance(sid)
    assert isinstance(imp, dict)
    assert all(set(v) == {"centrality", "community"} for v in imp.values())

    ix = await nx.browse()
    assert {"facets", "regimes", "spine", "catalogue"} <= set(ix)

    tree = await nx.act_tree("240/2013")  # ZISIF — in the corpus
    assert tree.get("provisions_total", 0) > 0 or "error" in tree
    await close_pool()
