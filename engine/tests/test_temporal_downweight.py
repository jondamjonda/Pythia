"""TC-2 — the temporal DOWN-WEIGHT proof (BEHAVIOUR-CHANGING, gated, additive).

The safety payoff of the temporal substrate: stop surfacing per-provision-SUPERSEDED law as a CURRENT
must-mention rule. Proves the TC-2 invariants:

  (a) OFF byte-identical    — resolve_trust(row) (no downweight) and must_surface are unchanged for EVERY
      input; the refinement applies ONLY with downweight=True. The new flag defaults OFF.
  (b) the SAFETY WIN        — a provision whose doc/dates say in-force but whose pit_status='SUPERSEDED'
      resolves temporal_status='superseded' and must_surface=False when downweight=True.
  (c) the CARDINAL never-upgrade — a provision the DOC level calls 'superseded'/'annulled' is NEVER refined
      back to active even if pit_status='IN_FORCE' (pit can only restrict, never un-restrict).
  (d) NOT_YET_IN_FORCE      — pit NOT_YET_IN_FORCE on an in-force provision ⇒ future-effective (still active,
      more accurate); pit IN_FORCE / None / unknown ⇒ unchanged.
  (e) MONOTONE-RESTRICTIVE  — over the full (step1 × pit) matrix the refined status is NEVER more active than
      step1, and a provision either signal calls superseded/annulled is never surfaced as current.
  (f) candidate set unchanged — fetch_provisions ON vs OFF returns the IDENTICAL id SET + ORDER; only the
      resolved must_surface/temporal_status of a per-provision-superseded rule differs (it is no longer
      must-surfaced). Retrieval/ranking/candidate generation is untouched.

The DB-touching proofs reuse the EXACT hard-guarded throwaway harness (test_amend_proof._connect_throwaway +
test_temporal_substrate._redirect_pool_to_throwaway): they connect ONLY to the throwaway 'pythia_amendtest' DB
and ABORT if current_database() is pythia/regsuper. With no throwaway DB they SKIP cleanly — never live. Each
DB test CASCADE-drops its tables on the way out so the shared pythia_amendtest DB does not leak FK-bearing
tables to other tests. The pure-resolver proofs need no DB.
"""

from __future__ import annotations

import hashlib
from datetime import date
from uuid import uuid4

import pytest

import pythia.ports as ports
from pythia.config import PythiaSettings
from pythia.graph.trust_resolve import (
    ACTIVE_TEMPORAL,
    _refine_temporal_with_pit,
    resolve_trust,
)
from tests.test_amend_proof import _FORBIDDEN_DBS, _connect_throwaway, _throwaway_dsn
from tests.test_temporal_consumer import _SETUP_SQL
from tests.test_temporal_substrate import _redirect_pool_to_throwaway

# the active vs not-active resolved temporal vocab
_ACTIVE = frozenset({"in-force", "future-effective"})
_NOT_ACTIVE = frozenset({"annulled", "superseded"})
# how "active" a resolved status is — higher = MORE active. Used for the monotone-restrictive proof.
_ACTIVITY_RANK = {"superseded": 0, "annulled": 0, "future-effective": 1, "in-force": 2}


def _h(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def _patch_downweight(monkeypatch, *, downweight: bool, consumer: bool = False) -> None:
    """Make ports see temporal_downweight_enabled=`downweight` (and consumer flag as given). ports._provision_cols
    reads the module-level get_settings(); patch THAT binding (the TC-1 test pattern)."""
    s = PythiaSettings()
    s.temporal_downweight_enabled = downweight
    s.temporal_consumer_enabled = consumer
    monkeypatch.setattr(ports, "get_settings", lambda: s, raising=True)


def _prow(**kw):
    """A ProvisionRow with safe rule-active defaults; override per test (a HARD_LAW operative obligation)."""
    base = dict(
        id="p",
        label="§1",
        text="x",
        jurisdiction="CZ",
        legal_hierarchy="STATUTE",
        valid_from=date(2000, 1, 1),
        valid_to=None,
        source_title=None,
        external_id=None,
        bindingness="HARD_LAW_ERGA_OMNES",
        is_authoritative=True,
        temporal_status="in-force",
        analysis_roles=[2],
        inherited_kind="operative",
    )
    base.update(kw)
    return ports.ProvisionRow(**base)


AS_OF = date(2024, 1, 1)


# ─────────────────────────────────────────────────────────── (a) the flag + OFF byte-identical (pure)


def test_downweight_flag_defaults_off():
    assert PythiaSettings().temporal_downweight_enabled is False


def test_off_is_byte_identical_over_pit_matrix():
    """The DEFAULT (downweight not passed / =False) NEVER reads pit_status: the resolved TrustTag is identical
    regardless of pit_status, AND identical to resolve_trust without downweight. The refinement only fires ON."""
    for pit in (None, "IN_FORCE", "SUPERSEDED", "NOT_YET_IN_FORCE", "GARBAGE"):
        row = _prow(pit_status=pit)
        default = resolve_trust(row, AS_OF)  # no downweight kwarg (the default path)
        explicit_off = resolve_trust(row, AS_OF, downweight=False)
        assert default == explicit_off  # the kwarg default is byte-identical
        # and identical to a row that carries NO pit_status at all (pit is never consulted OFF)
        bare = resolve_trust(_prow(pit_status=None), AS_OF, downweight=False)
        assert default.temporal_status == bare.temporal_status
        assert default.must_surface == bare.must_surface
        assert default.temporal_status == "in-force" and default.must_surface is True


def test_off_no_pit_reason_in_trail():
    """OFF leaves no pit-downweight reason in the audit trail even with pit_status='SUPERSEDED'."""
    row = _prow(pit_status="SUPERSEDED")
    tag = resolve_trust(row, AS_OF)  # OFF
    assert not any("pit-downweight" in r for r in tag.reasons)
    assert tag.temporal_status == "in-force" and tag.must_surface is True


# ─────────────────────────────────────────────────────────── (b) the SAFETY WIN


def test_safety_win_per_provision_superseded_not_surfaced():
    """A binding obligation whose doc-level status + open valid_to window say IN-FORCE, but whose head version
    is SUPERSEDED (pit_status='SUPERSEDED'): downweight=True ⇒ temporal_status='superseded', must_surface=False."""
    row = _prow(temporal_status="in-force", valid_to=None, pit_status="SUPERSEDED")
    off = resolve_trust(row, AS_OF, downweight=False)
    on = resolve_trust(row, AS_OF, downweight=True)
    # OFF: surfaced as a current rule
    assert off.temporal_status == "in-force" and off.must_surface is True
    # ON: refined to superseded, dropped from the must-mention register
    assert on.temporal_status == "superseded" and on.must_surface is False
    assert on.is_rule is True  # still a rule — just not a CURRENT one
    assert any("pit-downweight in-force→superseded" in r for r in on.reasons)


def test_safety_win_future_effective_provision_superseded():
    """A future-effective provision (valid_from in the future) whose head is SUPERSEDED also drops to superseded
    (monotone: future-effective is active → pit SUPERSEDED restricts it)."""
    row = _prow(valid_from=date(2030, 1, 1), temporal_status="in-force", pit_status="SUPERSEDED")
    off = resolve_trust(row, AS_OF, downweight=False)
    on = resolve_trust(row, AS_OF, downweight=True)
    assert off.temporal_status == "future-effective" and off.must_surface is True
    assert on.temporal_status == "superseded" and on.must_surface is False


# ─────────────────────────────────────────────────────────── (c) the CARDINAL never-upgrade


@pytest.mark.parametrize("pit", [None, "IN_FORCE", "NOT_YET_IN_FORCE", "SUPERSEDED", "GARBAGE"])
def test_never_upgrade_doc_superseded(pit):
    """A doc-level SUPERSEDED provision (expired valid_to, no annulment) is NEVER refined back to active —
    pit_status (even 'IN_FORCE') can only restrict, never un-restrict."""
    row = _prow(valid_to=date(2010, 1, 1), temporal_status="in-force", pit_status=pit)  # expired → superseded
    base = resolve_trust(row, AS_OF, downweight=False)
    on = resolve_trust(row, AS_OF, downweight=True)
    assert base.temporal_status == "superseded"
    assert on.temporal_status == "superseded"  # held, never upgraded
    assert on.must_surface is False


@pytest.mark.parametrize("pit", [None, "IN_FORCE", "NOT_YET_IN_FORCE", "SUPERSEDED", "GARBAGE"])
def test_never_upgrade_doc_annulled(pit):
    """A doc-level ANNULLED provision (CC EX_NUNC/EX_TUNC derogation) is NEVER refined back to active even with
    pit_status='IN_FORCE'. The cardinal never-upgrade rule applies to annulled too."""
    row = _prow(temporal_status="ANNULLED_BY_CC", annulment_effect="EX_TUNC", pit_status=pit)
    base = resolve_trust(row, AS_OF, downweight=False)
    on = resolve_trust(row, AS_OF, downweight=True)
    assert base.temporal_status == "annulled"
    assert on.temporal_status == "annulled"
    assert on.must_surface is False


# ─────────────────────────────────────────────────────────── (d) NOT_YET_IN_FORCE + IN_FORCE / None


def test_not_yet_in_force_on_in_force_becomes_future_effective():
    """An in-force provision whose head pit is NOT_YET_IN_FORCE ⇒ future-effective (still ACTIVE → must_surface
    stays True, just under the upcoming banner)."""
    row = _prow(temporal_status="in-force", pit_status="NOT_YET_IN_FORCE")
    on = resolve_trust(row, AS_OF, downweight=True)
    assert on.temporal_status == "future-effective"
    assert on.must_surface is True
    assert "future-effective" in ACTIVE_TEMPORAL


def test_pit_in_force_and_none_are_no_ops():
    """pit IN_FORCE / None / unknown NEVER change an active step1 status (pit IN_FORCE never upgrades)."""
    for pit in (None, "IN_FORCE", "GARBAGE"):
        row = _prow(temporal_status="in-force", pit_status=pit)
        on = resolve_trust(row, AS_OF, downweight=True)
        assert on.temporal_status == "in-force" and on.must_surface is True


def test_not_yet_in_force_does_not_upgrade_future_effective():
    """A future-effective provision (valid_from future) with pit NOT_YET_IN_FORCE stays future-effective — the
    NOT_YET arm only fires when step1=='in-force', so it never adds activity."""
    row = _prow(valid_from=date(2030, 1, 1), temporal_status="in-force", pit_status="NOT_YET_IN_FORCE")
    on = resolve_trust(row, AS_OF, downweight=True)
    assert on.temporal_status == "future-effective" and on.must_surface is True


# ─────────────────────────────────────────────────────────── (e) MONOTONE-RESTRICTIVE property


@pytest.mark.parametrize("step1", ["in-force", "future-effective", "superseded", "annulled"])
@pytest.mark.parametrize("pit", [None, "IN_FORCE", "SUPERSEDED", "NOT_YET_IN_FORCE", "GARBAGE", "in_force"])
def test_pure_refine_is_monotone_restrictive(step1, pit):
    """The pure helper over the full (step1 × pit) matrix:
    • the refined status is NEVER more active than step1 (monotone-restrictive);
    • a step1 already not-active is returned UNCHANGED (never-upgrade);
    • the result is always a valid temporal vocab value."""
    out = _refine_temporal_with_pit(step1, pit)
    assert out in {"in-force", "future-effective", "superseded", "annulled"}
    assert _ACTIVITY_RANK[out] <= _ACTIVITY_RANK[step1]  # never MORE active
    if step1 in _NOT_ACTIVE:
        assert out == step1  # never-upgrade: held exactly
    # the safety win: a SUPERSEDED pit on an active step1 always yields a not-active result
    if step1 in _ACTIVE and (pit or "").upper() == "SUPERSEDED":
        assert out == "superseded"


def test_pure_refine_specific_cells():
    """Spot-check the four behaviour cells of the truth table."""
    assert _refine_temporal_with_pit("in-force", "SUPERSEDED") == "superseded"
    assert _refine_temporal_with_pit("in-force", "NOT_YET_IN_FORCE") == "future-effective"
    assert _refine_temporal_with_pit("in-force", "IN_FORCE") == "in-force"
    assert _refine_temporal_with_pit("in-force", None) == "in-force"
    assert _refine_temporal_with_pit("future-effective", "NOT_YET_IN_FORCE") == "future-effective"  # no upgrade
    assert _refine_temporal_with_pit("superseded", "IN_FORCE") == "superseded"  # never-upgrade
    assert _refine_temporal_with_pit("annulled", "IN_FORCE") == "annulled"  # never-upgrade


def test_pure_refine_normalises_whitespace_and_non_str_pit():
    """Defensive hardening: a whitespace-padded or non-str pit_status must NOT silently weaken the safety win.
    `str(pit or '').strip().upper()` so ' superseded ' still fires, and a non-str degrades safely (never raises,
    never upgrades). (pit comes from a Postgres text column today, so these are belt-and-suspenders.)"""
    assert (
        _refine_temporal_with_pit("in-force", " SUPERSEDED ") == "superseded"
    )  # whitespace stripped → safety win fires
    assert _refine_temporal_with_pit("in-force", "superseded") == "superseded"  # lowercase normalised
    assert _refine_temporal_with_pit("in-force", 0) == "in-force"  # non-str (falsy) → no raise, no change
    assert (
        _refine_temporal_with_pit("in-force", ["SUPERSEDED"]) == "in-force"
    )  # non-str (truthy) → no raise, no upgrade
    assert _refine_temporal_with_pit("superseded", " IN_FORCE ") == "superseded"  # never-upgrade still holds


# ─────────────────────────────────────────────────────────── ports gate: pit carried for TC-2 even if TC-1 off


async def test_provision_cols_off_byte_identical_both_flags_off(monkeypatch):
    """BOTH flags OFF ⇒ _provision_cols is byte-identical _PROVISION_COLS and never even probes the schema."""
    _patch_downweight(monkeypatch, downweight=False, consumer=False)

    class _NoConn:
        async def fetchval(self, *a, **k):
            raise AssertionError("OFF path must not query the DB for column presence")

    cols = await ports._provision_cols(_NoConn())
    assert cols == ports._PROVISION_COLS
    assert "pit_status" not in cols


async def test_provision_cols_carries_pit_when_only_downweight_on(monkeypatch):
    """The widened gate: with temporal_downweight_enabled ON (and TC-1 display OFF) AND the substrate columns
    present, _provision_cols appends the pit tail so the row carries pit_status for the resolver's refinement."""
    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TC-2 ports-gate proof skipped by design (never runs on live)")
    await probe.close()
    dsn = _throwaway_dsn()
    try:
        async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
            assert await conn.fetchval("SELECT current_database()") not in _FORBIDDEN_DBS
            await conn.execute(_SETUP_SQL)
            # TC-1 display OFF, TC-2 down-weight ON
            _patch_downweight(monkeypatch, downweight=True, consumer=False)
            cols = await ports._provision_cols(conn)
            assert cols == ports._PROVISION_COLS + ports._PROVISION_COLS_PIT_TAIL
            # both OFF on the same present-column schema is still byte-identical
            _patch_downweight(monkeypatch, downweight=False, consumer=False)
            assert await ports._provision_cols(conn) == ports._PROVISION_COLS
    finally:
        await _drop_all(dsn)


# ─────────────────────────────────────────────────────────── (f) candidate set unchanged ON vs OFF


async def _seed_super(conn) -> dict[str, str]:
    """One doc + two binding obligations, both in the in-force candidate pool (open valid_to):
      A — pit IN_FORCE     (a genuine current rule)
      B — pit SUPERSEDED   (doc/dates in-force, head superseded — the safety-win subject)
    Returns {name: id}."""
    doc = uuid4()
    await conn.execute(
        "INSERT INTO source_document (id, external_id, title, bindingness, is_authoritative, temporal_status,"
        " legal_domain) VALUES ($1,$2,$3,$4,$5,$6,$7)",
        doc,
        "TC2/0001",
        "Test Act",
        "HARD_LAW_ERGA_OMNES",
        True,
        "in-force",
        ["capital_markets"],
    )
    a, b = uuid4(), uuid4()
    ta, tb = "obligation A current", "obligation B head superseded"
    cols = (
        "id, source_document_id, label, text, text_hash, jurisdiction, legal_hierarchy, analysis_roles,"
        " section_kind, valid_from, valid_to, centrality, embedding_bge, embedded_text_hash, pit_status"
    )
    ph = "$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15"
    await conn.execute(
        f"INSERT INTO provision ({cols}) VALUES ({ph})",
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
    )
    await conn.execute(
        f"INSERT INTO provision ({cols}) VALUES ({ph})",
        b,
        doc,
        "§ 2",
        tb,
        _h(tb),
        "CZ",
        "STATUTE",
        [2],
        "operative",
        date(2020, 1, 1),
        None,
        8.0,
        f"vec:{_h(tb)[:8]}",
        _h(tb),
        "SUPERSEDED",
    )
    return {"A": str(a), "B": str(b)}


async def _drop_all(dsn) -> None:
    """CASCADE-drop the throwaway tables so the shared pythia_amendtest DB does not leak FK-bearing tables."""
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        assert await conn.fetchval("SELECT current_database()") not in _FORBIDDEN_DBS
        await conn.execute(
            "DROP TABLE IF EXISTS provision CASCADE;"
            "DROP TABLE IF EXISTS source_document CASCADE;"
            "DROP TABLE IF EXISTS pythia_edge CASCADE;"
        )


async def test_candidate_set_unchanged_only_must_surface_differs(monkeypatch):
    """The load-bearing TC-2 retrieval invariant. fetch_provisions(ids) ON vs OFF →
    • IDENTICAL provision id SET + ORDER (the candidate set / retrieval is UNCHANGED — a superseded provision
      is still RETRIEVED as historical context, never dropped);
    • the ONLY behavioural difference: the per-provision-SUPERSEDED rule B is must_surface=True OFF but
      must_surface=False ON (temporal_status in-force→superseded). The genuine current rule A is unchanged."""
    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TC-2 candidate-parity proof skipped by design (never runs on live)")
    await probe.close()
    dsn = _throwaway_dsn()
    try:
        async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
            assert await conn.fetchval("SELECT current_database()") not in _FORBIDDEN_DBS
            await conn.execute(_SETUP_SQL)
            ids_map = await _seed_super(conn)
        ids = [ids_map["A"], ids_map["B"]]

        # OFF — pit not carried (consumer OFF too), no refinement
        async with _redirect_pool_to_throwaway(dsn):
            _patch_downweight(monkeypatch, downweight=False, consumer=False)
            off = await ports.NexusPort().fetch_provisions(ids)
        # ON — pit carried by the widened gate, resolver refines with downweight=True
        async with _redirect_pool_to_throwaway(dsn):
            _patch_downweight(monkeypatch, downweight=True, consumer=False)
            on = await ports.NexusPort().fetch_provisions(ids)

        # the candidate SET + ORDER is identical (retrieval unchanged — nothing dropped/reordered)
        assert [r.id for r in off] == ids == [r.id for r in on]

        off_by = {r.id: resolve_trust(r, downweight=False) for r in off}
        on_by = {r.id: resolve_trust(r, downweight=True) for r in on}

        # A — a genuine current rule: unchanged ON vs OFF
        assert off_by[ids_map["A"]].temporal_status == "in-force"
        assert on_by[ids_map["A"]].temporal_status == "in-force"
        assert off_by[ids_map["A"]].must_surface is True and on_by[ids_map["A"]].must_surface is True

        # B — the safety win: must_surface True OFF, False ON (head superseded ⇒ not a current rule)
        assert off_by[ids_map["B"]].temporal_status == "in-force"
        assert off_by[ids_map["B"]].must_surface is True
        assert on_by[ids_map["B"]].temporal_status == "superseded"
        assert on_by[ids_map["B"]].must_surface is False
        # OFF rows do not even carry pit_status (consumer + downweight both off); ON rows do
        assert off[1].pit_status is None
        assert {r.id: r.pit_status for r in on}[ids_map["B"]] == "SUPERSEDED"
    finally:
        await _drop_all(dsn)


async def test_engine_decorate_must_surface_drops_superseded(monkeypatch):
    """End-to-end at the engine decorate site: PythiaEngine._decorate threads downweight from the flag, so a
    per-provision-SUPERSEDED binding rule is excluded from the obligation_register when the flag is ON, while the
    cited_provisions SET is unchanged (it is still cited as historical context)."""
    from pythia.contract import PythiaAnswer
    from pythia.engine import PythiaEngine
    from pythia.graph.cte_expand import NeighborHit

    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TC-2 engine-decorate proof skipped by design (never runs on live)")
    await probe.close()
    dsn = _throwaway_dsn()
    try:
        async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
            assert await conn.fetchval("SELECT current_database()") not in _FORBIDDEN_DBS
            await conn.execute(_SETUP_SQL)
            ids_map = await _seed_super(conn)
        hits = [
            NeighborHit(ids_map["A"], hops=0, score=2.0, relation_path=["seed"]),
            NeighborHit(ids_map["B"], hops=0, score=1.9, relation_path=["seed"]),
        ]

        async def _decorate(downweight: bool):
            async with _redirect_pool_to_throwaway(dsn):
                _patch_downweight(monkeypatch, downweight=downweight, consumer=False)
                eng = PythiaEngine()
                cited = await eng._decorate(hits, downweight=downweight)
            return cited

        off = await _decorate(False)
        on = await _decorate(True)

        # the cited set is unchanged (B is still cited — never dropped)
        assert [c.provision_id for c in off] == [ids_map["A"], ids_map["B"]]
        assert [c.provision_id for c in on] == [ids_map["A"], ids_map["B"]]

        # the obligation register (must_surface) loses B when ON
        reg_off = [c.provision_id for c in off if c.trust and c.trust.must_surface]
        reg_on = [c.provision_id for c in on if c.trust and c.trust.must_surface]
        assert ids_map["B"] in reg_off  # OFF: B surfaced as current
        assert ids_map["B"] not in reg_on  # ON: B down-weighted out of the register
        assert ids_map["A"] in reg_off and ids_map["A"] in reg_on  # A unchanged
        # sanity on the answer shape (the register reflects the cited trust)
        assert isinstance(PythiaAnswer(cited_provisions=on, obligation_register=reg_on).obligation_register, list)
    finally:
        await _drop_all(dsn)
