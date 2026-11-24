"""TC-1 — the temporal CONSUMER proof (DISPLAY-ONLY, gated, additive).

Proves the four TC-1 invariants:
  (a) OFF byte-identical    — the SELECT column list resolved by ports._provision_cols is EXACTLY
      ports._PROVISION_COLS when temporal_consumer_enabled is OFF; resolve_trust output and the
      CitedProvision shape (minus the two new facets) are unchanged.
  (b) ON populates          — same query ON vs OFF yields an IDENTICAL cited-provision SET, identical
      ordering, identical temporal_status / must_surface; the ONLY difference is the new display facets
      pit_status / vector_stale become populated.
  (c) column-absence-safe   — the flag ON on a `provision` schema that LACKS pit_status/embedded_text_hash
      degrades to the OFF select + None facets, NO crash.
  (d) SUPERSEDED + stale     — a SUPERSEDED provision surfaces pit_status='SUPERSEDED'; a stale-vector one
      surfaces vector_stale=True; an in-force/fresh one surfaces 'IN_FORCE'/False.

The DB-touching proofs reuse the EXACT hard-guarded throwaway harness (test_amend_proof._connect_throwaway
+ the _redirect_pool_to_throwaway redirect from test_temporal_substrate): they connect ONLY to the throwaway
'pythia_amendtest' DB and ABORT if current_database() is pythia/regsuper. With no throwaway DB they SKIP
cleanly — never live. The pure projection / OFF-select tests need no DB.

DISPLAY-ONLY: TC-1 must NOT change resolve_trust temporal_status/must_surface, the tier map, the candidate
set, ranking, or refusal — only the two additive CitedProvision facets. The ON-vs-OFF parity test (b) is the
load-bearing proof of that invariant.
"""

from __future__ import annotations

import hashlib
from datetime import date
from uuid import uuid4

import pytest

import pythia.ports as ports
from pythia.config import PythiaSettings
from pythia.graph.trust_resolve import resolve_trust
from tests.test_amend_proof import _FORBIDDEN_DBS, _connect_throwaway, _throwaway_dsn
from tests.test_temporal_substrate import _redirect_pool_to_throwaway


def _h(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def _patch_consumer(monkeypatch, enabled: bool) -> None:
    """Make ports see temporal_consumer_enabled=`enabled`. ports._provision_cols calls the module-level
    get_settings(); patch THAT binding (the fusion-test pattern)."""
    s = PythiaSettings()
    s.temporal_consumer_enabled = enabled
    monkeypatch.setattr(ports, "get_settings", lambda: s, raising=True)


# ─────────────────────────────────────────────────────────── (a) OFF byte-identical (pure, no DB)


def test_consumer_flag_defaults_off():
    assert PythiaSettings().temporal_consumer_enabled is False


async def test_off_select_is_byte_identical(monkeypatch):
    """The resolved column list with the flag OFF is EXACTLY _PROVISION_COLS — no pit_status/stale_vector tail,
    so the OFF SQL is byte-identical to baseline. (_provision_cols short-circuits on the flag BEFORE touching
    the DB, so a sentinel conn that raises if used proves the OFF path never even probes the schema.)"""
    _patch_consumer(monkeypatch, False)

    class _NoConn:
        async def fetchval(self, *a, **k):  # the OFF path must NOT run the information_schema probe
            raise AssertionError("OFF path must not query the DB for column presence")

    cols = await ports._provision_cols(_NoConn())
    assert cols == ports._PROVISION_COLS
    assert "pit_status" not in cols and "stale_vector" not in cols


def test_pit_tail_shape_is_additive_display_only():
    """The TC-1 tail is a pure additive projection: it only adds the two aliased display columns and never
    appears in any WHERE/ORDER BY (those live in the query bodies, not the column list)."""
    assert ports._PROVISION_COLS_PIT_TAIL.strip().startswith(",")
    assert "p.pit_status AS pit_status" in ports._PROVISION_COLS_PIT_TAIL
    assert "stale_vector" in ports._PROVISION_COLS_PIT_TAIL
    assert "p.embedded_text_hash IS DISTINCT FROM p.text_hash" in ports._PROVISION_COLS_PIT_TAIL
    # the OFF column list never carries the tail
    assert "pit_status" not in ports._PROVISION_COLS


def test_resolve_trust_ignores_the_new_facets():
    """DISPLAY-ONLY anchor: resolve_trust reads NEITHER pit_status NOR stale_vector — so populating them on a
    ProvisionRow cannot move temporal_status / must_surface / the tier. Same row, with vs without the facets
    set, resolves to an IDENTICAL TrustTag."""
    base = dict(
        bindingness="HARD_LAW_ERGA_OMNES",
        analysis_roles=[2],
        valid_from=date(2020, 1, 1),
        valid_to=None,
        temporal_status="in-force",
        inherited_kind="operative",
    )
    plain = resolve_trust(
        ports.ProvisionRow(
            id="p",
            label="§1",
            text="x",
            jurisdiction="CZ",
            legal_hierarchy="STATUTE",
            valid_from=base["valid_from"],
            valid_to=base["valid_to"],
            source_title=None,
            external_id=None,
            bindingness=base["bindingness"],
            is_authoritative=True,
            temporal_status=base["temporal_status"],
            analysis_roles=base["analysis_roles"],
            inherited_kind=base["inherited_kind"],
        )
    )
    withf = resolve_trust(
        ports.ProvisionRow(
            id="p",
            label="§1",
            text="x",
            jurisdiction="CZ",
            legal_hierarchy="STATUTE",
            valid_from=base["valid_from"],
            valid_to=base["valid_to"],
            source_title=None,
            external_id=None,
            bindingness=base["bindingness"],
            is_authoritative=True,
            temporal_status=base["temporal_status"],
            analysis_roles=base["analysis_roles"],
            inherited_kind=base["inherited_kind"],
            pit_status="SUPERSEDED",
            stale_vector=True,
        )
    )  # the new facets set …
    assert plain == withf  # … change NOTHING about the resolved trust


# ─────────────────────────────────────────────────────────── throwaway-DB schema + seeding

# pythia_edge — fetch_provisions' WITH RECURSIVE walk reads it for the rel-27/28 annuls-target EXISTS subquery.
# Minimal shape (no rows seeded → annuls_target is always False here, which is fine for the TC-1 facets).
_EDGE_SQL = "DROP TABLE IF EXISTS pythia_edge; CREATE TABLE pythia_edge (rel_id int, src uuid, dst uuid);"

_SETUP_SQL = (
    _EDGE_SQL
    + """
DROP TABLE IF EXISTS provision;
DROP TABLE IF EXISTS source_document;
CREATE TABLE source_document (
    id               uuid PRIMARY KEY,
    external_id      text,
    title            text,
    bindingness      text,
    is_authoritative boolean,
    temporal_status  text,
    legal_domain     text[],
    annulment_effect text
);
CREATE TABLE provision (
    id                  uuid PRIMARY KEY,
    source_document_id  uuid NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
    label               text,
    text                text,
    text_hash           text,
    jurisdiction        text,
    legal_hierarchy     text,
    analysis_roles      int[],
    parent_id           uuid,
    is_anchor_only      boolean DEFAULT false,
    section_kind        text,
    kind_override       text,
    valid_from          date,
    valid_to            date,
    centrality          double precision,
    embedding_bge       text,   -- live is vector(1024); text suffices to prove the IS NOT NULL stale projection
    embedded_text_hash  text,   -- TMP-01
    pit_status          text    -- TMP-02
);
"""
)

# A schema DELIBERATELY LACKING the TC-1 substrate columns (pit_status / embedded_text_hash / embedding_bge) —
# the column-absence-safe witness. Carries everything _PROVISION_COLS needs so the OFF select still works.
_SETUP_SQL_NO_PIT = (
    _EDGE_SQL
    + """
DROP TABLE IF EXISTS provision;
DROP TABLE IF EXISTS source_document;
CREATE TABLE source_document (
    id               uuid PRIMARY KEY,
    external_id      text,
    title            text,
    bindingness      text,
    is_authoritative boolean,
    temporal_status  text,
    legal_domain     text[],
    annulment_effect text
);
CREATE TABLE provision (
    id                  uuid PRIMARY KEY,
    source_document_id  uuid NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
    label               text,
    text                text,
    text_hash           text,
    jurisdiction        text,
    legal_hierarchy     text,
    analysis_roles      int[],
    parent_id           uuid,
    is_anchor_only      boolean DEFAULT false,
    section_kind        text,
    kind_override       text,
    valid_from          date,
    valid_to            date,
    centrality          double precision
);
"""
)


async def _seed(conn, *, with_pit: bool) -> dict[str, str]:
    """Seed one doc + three provisions covering the facet matrix:
      A — IN_FORCE, fresh vector (pit IN_FORCE, vector_stale False)
      B — SUPERSEDED, stale vector (pit SUPERSEDED, vector_stale True)
      C — IN_FORCE, NO vector (pit IN_FORCE, vector_stale False — no embedding ⇒ not stale)
    Returns {name: id}. When with_pit is False the pit/embedding columns simply do not exist on the table."""
    doc = uuid4()
    await conn.execute(
        "INSERT INTO source_document (id, external_id, title, bindingness, is_authoritative, temporal_status,"
        " legal_domain) VALUES ($1,$2,$3,$4,$5,$6,$7)",
        doc,
        "TC1/0001",
        "Test Act",
        "HARD_LAW_ERGA_OMNES",
        True,
        "in-force",
        ["capital_markets"],
    )
    a, b, c = uuid4(), uuid4(), uuid4()
    ta, tb, tc = "obligation A in force", "obligation B superseded", "obligation C no vector"
    base_cols = (
        "id, source_document_id, label, text, text_hash, jurisdiction, legal_hierarchy, "
        "analysis_roles, section_kind, valid_from, valid_to, centrality"
    )
    if with_pit:
        cols = base_cols + ", embedding_bge, embedded_text_hash, pit_status"
        await conn.execute(
            f"INSERT INTO provision ({cols}) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)",
            a,
            doc,
            "§ 1",
            ta,
            _h(ta),
            "CZ",
            "STATUTE",
            [2],
            "operative",
            date(2020, 1, 1),
            None,
            9.0,
            f"vec:{_h(ta)[:8]}",
            _h(ta),
            "IN_FORCE",
        )  # A: fresh
        await conn.execute(
            f"INSERT INTO provision ({cols}) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)",
            b,
            doc,
            "§ 2",
            tb,
            _h(tb),
            "CZ",
            "STATUTE",
            [2],
            "operative",
            date(2019, 1, 1),
            None,
            8.0,
            "vec:old",
            _h("OLD TEXT"),
            "SUPERSEDED",
        )  # B: vector embeds OLD
        await conn.execute(
            f"INSERT INTO provision ({cols}) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)",
            c,
            doc,
            "§ 3",
            tc,
            _h(tc),
            "CZ",
            "STATUTE",
            [2],
            "operative",
            date(2020, 1, 1),
            None,
            7.0,
            None,
            None,
            "IN_FORCE",
        )  # C: no embedding
    else:
        cols = base_cols
        for pid, lbl, txt, cen in ((a, "§ 1", ta, 9.0), (b, "§ 2", tb, 8.0), (c, "§ 3", tc, 7.0)):
            await conn.execute(
                f"INSERT INTO provision ({cols}) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
                pid,
                doc,
                lbl,
                txt,
                _h(txt),
                "CZ",
                "STATUTE",
                [2],
                "operative",
                date(2020, 1, 1),
                None,
                cen,
            )
    return {"A": str(a), "B": str(b), "C": str(c)}


# ─────────────────────────────────────────────────────────── (b) ON populates without changing the set


async def test_on_vs_off_identical_set_only_facets_differ(monkeypatch):
    """The load-bearing DISPLAY-ONLY proof. Same fetch_provisions(ids) ON vs OFF →
    • IDENTICAL provision id SET + ORDER
    • IDENTICAL resolve_trust temporal_status + must_surface per provision
    • the ONLY difference: pit_status / stale_vector are None OFF, populated ON."""
    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TC-1 ON/OFF parity skipped by design (never runs on live)")
    await probe.close()
    dsn = _throwaway_dsn()
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        assert await conn.fetchval("SELECT current_database()") not in _FORBIDDEN_DBS
        await conn.execute(_SETUP_SQL)
        ids_map = await _seed(conn, with_pit=True)
    ids = [ids_map["A"], ids_map["B"], ids_map["C"]]

    # OFF
    async with _redirect_pool_to_throwaway(dsn):
        _patch_consumer(monkeypatch, False)
        off = await ports.NexusPort().fetch_provisions(ids)
    # ON
    async with _redirect_pool_to_throwaway(dsn):
        _patch_consumer(monkeypatch, True)
        on = await ports.NexusPort().fetch_provisions(ids)

    # identical set + order
    assert [r.id for r in off] == ids == [r.id for r in on]
    # identical resolved trust (temporal_status + must_surface) — the protected DISPLAY-ONLY invariant
    for ro, rn in zip(off, on, strict=True):
        to, tn = resolve_trust(ro), resolve_trust(rn)
        assert to.temporal_status == tn.temporal_status
        assert to.must_surface == tn.must_surface
        assert to.bindingness_tier == tn.bindingness_tier
    # OFF leaves the new facets None; ON populates them
    assert all(r.pit_status is None and r.stale_vector is None for r in off)
    by = {r.id: r for r in on}
    assert by[ids_map["A"]].pit_status == "IN_FORCE" and by[ids_map["A"]].stale_vector is False
    assert by[ids_map["B"]].pit_status == "SUPERSEDED" and by[ids_map["B"]].stale_vector is True
    assert by[ids_map["C"]].pit_status == "IN_FORCE" and by[ids_map["C"]].stale_vector is False


# ─────────────────────────────────────────────────────────── (c) column-absence-safe


async def test_flag_on_column_absent_degrades_to_none_no_crash(monkeypatch):
    """Flag ON on a schema that LACKS pit_status/embedded_text_hash/embedding_bge: _provision_cols must degrade
    to the OFF select and fetch_provisions must return the rows with None facets — NO crash."""
    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TC-1 column-absence proof skipped by design")
    await probe.close()
    dsn = _throwaway_dsn()
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        assert await conn.fetchval("SELECT current_database()") not in _FORBIDDEN_DBS
        await conn.execute(_SETUP_SQL_NO_PIT)
        ids_map = await _seed(conn, with_pit=False)
        # the guard itself reports the columns absent
        assert await ports._pit_cols_present(conn) is False
    ids = [ids_map["A"], ids_map["B"], ids_map["C"]]

    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        _patch_consumer(monkeypatch, True)
        # flag is ON, but the guard forces the byte-identical OFF select (columns absent)
        assert await ports._provision_cols(conn) == ports._PROVISION_COLS
        rows = await ports.NexusPort().fetch_provisions(ids)  # must NOT raise

    assert [r.id for r in rows] == ids
    assert all(r.pit_status is None and r.stale_vector is None for r in rows)


async def test_pit_cols_present_true_when_substrate_exists(monkeypatch):
    """The positive arm of the guard: with the substrate columns present, _pit_cols_present is True and the ON
    select carries the additive tail."""
    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TC-1 column-presence proof skipped by design")
    await probe.close()
    dsn = _throwaway_dsn()
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        await conn.execute(_SETUP_SQL)
        await _seed(conn, with_pit=True)
        assert await ports._pit_cols_present(conn) is True
        _patch_consumer(monkeypatch, True)
        cols = await ports._provision_cols(conn)
        assert cols == ports._PROVISION_COLS + ports._PROVISION_COLS_PIT_TAIL
        # OFF on the SAME present-column schema is still byte-identical
        _patch_consumer(monkeypatch, False)
        assert await ports._provision_cols(conn) == ports._PROVISION_COLS


# ─────────────────────────────────────────────────────────── (d) SUPERSEDED + stale surfacing via candidates


async def test_candidates_on_surfaces_pit_and_stale(monkeypatch):
    """The other SELECT site (candidates) also surfaces the facets when ON: a SUPERSEDED provision shows
    pit_status='SUPERSEDED', a stale one shows vector_stale=True — and the candidate SET/ORDER (centrality desc)
    is identical ON vs OFF."""
    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TC-1 candidates facet proof skipped by design")
    await probe.close()
    dsn = _throwaway_dsn()
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        await conn.execute(_SETUP_SQL)
        ids_map = await _seed(conn, with_pit=True)

    async with _redirect_pool_to_throwaway(dsn):
        _patch_consumer(monkeypatch, False)
        # include SUPERSEDED B by passing as_of in its window is unnecessary: candidates filters on valid_to;
        # B has valid_to NULL (open) so it stays in the in-force candidate pool — its pit_status SUPERSEDED is a
        # SEPARATE per-provision signal (a closed head), exactly what TC-1 surfaces honestly. include_case_law
        # irrelevant (all STATUTE).
        off = await ports.NexusPort().candidates(jurisdiction=["CZ"], limit=50)
    async with _redirect_pool_to_throwaway(dsn):
        _patch_consumer(monkeypatch, True)
        on = await ports.NexusPort().candidates(jurisdiction=["CZ"], limit=50)

    assert [r.id for r in off] == [r.id for r in on]  # identical centrality-desc set + order
    assert all(r.pit_status is None for r in off)
    by = {r.id: r for r in on}
    assert by[ids_map["B"]].pit_status == "SUPERSEDED" and by[ids_map["B"]].stale_vector is True
    assert by[ids_map["A"]].pit_status == "IN_FORCE" and by[ids_map["A"]].stale_vector is False


# ─────────────────────────────────────────────────────────── (e) anchor-only exclusion matches the canonical predicate


async def test_anchor_only_with_changed_hash_is_not_stale(monkeypatch):
    """The stale_vector projection matches TMP-01 stale_vector_ids() EXACTLY: an is_anchor_only row that
    (pathologically) carries a vector whose embedded hash mismatches text_hash is NOT reported stale — anchors
    are excluded by the `NOT is_anchor_only` clause — while an identical NON-anchor row IS stale."""
    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TC-1 anchor-exclusion proof skipped by design")
    await probe.close()
    dsn = _throwaway_dsn()
    anchor, normal = uuid4(), uuid4()
    cols = (
        "id, source_document_id, label, text, text_hash, jurisdiction, legal_hierarchy, analysis_roles,"
        " section_kind, valid_from, valid_to, centrality, is_anchor_only, embedding_bge,"
        " embedded_text_hash, pit_status"
    )
    ph = "$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16"
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        await conn.execute(_SETUP_SQL)
        doc = uuid4()
        await conn.execute(
            "INSERT INTO source_document (id, external_id, title, bindingness, is_authoritative, temporal_status,"
            " legal_domain) VALUES ($1,$2,$3,$4,$5,$6,$7)",
            doc,
            "TC1/ANCHOR",
            "Anchor Act",
            "HARD_LAW_ERGA_OMNES",
            True,
            "in-force",
            ["capital_markets"],
        )
        # both carry a vector whose embedded hash != current text_hash (the canonical 'stale' shape); they differ
        # ONLY on is_anchor_only — so the predicate's anchor exclusion is the sole cause of the divergent verdict.
        await conn.execute(
            f"INSERT INTO provision ({cols}) VALUES ({ph})",
            anchor,
            doc,
            "ČÁST PRVNÍ",
            "structural",
            _h("new"),
            "CZ",
            "STATUTE",
            [],
            "operative",
            date(2020, 1, 1),
            None,
            1.0,
            True,
            "vec:a",
            _h("OLD"),
            "IN_FORCE",
        )
        await conn.execute(
            f"INSERT INTO provision ({cols}) VALUES ({ph})",
            normal,
            doc,
            "§ 1",
            "operative text",
            _h("new"),
            "CZ",
            "STATUTE",
            [2],
            "operative",
            date(2020, 1, 1),
            None,
            9.0,
            False,
            "vec:n",
            _h("OLD"),
            "IN_FORCE",
        )
    async with _redirect_pool_to_throwaway(dsn):
        _patch_consumer(monkeypatch, True)
        rows = {r.id: r for r in await ports.NexusPort().fetch_provisions([str(anchor), str(normal)])}
    assert rows[str(normal)].stale_vector is True  # identical shape, non-anchor → stale
    assert rows[str(anchor)].stale_vector is False  # anchor excluded (matches stale_vector_ids())
