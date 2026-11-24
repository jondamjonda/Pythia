"""N6 (projection) — faceted-browse shaping helpers (pure) + S4 CASE_LAW projection filter (DB).

Pure tests lock the shaping helpers (group_regime_docs top-N, shape_facets null-drop + sort).
The DB tests lock S4: a court ruling (legal_hierarchy='CASE_LAW') must NEVER surface as a spine
entry or a regime anchor, while facets / catalogue / act_tree (which are over ALL documents, incl.
repealed + case law, by design) stay byte-stable against the recorded golden.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pythia.db import get_pool
from pythia.graph.browse import act_tree, browse_index, group_regime_docs, shape_facets

_GOLDEN = json.loads((Path(__file__).parent / "fixtures_browse_golden.json").read_text())


def _canon(obj):
    """Normalise exactly as the golden was serialised (UUID/Decimal -> str, sorted keys)."""
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str, sort_keys=True))


# ---------------------------------------------------------------- pure helpers (no DB)


def test_shape_facets_drops_nulls_and_sorts_by_count():
    rows = {"bindingness": [{"v": "SOFT_LAW", "n": 2}, {"v": None, "n": 9}, {"v": "HARD_LAW", "n": 5}]}
    out = shape_facets(rows)["bindingness"]
    assert out == [{"value": "HARD_LAW", "count": 5}, {"value": "SOFT_LAW", "count": 2}]
    assert all(v["value"] is not None for v in out)  # NULL bucket dropped


def test_shape_facets_breaks_ties_by_value():
    rows = {"jurisdiction": [{"v": "EU", "n": 3}, {"v": "CZ", "n": 3}]}
    out = shape_facets(rows)["jurisdiction"]
    assert [v["value"] for v in out] == ["CZ", "EU"]  # equal counts → value order


def test_group_regime_docs_keeps_top_n_per_community():
    rows = [  # pre-sorted by community, then n desc (as the SQL returns)
        {"community": 0, "ext": "256/2004", "n": 50},
        {"community": 0, "ext": "89/2012", "n": 30},
        {"community": 0, "ext": "634/1992", "n": 10},
        {"community": 0, "ext": "21/1992", "n": 5},
        {"community": 1, "ext": "240/2013", "n": 40},
    ]
    out = group_regime_docs(rows, top=3)
    assert [d["ext"] for d in out[0]] == ["256/2004", "89/2012", "634/1992"]  # 4th dropped
    assert out[1] == [{"ext": "240/2013", "provisions": 40}]


def test_group_regime_docs_empty():
    assert group_regime_docs([]) == {}


# ---------------------------------------------------------------- S4 projection filter (DB)


async def _case_law_provision_ids() -> set[str]:
    """The id (as text) of every provision Nexus classifies as a court ruling."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id::text id FROM provision WHERE legal_hierarchy = 'CASE_LAW'")
    return {r["id"] for r in rows}


async def _provision_ids_by_label_act() -> dict[tuple, str]:
    """(label, source external_id) → provision id::text, to map projected rows back to provisions."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT p.id::text id, p.label, sd.external_id ext
                 FROM provision p JOIN source_document sd ON sd.id = p.source_document_id"""
        )
    return {(r["label"], r["ext"]): r["id"] for r in rows}


async def test_spine_has_no_case_law_entry():
    # the spine is the "these laws matter most" list — a court ruling must never appear on it.
    ix = await browse_index(top_spine=200)  # deep enough to pass rank-103 case law
    assert ix["spine"], "spine empty — centrality layer is stale/NULL"
    by_key = await _provision_ids_by_label_act()
    case_law = await _case_law_provision_ids()
    spine_ids = {by_key[(s["label"], s["ext"])] for s in ix["spine"] if (s["label"], s["ext"]) in by_key}
    assert spine_ids, "could not map any spine row back to a provision id"
    assert not (spine_ids & case_law), "a CASE_LAW provision leaked into the spine"


async def test_no_regime_anchor_is_case_law():
    # a regime's anchor is its "most important law" — it must never be a court ruling.
    ix = await browse_index(top_regimes=200)  # well past the 2 case-law-anchored regimes
    assert ix["regimes"], "regimes empty — community layer is stale/NULL"
    case_law = await _case_law_provision_ids()
    # resolve each non-null anchor (label, ext) back to its provision id
    by_key = await _provision_ids_by_label_act()
    anchor_ids, anchored = set(), 0
    for r in ix["regimes"]:
        a = r["anchor"]
        if a.get("ext") is None and a.get("label") is None:
            continue  # community had no non-CASE_LAW member
        anchored += 1
        key = (a["label"], a["ext"])
        if key in by_key:
            anchor_ids.add(by_key[key])
    assert anchored, "no regime produced an anchor"
    assert not (anchor_ids & case_law), "a CASE_LAW provision was picked as a regime anchor"


async def test_regimes_and_spine_are_nonempty_at_defaults():
    # the intelligence layer is fresh: the default index must surface real regimes + spine.
    ix = await browse_index()
    assert len(ix["spine"]) > 0
    assert len(ix["regimes"]) > 0
    # and the spine is still ordered by descending centrality after the filter
    cents = [s["centrality"] for s in ix["spine"]]
    assert cents == sorted(cents, reverse=True)


# QC-H10 (eng-tests-02): these two compare browse_index() (LIVE DB) against a frozen golden snapshot,
# so they go RED on every corpus growth (the golden was recorded at 628 docs; live is now larger).
# Marked @pytest.mark.live so they are excluded from the deterministic `-m "not live"` suite and only
# run deliberately against a corpus matching the golden (regenerate fixtures_browse_golden.json first).
@pytest.mark.live
async def test_facets_are_byte_stable_vs_golden():
    # facets are over ALL documents (repealed + case law visible by design) — S4 must not touch them.
    # shape_facets gives each axis a total order (count desc, then value), so this is byte-stable.
    ix = await browse_index()
    assert _canon(ix["facets"]) == _GOLDEN["facets"]


@pytest.mark.live
async def test_catalogue_is_byte_stable_vs_golden():
    # the catalogue is the master document list over ALL docs — its CONTENT must be unchanged by the
    # spine/anchor filter. The query's `ORDER BY provisions DESC` has no tiebreaker, so the relative
    # order of equal-count docs is Postgres-arbitrary; compare content keyed on the unique ext, then
    # separately assert the contractual provisions-desc ordering still holds.
    ix = await browse_index()

    def by_ext(rows):
        return sorted(_canon(rows), key=lambda r: r["ext"])

    assert by_ext(ix["catalogue"]) == by_ext(_GOLDEN["catalogue"])  # content byte-stable
    provs = [r["provisions"] for r in ix["catalogue"]]
    assert provs == sorted(provs, reverse=True)  # provisions-desc contract holds


async def test_act_tree_is_byte_stable_vs_golden():
    # act_tree's STABLE contract (document / provisions_total / top_level, ordered by ordinal) must equal the
    # golden. `nodes` and `tree` are ADDITIVE fields: `nodes` is the full ordered provision list (flat,
    # parent-pointer included); `tree` is the NESTED hierarchy_path projection the explorer renders.
    res = await act_tree(_GOLDEN["act_tree_ext"])
    stable = {k: v for k, v in res.items() if k not in ("nodes", "tree")}
    assert _canon(stable) == _GOLDEN["act_tree"]
    assert len(res["nodes"]) == res["provisions_total"]  # nodes is the FULL provision list
    assert all(t in res["nodes"] for t in res["top_level"])  # top_level ⊆ nodes


def _tree_depth(nodes, d=1):
    return max([d] + [_tree_depth(n["children"], d + 1) for n in nodes if n.get("children")])


async def test_act_tree_nests_on_hierarchy_path():
    # the §-tree fix: a CZ code (no parent_id) must come back GENUINELY NESTED on its ltree, not as a flat
    # list of every § — synthetic structural groups (Část/Hlava/Díl/…) at the roots, real depth below them.
    res = await act_tree(_GOLDEN["act_tree_ext"])
    roots = res["tree"]
    assert roots, "act tree has no roots"
    assert any(r["kind"] == "structure" for r in roots), "CZ act did not nest under structural groups"
    assert _tree_depth(roots) >= 3, "tree is too shallow — ltree nesting not applied"
    # every real provision row is reachable in the nested tree exactly once (no drops, no dups)

    def _collect(ns, acc):
        for n in ns:
            if n["kind"] == "provision":
                acc.append(n["id"])
            _collect(n["children"], acc)
        return acc

    ids = _collect(roots, [])
    assert len(ids) == res["provisions_total"] == len(set(ids))


async def test_catalogue_still_includes_case_law_docs():
    # guard the grain split: filtering spine/anchors must NOT hide case law from the catalogue/facets.
    ix = await browse_index()
    hierarchies = {c.get("legal_hierarchy") for c in ix["catalogue"]}
    assert "CASE_LAW" in hierarchies, "case law vanished from the catalogue — grain split broken"
