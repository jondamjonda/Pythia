"""TMP-01 + TMP-02 — the temporal-substrate PROOF HARNESS (throwaway-DB, hard-guarded).

Proves the two coupled #64 substrate pieces on a CLEARLY-throwaway database, reusing the exact hard-guard
pattern of tests/test_amend_proof.py (it refuses to ever run on live pythia/regsuper):

  TMP-01 — re-embed-on-amend + the staleness INVARIANT (models a FUTURE version-aware IN-PLACE amend;
  today's amend() delete+reinserts with NULL vectors, so this is defense-in-depth — see the module docstring
  in pythia/ingest/embed_bge.py):
    • the embedder stamps embedded_text_hash in the SAME UPDATE that writes the vector;
    • a provision whose text is updated IN PLACE (new text_hash, vector untouched) reads STALE
      (embedded_text_hash <> text_hash);
    • the SCOPED re-embed reconciles ONLY the changed rows (not a corpus-wide sweep) → equal again;
    • a NON-amended provision is never re-embedded.

  TMP-02 — the per-provision temporal-position facet (pit_status):
    • IN_FORCE / SUPERSEDED / NOT_YET_IN_FORCE are CORRECT vs the head provision_version window;
    • recompute is idempotent + network-free + NULLs-nothing;
    • date-crossing (NOT_YET → IN_FORCE at a later as_of, exactly 1 change), fully-repealed-no-replacement
      lineage (closed head, no open head → SUPERSEDED), and future/past as_of are all honored;
    • the partial index (WHERE pit_status='IN_FORCE') is unwired substrate (no reader filters on it today);
    • the stale-vector flag is queryable;
    • pit_status is a SEPARATE thing from the doc-level source_document.temporal_status (no collision).

  THE DEFAULT-OFF GATE (Finding 3, the crux):
    • the flag ships default-OFF;
    • with the flag OFF the REAL embed_all writes NO embedded_text_hash and creates no TMP-01 column on a
      non-activated corpus (proven against a table that deliberately lacks the column);
    • with the flag OFF scheduler.run_temporal returns the bare DOC-level recompute (pre-TMP-02 shape) and
      never calls recompute_provision_temporal_status (patched to raise);
    • with the flag ON scheduler.run_temporal returns BOTH 'doc' and 'provision' facets.

ABSOLUTE SAFETY — this NEVER touches the live corpus. It connects ONLY to the throwaway DB resolved by
test_amend_proof._connect_throwaway() (env PYTHIA_AMENDTEST_URL, else a 'pythia_amendtest' DB derived from
the DSN), and a HARD GUARD aborts if current_database() is 'pythia'/'regsuper'. With no throwaway DB the
whole module SKIPS cleanly (the expected default).

The embedder itself is NOT invoked here (it loads a multi-GB bge-m3 model); instead the throwaway harness
drives the EXACT SQL the embedder/helpers run — the in-statement stamp, the stale-set predicate, and the
scoped UPDATE — so the substrate logic is proven without the model. A thin unit test additionally proves the
pure _pit_status projection directly (no DB).

Run:  .venv/bin/python -m pytest tests/test_temporal_substrate.py -q
With no throwaway DB this SKIPS cleanly — that is the expected default outcome.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from uuid import uuid4

import pytest

import pythia.db as pdb
from pythia.config import PythiaSettings
from pythia.ingest import provision_versioning as pvz
from tests.test_amend_proof import _FORBIDDEN_DBS, _connect_throwaway, _throwaway_dsn


class _redirect_pool_to_throwaway:
    """Async context manager — point pythia.db's get_pool() at the THROWAWAY DB so a helper that uses the
    shared pool (recompute_provision_temporal_status) runs there, NEVER on live. Belt-and-suspenders: it
    overrides _dsn to the throwaway DSN, drops any existing (possibly-live) pool, and on enter asserts the
    pool's current_database() is non-forbidden — if it ever resolves to pythia/regsuper it raises before any
    write. Restores the original _dsn + drops the throwaway pool on exit."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._orig = None

    async def __aenter__(self):
        self._orig = pdb._dsn
        await pdb.close_pool()  # drop any live pool built earlier
        pdb._dsn = lambda: self._dsn  # type: ignore[assignment]
        pool = await pdb.get_pool()
        async with pool.acquire() as conn:
            dbname = await conn.fetchval("SELECT current_database()")
        if dbname in _FORBIDDEN_DBS:
            await pdb.close_pool()
            pdb._dsn = self._orig  # type: ignore[assignment]
            raise AssertionError(f"pool redirect resolved to live DB {dbname!r} — refusing to run")
        return pool

    async def __aexit__(self, *exc):
        await pdb.close_pool()
        pdb._dsn = self._orig  # type: ignore[assignment]


# The minimal schema this harness needs: a `provision` table carrying BOTH the TMP-01 staleness columns
# (text_hash + embedded_text_hash + embedding_bge) and the TMP-02 facet column (pit_status), plus the
# provision_version lineage (so recompute_provision_temporal_status can project the head window). It mirrors
# the production additive shape but is a throwaway scratch schema (dropped+recreated per test).
_SETUP_SQL = """
DROP TABLE IF EXISTS provision_version;
DROP TABLE IF EXISTS provision;
DROP TABLE IF EXISTS source_document;
CREATE TABLE source_document (
    id              uuid PRIMARY KEY,
    external_id     text,
    publisher       text,
    temporal_status text
);
CREATE TABLE provision (
    id                 uuid PRIMARY KEY,
    source_document_id uuid NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
    label              text,
    text               text,
    text_hash          text,
    text_format        text,
    is_anchor_only     boolean NOT NULL DEFAULT false,
    valid_from         date,
    valid_to           date,
    embedding_bge      text,   -- the live column is vector(1024); text suffices to prove the stamp/scoping
    embedded_text_hash text,   -- TMP-01 staleness invariant column
    pit_status         text    -- TMP-02 per-provision facet
);
CREATE TABLE provision_version (
    id                 uuid PRIMARY KEY,
    source_document_id uuid NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
    provision_label    text NOT NULL,
    version_no         int  NOT NULL,
    valid_from         date,
    valid_to           date,
    text               text NOT NULL,
    text_hash          text,
    text_format        text,
    is_head            boolean NOT NULL DEFAULT true,
    detected_at        timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_pv ON provision_version (source_document_id, provision_label, version_no);
CREATE UNIQUE INDEX uq_pv_head
    ON provision_version (source_document_id, provision_label) WHERE is_head;
-- the additive TMP indexes (idempotent in production; created here to prove they SERVE the queries)
CREATE INDEX ix_provision_stale_vector ON provision (id)
    WHERE embedding_bge IS NOT NULL AND embedded_text_hash IS DISTINCT FROM text_hash;
CREATE INDEX ix_provision_in_force ON provision (id) WHERE pit_status = 'IN_FORCE';
"""


def _h(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


async def _stamp_embed(conn, prov_id, text):
    """Drive the EXACT in-statement stamp embed_all runs: write the (stand-in) vector AND embedded_text_hash
    = text_hash atomically. (The real embedder encodes `text`; here the vector value is irrelevant — only the
    hash stamping + scoping are under test.)"""
    await conn.execute(
        "UPDATE provision SET embedding_bge = $2, embedded_text_hash = text_hash WHERE id = $1",
        prov_id,
        f"vec:{_h(text)[:8]}",
    )


async def _stale_ids(conn, ids=None):
    """The stale-set predicate the production stale_vector_ids() uses: has a vector, but for the OLD text."""
    where = "embedding_bge IS NOT NULL AND NOT is_anchor_only AND embedded_text_hash IS DISTINCT FROM text_hash"
    args = []
    if ids is not None:
        where += " AND id = ANY($1::uuid[])"
        args = [ids]
    rows = await conn.fetch(f"SELECT id FROM provision WHERE {where}", *args)
    return [r["id"] for r in rows]


def _settings(enabled: bool) -> PythiaSettings:
    """A throwaway PythiaSettings with temporal_substrate_enabled forced to `enabled` (mirrors the
    monkeypatch pattern in test_fusion_parity / test_discovery — patch the module-bound get_settings)."""
    s = PythiaSettings()
    s.temporal_substrate_enabled = enabled
    return s


# ───────────────────────────────────────────────────────────────────────── TMP-02 pure projection (no DB)


def test_pit_status_projection_is_correct_and_pure():
    """_pit_status is the pure projection — no DB, deterministic. Prove all three positions + edges."""
    today = date(2026, 6, 26)
    past, future = date(2020, 1, 1), date(2030, 1, 1)
    # NOT_YET_IN_FORCE: head opens in the future
    assert pvz._pit_status(future, None, False, today) == pvz._PIT_NOT_YET
    # IN_FORCE: open head, effect date reached
    assert pvz._pit_status(past, None, False, today) == pvz._PIT_IN_FORCE
    assert pvz._pit_status(None, None, False, today) == pvz._PIT_IN_FORCE  # unknown lower bound, open head
    # SUPERSEDED: a closed head (a later version took over) — regardless of dates
    assert pvz._pit_status(past, today, True, today) == pvz._PIT_SUPERSEDED
    # SUPERSEDED: open head but valid_to has passed (end date reached)
    assert pvz._pit_status(past, past, False, today) == pvz._PIT_SUPERSEDED
    # boundary: valid_from == today is IN_FORCE (>, not >=); valid_to == today is SUPERSEDED (<=)
    assert pvz._pit_status(today, None, False, today) == pvz._PIT_IN_FORCE
    assert pvz._pit_status(past, today, False, today) == pvz._PIT_SUPERSEDED


# ───────────────────────────────────────────────────────────────────────── the throwaway-DB proofs


async def test_tmp01_staleness_invariant_roundtrip_on_throwaway_db():
    """TMP-01: amend → stale flagged → SCOPED re-embed → reconciled; a non-amended provision is never touched;
    the re-embed is SCOPED (not corpus-wide)."""
    conn = await _connect_throwaway()
    if conn is None:
        pytest.skip("no throwaway DB — TMP-01 staleness proof skipped by design (never runs on live)")
    try:
        dbname = await conn.fetchval("SELECT current_database()")
        assert dbname not in _FORBIDDEN_DBS, "temporal-substrate harness must run on a throwaway DB"
        await conn.execute(_SETUP_SQL)

        doc_id = uuid4()
        await conn.execute(
            "INSERT INTO source_document (id, external_id, publisher, temporal_status) VALUES ($1,$2,$3,$4)",
            doc_id,
            "TEST-TMP/0001",
            "Sbírka zákonů",
            "in-force",
        )
        # two provisions: §1 will be amended, §2 will NOT (the scoping witness)
        p1, p2 = uuid4(), uuid4()
        t1_old, t2 = "obligation v1", "definitions stay put"
        await conn.executemany(
            "INSERT INTO provision (id, source_document_id, label, text, text_hash, is_anchor_only) "
            "VALUES ($1,$2,$3,$4,$5,false)",
            [(p1, doc_id, "§ 1", t1_old, _h(t1_old)), (p2, doc_id, "§ 2", t2, _h(t2))],
        )

        # 1) initial embed of BOTH (the in-statement stamp) → invariant holds everywhere
        await _stamp_embed(conn, p1, t1_old)
        await _stamp_embed(conn, p2, t2)
        assert await _stale_ids(conn) == [], "freshly embedded → nothing stale (embedded_text_hash==text_hash)"

        # 2) IN-PLACE text update of §1 (the hypothetical version-aware future surface — NOT today's amend(),
        #    which delete+reinserts): new text_hash; the vector + embedded_text_hash are UNTOUCHED. The
        #    invariant must now read §1 as STALE, §2 as current.
        t1_new = "obligation v2 AMENDED"
        await conn.execute("UPDATE provision SET text=$2, text_hash=$3 WHERE id=$1", p1, t1_new, _h(t1_new))
        stale = await _stale_ids(conn)
        assert stale == [p1], f"only the amended §1 is stale, got {stale}"
        # the silent split is real: the stored vector still encodes the OLD text's hash, text now NEW
        row = await conn.fetchrow("SELECT embedded_text_hash, text_hash FROM provision WHERE id=$1", p1)
        assert row["embedded_text_hash"] == _h(t1_old) and row["text_hash"] == _h(t1_new)

        # 3) the SCOPED re-embed: re-embed ONLY the stale set (mirrors reembed_stale → embed_all(ids=stale)).
        #    Prove it is scoped: §2's vector value must be byte-identical before/after.
        p2_vec_before = await conn.fetchval("SELECT embedding_bge FROM provision WHERE id=$1", p2)
        for sid in stale:  # the production helper batches; one row here
            txt = await conn.fetchval("SELECT text FROM provision WHERE id=$1", sid)
            await _stamp_embed(conn, sid, txt)
        assert await _stale_ids(conn) == [], "after scoped re-embed nothing is stale (reconciled)"
        row = await conn.fetchrow("SELECT embedded_text_hash, text_hash FROM provision WHERE id=$1", p1)
        assert row["embedded_text_hash"] == row["text_hash"] == _h(t1_new), "§1 vector now matches NEW text"
        p2_vec_after = await conn.fetchval("SELECT embedding_bge FROM provision WHERE id=$1", p2)
        assert p2_vec_after == p2_vec_before, "the non-amended §2 was NEVER re-embedded (scoped, not corpus-wide)"

        # 4) the partial stale-vector INDEX exists and matches the predicate (its DEFINITION is the predicate,
        #    so it is index-native; on a 2-row table the planner uses a seq scan, hence we assert the index
        #    DEFINITION rather than a plan). The index was created by _SETUP_SQL; confirm it is the right one.
        idxdef = await conn.fetchval("SELECT indexdef FROM pg_indexes WHERE indexname='ix_provision_stale_vector'")
        assert (
            idxdef
            and "embedded_text_hash IS DISTINCT FROM text_hash" in idxdef
            and "embedding_bge IS NOT NULL" in idxdef
        ), (
            f"the stale-vector partial index must match the helper predicate exactly "
            f"(embedding_bge IS NOT NULL AND embedded_text_hash IS DISTINCT FROM text_hash), got {idxdef!r}"
        )
    finally:
        await conn.execute("DROP TABLE IF EXISTS provision_version")
        await conn.execute("DROP TABLE IF EXISTS provision")
        await conn.execute("DROP TABLE IF EXISTS source_document")
        await conn.close()


async def test_tmp02_pit_status_derivation_and_index_on_throwaway_db():
    """TMP-02: pit_status is DERIVED correctly (IN_FORCE/SUPERSEDED/NOT_YET_IN_FORCE) from the head version
    window; recompute is idempotent + NULL-nothing; the partial index serves current-only; the doc-level
    source_document.temporal_status is UNAFFECTED (the two facets never collide).

    Drives the REAL recompute_provision_temporal_status() via the shared pool — redirected to the throwaway DB
    by _redirect_pool_to_throwaway (hard-guarded) so the production code path itself is exercised, never live."""
    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TMP-02 derivation proof skipped by design (never runs on live)")
    await probe.close()
    dsn = _throwaway_dsn()
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        dbname = await conn.fetchval("SELECT current_database()")
        assert dbname not in _FORBIDDEN_DBS
        await conn.execute(_SETUP_SQL)

        today = date.today()
        past = today - timedelta(days=365)
        future = today + timedelta(days=365)

        # ONE doc that is itself IN-FORCE at the doc level, holding three provisions in three positions:
        #   §A — current head, open window, effect in the past         → IN_FORCE
        #   §B — head CLOSED (superseded by a later version)           → SUPERSEDED
        #   §C — current head, but effect date in the future           → NOT_YET_IN_FORCE
        doc_id = uuid4()
        doc_status = "in-force"
        await conn.execute(
            "INSERT INTO source_document (id, external_id, publisher, temporal_status) VALUES ($1,$2,$3,$4)",
            doc_id,
            "TEST-TMP/0002",
            "Sbírka zákonů",
            doc_status,
        )

        async def _prov(label, text, vf, vt):
            pid = uuid4()
            await conn.execute(
                "INSERT INTO provision (id, source_document_id, label, text, text_hash, valid_from, valid_to) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7)",
                pid,
                doc_id,
                label,
                text,
                _h(text),
                vf,
                vt,
            )
            return pid

        async def _ver(label, vno, vf, vt, is_head):
            await conn.execute(
                """INSERT INTO provision_version (id, source_document_id, provision_label, version_no,
                       valid_from, valid_to, text, text_hash, is_head)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                uuid4(),
                doc_id,
                label,
                vno,
                vf,
                vt,
                f"{label} text",
                _h(f"{label} text"),
                is_head,
            )

        await _prov("§ A", "in force now", past, None)
        await _ver("§ A", 1, past, None, True)

        # §B: a closed v1 head (superseded) + a current open v2 head under a DIFFERENT label-instance is the
        # real lineage; here we model the closed head directly (its provision row points at the old text).
        await _prov("§ B", "superseded body", past, today)
        await _ver("§ B", 1, past, today, False)  # is_head=false → SUPERSEDED

        await _prov("§ C", "future law", future, None)
        await _ver("§ C", 1, future, None, True)

        # also a structural anchor (no version) → pit_status must stay NULL (never fabricated)
        anchor = uuid4()
        await conn.execute(
            "INSERT INTO provision (id, source_document_id, label, text, text_hash, is_anchor_only) "
            "VALUES ($1,$2,$3,$4,$5,true)",
            anchor,
            doc_id,
            "PART I",
            "",
            _h(""),
        )

        # DRY-RUN first: derive-don't-author preview, writes nothing
        dry = await pvz.recompute_provision_temporal_status(apply=False, as_of=today)
        assert dry["mode"] == "DRY-RUN" and dry["would_change"] >= 3

        res = await pvz.recompute_provision_temporal_status(apply=True, as_of=today)
        assert res["mode"] == "APPLIED" and res["changed"] >= 3

        got = {
            r["label"]: r["pit_status"]
            for r in await conn.fetch("SELECT label, pit_status FROM provision WHERE source_document_id=$1", doc_id)
        }
        assert got["§ A"] == pvz._PIT_IN_FORCE
        assert got["§ B"] == pvz._PIT_SUPERSEDED
        assert got["§ C"] == pvz._PIT_NOT_YET
        assert got["PART I"] is None, "a provision with no version lineage stays NULL (never fabricated)"

        # IDEMPOTENT + NULL-NOTHING: a second recompute with the same as_of changes 0 rows and destroys nothing
        again = await pvz.recompute_provision_temporal_status(apply=True, as_of=today)
        assert again["changed"] == 0, "recompute is idempotent (no date crossed → 0 changes)"
        still = {
            r["label"]: r["pit_status"]
            for r in await conn.fetch("SELECT label, pit_status FROM provision WHERE source_document_id=$1", doc_id)
        }
        assert still == got, "recompute NULLed/destroyed nothing"

        # the partial index makes 'current law only' index-native (WHERE pit_status='IN_FORCE')
        in_force = [
            r["label"]
            for r in await conn.fetch(
                "SELECT label FROM provision WHERE pit_status = 'IN_FORCE' AND source_document_id=$1", doc_id
            )
        ]
        assert in_force == ["§ A"], f"current-only filter returns exactly the in-force §, got {in_force}"

        # the DOC-LEVEL source_document.temporal_status is UNAFFECTED (the two facets are separate things)
        assert await conn.fetchval("SELECT temporal_status FROM source_document WHERE id=$1", doc_id) == doc_status, (
            "TMP-02 must not collide with the doc-level temporal_status (d1_check stays green)"
        )

        # the stale-vector flag (TMP-01) is queryable on the SAME table without disturbing pit_status
        await _stamp_embed(
            conn,
            await conn.fetchval("SELECT id FROM provision WHERE label='§ A' AND source_document_id=$1", doc_id),
            "in force now",
        )
        # nothing amended yet → no stale vectors
        assert await _stale_ids(conn) == []

        # teardown the throwaway scratch tables (still inside the hard-guarded redirect → throwaway only)
        await conn.execute("DROP TABLE IF EXISTS provision_version")
        await conn.execute("DROP TABLE IF EXISTS provision")
        await conn.execute("DROP TABLE IF EXISTS source_document")


# ───────────────────────────────────────────────────────────── TMP-02 edge cases (date-crossing / repeal)


async def _seed_one_prov(conn, doc_id, label, text, vf, vt, ver_vf, ver_vt, is_head):
    """Insert one provision + its single head/closed version row (the shape recompute projects)."""
    pid = uuid4()
    await conn.execute(
        "INSERT INTO provision (id, source_document_id, label, text, text_hash, valid_from, valid_to) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7)",
        pid,
        doc_id,
        label,
        text,
        _h(text),
        vf,
        vt,
    )
    await conn.execute(
        """INSERT INTO provision_version (id, source_document_id, provision_label, version_no,
               valid_from, valid_to, text, text_hash, is_head)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
        uuid4(),
        doc_id,
        label,
        1,
        ver_vf,
        ver_vt,
        f"{label} text",
        _h(f"{label} text"),
        is_head,
    )
    return pid


async def test_tmp02_date_crossing_and_repeal_edges_on_throwaway_db():
    """TMP-02 edges: (a) a NOT_YET provision at as_of < valid_from flips to IN_FORCE at a later as_of and the
    recompute reports EXACTLY 1 change; (b) a fully-repealed-no-replacement lineage (head closed, no open head)
    → SUPERSEDED; (c) a future-as_of recompute reclassifies an IN_FORCE today as NOT_YET when as_of precedes
    its valid_from. All on the throwaway DB via the hard-guarded pool redirect."""
    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TMP-02 edge proof skipped by design (never runs on live)")
    await probe.close()
    dsn = _throwaway_dsn()
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        assert await conn.fetchval("SELECT current_database()") not in _FORBIDDEN_DBS
        await conn.execute(_SETUP_SQL)

        today = date.today()
        soon = today + timedelta(days=10)
        past = today - timedelta(days=365)

        doc_id = uuid4()
        await conn.execute(
            "INSERT INTO source_document (id, external_id, publisher, temporal_status) VALUES ($1,$2,$3,$4)",
            doc_id,
            "TEST-TMP/0003",
            "Sbírka zákonů",
            "in-force",
        )

        # §X: head opens at `soon` (the future) → NOT_YET today; flips IN_FORCE once as_of >= soon (edge a)
        await _seed_one_prov(conn, doc_id, "§ X", "future law", soon, None, soon, None, True)
        # §R: fully repealed, no replacement — the only version is CLOSED (is_head=false), no open head (edge b)
        await _seed_one_prov(conn, doc_id, "§ R", "repealed body", past, today, past, today, False)

        # (a) baseline at today: §X is NOT_YET, §R is SUPERSEDED
        r0 = await pvz.recompute_provision_temporal_status(apply=True, as_of=today)
        assert r0["mode"] == "APPLIED"
        got0 = {
            r["label"]: r["pit_status"]
            for r in await conn.fetch("SELECT label, pit_status FROM provision WHERE source_document_id=$1", doc_id)
        }
        assert got0["§ X"] == pvz._PIT_NOT_YET
        assert got0["§ R"] == pvz._PIT_SUPERSEDED, "fully-repealed-no-replacement lineage → SUPERSEDED (edge b)"

        # (a) DATE-CROSSING: recompute at as_of == soon → §X crosses NOT_YET→IN_FORCE, EXACTLY 1 change. §R
        # stays SUPERSEDED (its valid_to=today < soon), so only §X moves.
        r1 = await pvz.recompute_provision_temporal_status(apply=True, as_of=soon)
        assert r1["changed"] == 1, f"exactly one provision crosses NOT_YET→IN_FORCE at the new as_of, got {r1}"
        got1 = {
            r["label"]: r["pit_status"]
            for r in await conn.fetch("SELECT label, pit_status FROM provision WHERE source_document_id=$1", doc_id)
        }
        assert got1["§ X"] == pvz._PIT_IN_FORCE, "the crossed provision is now IN_FORCE"
        assert got1["§ R"] == pvz._PIT_SUPERSEDED

        # (c) FUTURE/PAST as_of recompute is honored: at as_of BEFORE §X's valid_from it reads NOT_YET again,
        # crossing IN_FORCE→NOT_YET — exactly the one §X moves back.
        r2 = await pvz.recompute_provision_temporal_status(apply=True, as_of=past)
        assert r2["changed"] == 1, f"as_of in the past re-crosses exactly §X back to NOT_YET, got {r2}"
        got2 = {
            r["label"]: r["pit_status"]
            for r in await conn.fetch("SELECT label, pit_status FROM provision WHERE source_document_id=$1", doc_id)
        }
        assert got2["§ X"] == pvz._PIT_NOT_YET, "as_of in the past → §X is NOT_YET again (as_of is honored)"

        await conn.execute("DROP TABLE IF EXISTS provision_version")
        await conn.execute("DROP TABLE IF EXISTS provision")
        await conn.execute("DROP TABLE IF EXISTS source_document")


async def test_scheduler_run_temporal_returns_doc_and_provision_when_flag_on(monkeypatch):
    """With the flag ON, scheduler.run_temporal returns BOTH 'doc' and 'provision' keys (the dual recompute),
    with the TMP-02 provision recompute driven FOR REAL against the throwaway pool. The DOC-level recompute is
    stubbed (its source_document schema is out of scope for this minimal throwaway) so the test isolates that
    the ON path reaches recompute_provision_temporal_status and merges both facets."""
    from pythia.ingest import scheduler as sched

    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — scheduler dual-recompute proof skipped by design")
    await probe.close()
    monkeypatch.setattr(sched, "get_settings", lambda: _settings(True), raising=True)

    async def _doc_stub(*, apply):
        return {"mode": "DRY-RUN", "would_change": 0}

    monkeypatch.setattr(sched, "recompute_temporal_status", _doc_stub, raising=True)

    dsn = _throwaway_dsn()
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        assert await conn.fetchval("SELECT current_database()") not in _FORBIDDEN_DBS
        await conn.execute(_SETUP_SQL)
        out = await sched.run_temporal(apply=False)
        assert set(out.keys()) == {"doc", "provision"}, f"flag-ON run_temporal returns both facets, got {out}"
        assert out["provision"]["mode"] == "DRY-RUN", "the real TMP-02 provision recompute ran (DRY-RUN)"
        await conn.execute("DROP TABLE IF EXISTS provision_version")
        await conn.execute("DROP TABLE IF EXISTS provision")
        await conn.execute("DROP TABLE IF EXISTS source_document")


# ───────────────────────────────────────────────────────────── the DEFAULT-OFF GATE (Finding 3) — the crux


def test_temporal_substrate_flag_defaults_off():
    """The substrate flag MUST ship DEFAULT-OFF — live byte-parity is only guaranteed while it is False."""
    assert PythiaSettings().temporal_substrate_enabled is False


async def test_scheduler_run_temporal_off_path_is_doc_only(monkeypatch):
    """GATE — with the flag OFF, scheduler.run_temporal returns the DOC-level recompute DIRECTLY (a flat dict,
    NOT {'doc':...,'provision':...}) and NEVER calls recompute_provision_temporal_status — so a cron run runs
    no _PIT_DDL and never touches provision.pit_status. recompute_provision_temporal_status is patched to RAISE:
    if the OFF path ever reaches it the test fails loudly."""
    from pythia.ingest import scheduler as sched

    async def _boom(*a, **k):
        raise AssertionError("OFF path must NOT call recompute_provision_temporal_status (gate leaked)")

    async def _doc_stub(*, apply):
        return {"mode": "DRY-RUN", "would_change": 0}

    monkeypatch.setattr(sched, "get_settings", lambda: _settings(False), raising=True)
    monkeypatch.setattr(sched, "recompute_provision_temporal_status", _boom, raising=True)
    monkeypatch.setattr(sched, "recompute_temporal_status", _doc_stub, raising=True)
    out = await sched.run_temporal(apply=False)
    assert out == {"mode": "DRY-RUN", "would_change": 0}, (
        f"OFF run_temporal must be the bare doc-level result (pre-TMP-02 shape), got {out}"
    )
    assert "provision" not in out and "doc" not in out, "OFF path must NOT wrap in the dual-facet shape"


async def test_embed_off_path_does_not_touch_new_column_on_throwaway_db(monkeypatch):
    """GATE (the crux) — with the flag OFF, the production embed_all must be byte-identical to pre-TMP-01: its
    SELECT must NOT read text_hash for the stamp and its UPDATE must reference NO embedded_text_hash column. We
    drive the REAL embed_all against a throwaway `provision` that DELIBERATELY LACKS embedded_text_hash; if the
    OFF path named that column, asyncpg would raise UndefinedColumnError and this test would fail. ensure_column
    is stubbed to a no-op (the scratch table already has the right shape; the real hnsw/vector DDL can't run on
    a text stand-in column) — so this isolates the OFF *embed UPDATE/SELECT* statements specifically."""
    import pythia.ingest.embed_bge as eb

    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — embed OFF-gate proof skipped by design (never runs on live)")
    await probe.close()

    monkeypatch.setattr(eb, "get_settings", lambda: _settings(False), raising=True)  # flag OFF

    class _M:  # stub the encoder — no bge-m3 download
        def encode(self, texts, **k):
            return [[0.1] for _ in texts]

    monkeypatch.setattr(eb, "model", lambda: _M(), raising=True)
    monkeypatch.setattr(eb, "write_mps_heartbeat", lambda *a, **k: None, raising=True)
    monkeypatch.setattr(eb, "check_between_batches", lambda *a, **k: None, raising=True)

    async def _ensure_noop():  # the scratch table already has the right shape
        assert eb.get_settings().temporal_substrate_enabled is False  # belt: still OFF here

    monkeypatch.setattr(eb, "ensure_column", _ensure_noop, raising=True)

    dsn = _throwaway_dsn()
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        assert await conn.fetchval("SELECT current_database()") not in _FORBIDDEN_DBS
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        # a non-activated corpus: provision WITHOUT embedded_text_hash; a real vector(1) so the genuine
        # $2::vector cast in the OFF UPDATE runs unchanged (no stub of _vec — the REAL embed path is exercised)
        await conn.execute("DROP TABLE IF EXISTS provision")
        await conn.execute(
            """CREATE TABLE provision (id uuid PRIMARY KEY, source_document_id uuid, label text, text text,
                   text_hash text, is_anchor_only boolean NOT NULL DEFAULT false, embedding_bge vector(1))"""
        )
        pid = uuid4()
        await conn.execute(
            "INSERT INTO provision (id, label, text, text_hash) VALUES ($1,$2,$3,$4)", pid, "§ 1", "body", _h("body")
        )

        # REAL embed_all OFF path — would raise UndefinedColumnError if it named embedded_text_hash anywhere
        n = await eb.embed_all(only_missing=True)
        assert n == 1

        has_col = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='provision' AND column_name='embedded_text_hash')"
        )
        assert has_col is False, "OFF path must NOT create embedded_text_hash on a non-activated corpus"
        assert await conn.fetchval("SELECT embedding_bge IS NOT NULL FROM provision WHERE id=$1", pid), (
            "OFF UPDATE wrote the vector WITHOUT referencing the (absent) embedded_text_hash column"
        )
        await conn.execute("DROP TABLE IF EXISTS provision")


# ═══════════════════════════════════════════════════════════════════════════ TMP-03 + TMP-04 (#63/N3)
# The PIT as-of CORRECTNESS fix (version-in-force text at a past as_of) + the amend is_anchor_only fix, and
# the amend PROVENANCE (source_event + approval link) + nexus_event emission with the no-false-alert fold.
# All on the hard-guarded throwaway DB; all gated behind temporal_substrate_enabled (OFF ⇒ byte-identical).

# A fuller throwaway schema than _SETUP_SQL above: provision_version carries source_event (TMP-04 writes it),
# provision carries is_anchor_only (the amend exclusion), source_document carries external_id (event payload),
# plus the operational discovery_queue + nexus_event tables the TMP-04 provenance/emit paths touch.
# Teardown DROP (CASCADE, creates nothing) — leaves the throwaway DB clean so a LATER test file (e.g.
# test_amend_proof.py, whose own setup drops only provision_version+source_document) is never blocked by a
# leftover `provision` FK from these tests. Used at the END of every TMP-03/04 test.
_TMP0304_DROP = """
DROP TABLE IF EXISTS provision_version CASCADE;
DROP TABLE IF EXISTS provision CASCADE;
DROP TABLE IF EXISTS source_document CASCADE;
DROP TABLE IF EXISTS discovery_queue CASCADE;
DROP TABLE IF EXISTS nexus_event CASCADE;
"""

_TMP0304_SQL = (
    _TMP0304_DROP
    + """
CREATE TABLE source_document (
    id uuid PRIMARY KEY, external_id text, publisher text, temporal_status text);
CREATE TABLE provision (
    id uuid PRIMARY KEY,
    source_document_id uuid NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
    label text, text text, text_hash text, text_format text,
    is_anchor_only boolean NOT NULL DEFAULT false, valid_from date, valid_to date);
CREATE TABLE provision_version (
    id uuid PRIMARY KEY,
    source_document_id uuid NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
    provision_label text NOT NULL, version_no int NOT NULL,
    valid_from date, valid_to date, text text NOT NULL, text_hash text, text_format text,
    is_head boolean NOT NULL DEFAULT true, source_event uuid,
    detected_at timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX uq_pv ON provision_version (source_document_id, provision_label, version_no);
CREATE UNIQUE INDEX uq_pv_head ON provision_version (source_document_id, provision_label) WHERE is_head;
CREATE INDEX ix_pv_pit ON provision_version (source_document_id, provision_label, valid_from);
CREATE TABLE discovery_queue (
    id uuid PRIMARY KEY, external_id text, status text, metadata jsonb NOT NULL DEFAULT '{}'::jsonb);
CREATE TABLE nexus_event (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL, payload jsonb, created_at timestamptz NOT NULL DEFAULT now(),
    consumed_at timestamptz);
"""
)


async def _seed_doc_prov_v1(conn, *, label, old_text, vf, anchor=False):
    """Insert one source_document + its provision row + the v1 head version (the live single-version shape)."""
    doc_id = uuid4()
    await conn.execute(
        "INSERT INTO source_document (id, external_id, publisher, temporal_status) VALUES ($1,$2,$3,$4)",
        doc_id,
        "TMP0304/0001",
        "Sbírka zákonů",
        "in-force",
    )
    pid = uuid4()
    await conn.execute(
        "INSERT INTO provision (id, source_document_id, label, text, text_hash, text_format, is_anchor_only, "
        "valid_from) VALUES ($1,$2,$3,$4,$5,'gfm',$6,$7)",
        pid,
        doc_id,
        label,
        old_text,
        _h(old_text),
        anchor,
        vf,
    )
    await conn.execute(
        """INSERT INTO provision_version (id, source_document_id, provision_label, version_no,
               valid_from, valid_to, text, text_hash, text_format, is_head)
           VALUES ($1,$2,$3,1,$4::date,NULL,$5,$6,'gfm',true)""",
        uuid4(),
        doc_id,
        label,
        vf,
        old_text,
        _h(old_text),
    )
    return doc_id, pid


async def _replace_provision_text(conn, doc_id, label, new_text, *, anchor=False):
    """Mirror the non-destructive re-ingest's 'provisions replaced' step: the provision row now carries the
    NEW text (head text), exactly as amend() reads it before reconciling provision_version. A re-ingest gives
    the provision a FRESH id (the old row is deleted); returns that new id so a test can query the overlay."""
    await conn.execute("DELETE FROM provision WHERE source_document_id=$1", doc_id)
    new_pid = uuid4()
    await conn.execute(
        "INSERT INTO provision (id, source_document_id, label, text, text_hash, text_format, is_anchor_only) "
        "VALUES ($1,$2,$3,$4,$5,'gfm',$6)",
        new_pid,
        doc_id,
        label,
        new_text,
        _h(new_text),
        anchor,
    )
    return new_pid


# ───────────────────────────────────────────────────────────────────── TMP-03: version-aware past as_of


async def test_tmp03_past_asof_serves_in_force_version_text_gated():
    """TMP-03 (INV-7): after a real amend forms v2, a PAST as_of inside the OLD window must serve the OLD
    version's TEXT via the seed overlay when the gate is ON; with the gate OFF the overlay never runs and the
    Seed carries no asof_text (the OFF path reads the HEAD — byte-identical to pre-TMP-03). A current/None
    as_of serves the head either way (the head IS the current version)."""
    from datetime import date as _date

    from pythia.graph import seed as seedmod

    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TMP-03 past-as_of proof skipped by design (never runs on live)")
    await probe.close()
    dsn = _throwaway_dsn()
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        assert await conn.fetchval("SELECT current_database()") not in _FORBIDDEN_DBS
        await conn.execute(_TMP0304_SQL)

        label = "§ 1"
        old_text, new_text = "obligation v1 (the OLD law)", "obligation v2 AMENDED (the NEW law)"
        old_from, new_from = _date(2020, 1, 1), _date(2021, 6, 1)
        past = _date(2020, 6, 1)  # inside the OLD window [2020-01-01, 2021-06-01)

        doc_id, pid = await _seed_doc_prov_v1(conn, label=label, old_text=old_text, vf=old_from)
        # the amend forms v2 (close old head at new_from, append new head with the NEW text)
        pid = await _replace_provision_text(conn, doc_id, label, new_text)
        async with conn.transaction():
            res = await pvz.amend(
                conn,
                source_document_id=doc_id,
                new_effect_date=new_from,
                prior_heads={label: (1, _h(old_text), old_from)},
            )
        assert res["appended"] == 1
        # the live provision row's HEAD text is now the NEW text (what the OFF path would serve)
        assert await conn.fetchval("SELECT text FROM provision WHERE id=$1", pid) == new_text

        # GATE OFF: overlay is a no-op → asof_text stays None (OFF reads the head — byte-identical)
        seedmod_settings_off = _settings(False)
        import pythia.graph.seed as _sm

        orig = _sm.get_settings
        _sm.get_settings = lambda: seedmod_settings_off
        try:
            out_off = await seedmod._overlay_asof_text(conn, [seedmod.Seed(str(pid), 1.0)], past)
            assert out_off[0].asof_text is None, "OFF path must not populate asof_text (head-read, byte-identical)"
        finally:
            _sm.get_settings = orig

        # GATE ON: a PAST as_of inside the old window serves the OLD version's TEXT (not the head)
        _sm.get_settings = lambda: _settings(True)
        try:
            out_past = await seedmod._overlay_asof_text(conn, [seedmod.Seed(str(pid), 1.0)], past)
            assert out_past[0].asof_text == old_text, (
                f"past as_of must serve the in-force OLD version text, got {out_past[0].asof_text!r}"
            )
            # a current as_of (today) serves the head/new text — never a not-yet/old version
            out_now = await seedmod._overlay_asof_text(conn, [seedmod.Seed(str(pid), 1.0)], _date.today())
            assert out_now[0].asof_text is None, "current as_of takes the head path (no overlay) — head IS current"
            # a None as_of also takes the head path
            out_none = await seedmod._overlay_asof_text(conn, [seedmod.Seed(str(pid), 1.0)], None)
            assert out_none[0].asof_text is None
            # INV-7: a past as_of BEFORE the act ever existed has no version → no fabricated text (stays None)
            out_pre = await seedmod._overlay_asof_text(conn, [seedmod.Seed(str(pid), 1.0)], _date(2019, 1, 1))
            assert out_pre[0].asof_text is None, "as_of before v1's valid_from has no in-force version (honest None)"
        finally:
            _sm.get_settings = orig

        await conn.execute(_TMP0304_DROP)  # teardown (throwaway only)


async def test_tmp03_amend_excludes_anchor_only_rows():
    """TMP-03 — amend() must EXCLUDE is_anchor_only structural rows from versioning (mirrors apply's
    anchor_excl). A re-ingest whose provision set is a real § PLUS a structural anchor must version ONLY the §,
    never the anchor (the 31-mis-versioned-anchor-rows bug). The anchor must produce NO provision_version row."""
    from datetime import date as _date

    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TMP-03 anchor-exclusion proof skipped by design")
    await probe.close()
    dsn = _throwaway_dsn()
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        assert await conn.fetchval("SELECT current_database()") not in _FORBIDDEN_DBS
        await conn.execute(_TMP0304_SQL)

        new_from = _date(2021, 6, 1)
        doc_id = uuid4()
        await conn.execute(
            "INSERT INTO source_document (id, external_id, publisher, temporal_status) VALUES ($1,$2,$3,$4)",
            doc_id,
            "TMP0304/0002",
            "Sbírka zákonů",
            "in-force",
        )
        # the re-inserted provision set: one REAL § (no prior head → 'added') + one structural ANCHOR
        await conn.execute(
            "INSERT INTO provision (id, source_document_id, label, text, text_hash, text_format, is_anchor_only) "
            "VALUES ($1,$2,$3,$4,$5,'gfm',false)",
            uuid4(),
            doc_id,
            "§ 1",
            "real body",
            _h("real body"),
        )
        await conn.execute(
            "INSERT INTO provision (id, source_document_id, label, text, text_hash, text_format, is_anchor_only) "
            "VALUES ($1,$2,$3,$4,$5,'gfm',true)",
            uuid4(),
            doc_id,
            "ČÁST PRVNÍ",
            "",
            _h(""),
        )

        async with conn.transaction():
            res = await pvz.amend(conn, source_document_id=doc_id, new_effect_date=new_from, prior_heads={})

        assert res["added"] == 1, f"only the real § is versioned (the anchor is excluded), got {res}"
        labels = [
            r["provision_label"]
            for r in await conn.fetch(
                "SELECT provision_label FROM provision_version WHERE source_document_id=$1", doc_id
            )
        ]
        assert labels == ["§ 1"], f"the anchor must NEVER get a provision_version row, got {labels}"

        await conn.execute(_TMP0304_DROP)


async def test_tmp03_cte_overlay_serves_in_force_version_text_gated():
    """TMP-03 — the four-corners (cte_expand) overlay mirrors the seed overlay: a PAST as_of carries the OLD
    version's TEXT onto a NeighborHit (ON), None on OFF / current as_of. Drives cte_expand._overlay_asof_text
    directly over a reconciled v2 lineage so the NeighborHit wiring is locked (the walk SQL itself is unchanged
    — the in-force mask already gates reachability; this only version-corrects the served text)."""
    from datetime import date as _date

    from pythia.graph import cte_expand as cte

    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TMP-03 cte-overlay proof skipped by design")
    await probe.close()
    dsn = _throwaway_dsn()
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        assert await conn.fetchval("SELECT current_database()") not in _FORBIDDEN_DBS
        await conn.execute(_TMP0304_SQL)

        label = "§ 1"
        old_text, new_text = "cte v1 OLD", "cte v2 NEW AMENDED"
        old_from, new_from = _date(2020, 1, 1), _date(2021, 6, 1)
        past = _date(2020, 6, 1)
        doc_id, pid = await _seed_doc_prov_v1(conn, label=label, old_text=old_text, vf=old_from)
        pid = await _replace_provision_text(conn, doc_id, label, new_text)
        async with conn.transaction():
            await pvz.amend(
                conn,
                source_document_id=doc_id,
                new_effect_date=new_from,
                prior_heads={label: (1, _h(old_text), old_from)},
            )

        hit = cte.NeighborHit(str(pid), 1, 1.0, ["references"])
        orig = cte.get_settings  # cte._overlay_asof_text reads cte.get_settings
        # OFF → no overlay
        cte.get_settings = lambda: _settings(False)
        try:
            out_off = await cte._overlay_asof_text(conn, [cte.NeighborHit(str(pid), 1, 1.0, ["references"])], past)
            assert out_off[0].asof_text is None, "OFF cte path leaves NeighborHit.asof_text None (head-read)"
        finally:
            cte.get_settings = orig
        # ON + past → OLD version text
        cte.get_settings = lambda: _settings(True)
        try:
            out_on = await cte._overlay_asof_text(conn, [hit], past)
            assert out_on[0].asof_text == old_text, (
                f"cte past as_of serves OLD version text, got {out_on[0].asof_text!r}"
            )
            out_now = await cte._overlay_asof_text(conn, [cte.NeighborHit(str(pid), 1, 1.0, [])], _date.today())
            assert out_now[0].asof_text is None, "current as_of takes head path (no overlay)"
        finally:
            cte.get_settings = orig

        await conn.execute(_TMP0304_DROP)


# ───────────────────────────────────────────────────────── TMP-04: provenance + event emission + no-alert


async def test_tmp04_amend_writes_provenance_and_emits_event_gated():
    """TMP-04 — a GATED-ON amend writes provision_version.source_event (the authorizing id) on each NEW version
    row, links the authorizing discovery_queue approval row, and EMITS the 'amended' + 'reindex-required'
    nexus_event rows the monitors consume. (Provenance + emit are GATED — the OFF test below proves silence.)"""
    from datetime import date as _date

    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TMP-04 provenance/emit proof skipped by design")
    await probe.close()
    dsn = _throwaway_dsn()
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        assert await conn.fetchval("SELECT current_database()") not in _FORBIDDEN_DBS
        await conn.execute(_TMP0304_SQL)

        label = "§ 1"
        old_text, new_text = "v1 body", "v2 body AMENDED"
        old_from, new_from = _date(2020, 1, 1), _date(2021, 6, 1)
        doc_id, pid = await _seed_doc_prov_v1(conn, label=label, old_text=old_text, vf=old_from)
        await _replace_provision_text(conn, doc_id, label, new_text)

        # an authorizing approval row (the who/what-authorized) + a synthetic source_event id
        approval_id, source_event = uuid4(), uuid4()
        await conn.execute(
            "INSERT INTO discovery_queue (id, external_id, status, metadata) VALUES ($1,$2,'approved','{}')",
            approval_id,
            "TMP0304/0001",
        )

        import pythia.ingest.provision_versioning as _pvz

        orig = _pvz.get_settings
        _pvz.get_settings = lambda: _settings(True)  # GATE ON
        try:
            async with conn.transaction():
                res = await _pvz.amend(
                    conn,
                    source_document_id=doc_id,
                    new_effect_date=new_from,
                    prior_heads={label: (1, _h(old_text), old_from)},
                    source_event=source_event,
                    approval_id=approval_id,
                )
        finally:
            _pvz.get_settings = orig

        assert res["appended"] == 1 and res.get("emitted") is True
        # (a) source_event stamped on the NEW (v2) head row; the OLD (v1) row keeps its NULL
        se = await conn.fetchval(
            "SELECT source_event FROM provision_version WHERE source_document_id=$1 AND provision_label=$2 "
            "AND version_no=2",
            doc_id,
            label,
        )
        assert se == source_event, "the new version row carries the authorizing source_event"
        se_v1 = await conn.fetchval(
            "SELECT source_event FROM provision_version WHERE source_document_id=$1 AND provision_label=$2 "
            "AND version_no=1",
            doc_id,
            label,
        )
        assert se_v1 is None, "the pre-existing v1 row's source_event is untouched (NULL)"

        # (b) the approval link was written into discovery_queue.metadata (who/what authorized)
        meta = await conn.fetchval("SELECT metadata FROM discovery_queue WHERE id=$1", approval_id)
        import json as _json

        meta = _json.loads(meta) if isinstance(meta, str) else meta
        assert "amend_materialized" in meta, f"the approval row records the amend it authorized, got {meta}"
        assert meta["amend_materialized"]["source_document_id"] == str(doc_id)

        # (c) the events fired — both kinds the monitors consume
        kinds = [r["kind"] for r in await conn.fetch("SELECT kind FROM nexus_event ORDER BY created_at")]
        assert _pvz.AMENDED_EVENT in kinds and _pvz.REINDEX_REQUIRED_EVENT in kinds, (
            f"amend must emit 'amended' + 'reindex-required', got {kinds}"
        )

        # (d) the GNN-stale monitor consumes 'reindex-required' (it is in the consumer's source set)
        from pythia.ingest.graph_changed_consumer import _SOURCE_KINDS

        assert _pvz.REINDEX_REQUIRED_EVENT in _SOURCE_KINDS, (
            "the graph-changed consumer must drain 'reindex-required' (the GNN-stale rail)"
        )
        # and the monitoring stale rail surfaces both kinds
        from pythia.ingest.monitoring import _STALE_KINDS

        assert _pvz.AMENDED_EVENT in _STALE_KINDS and _pvz.REINDEX_REQUIRED_EVENT in _STALE_KINDS

        await conn.execute(_TMP0304_DROP)


async def test_tmp04_no_change_reamend_raises_no_false_alert():
    """TMP-04 — the no-false-alert fold. A re-amend that changes NOTHING (same text_hash → all-noop) must
    produce NO new version row AND emit NO event, so the change-detection/alert chain can never raise a false
    'your obligation changed'. Consistent with impact.NOOP_CHANGE_SUMMARY ('+0 ~0 -0' → no impact)."""
    from datetime import date as _date

    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TMP-04 no-false-alert proof skipped by design")
    await probe.close()
    dsn = _throwaway_dsn()
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        assert await conn.fetchval("SELECT current_database()") not in _FORBIDDEN_DBS
        await conn.execute(_TMP0304_SQL)

        label = "§ 1"
        same_text = "unchanged obligation body"
        old_from, reingest_from = _date(2020, 1, 1), _date(2021, 6, 1)
        doc_id, pid = await _seed_doc_prov_v1(conn, label=label, old_text=same_text, vf=old_from)
        # the re-ingest replaces the provision with the SAME text (text_hash unchanged)
        await _replace_provision_text(conn, doc_id, label, same_text)

        import pythia.ingest.provision_versioning as _pvz

        orig = _pvz.get_settings
        _pvz.get_settings = lambda: _settings(True)  # GATE ON — the path that COULD emit
        try:
            async with conn.transaction():
                res = await _pvz.amend(
                    conn,
                    source_document_id=doc_id,
                    new_effect_date=reingest_from,
                    prior_heads={label: (1, _h(same_text), old_from)},
                )
        finally:
            _pvz.get_settings = orig

        assert res == {
            "appended": 0,
            "added": 0,
            "readded": 0,
            "removed": 0,
            "noop": 1,
            "ambiguous": 0,
            "source_event": None,
        }, f"a no-change re-amend is all-noop, got {res}"
        assert res.get("emitted") is not True, "a no-change re-amend must NOT mark itself emitted"
        # still exactly ONE version row (no duplicate v2), and NO event fired
        assert await conn.fetchval("SELECT count(*) FROM provision_version WHERE source_document_id=$1", doc_id) == 1, (
            "no-change re-amend creates no duplicate version row (the fold)"
        )
        assert await conn.fetchval("SELECT count(*) FROM nexus_event") == 0, (
            "no-change re-amend emits NO event → no false 'obligation changed' alert"
        )

        # the fold is consistent with impact's '+0 ~0 -0' no-op filter (the macro side)
        from pythia.graph.impact import parse_change_summary

        assert parse_change_summary("+0 ~0 -0 (provisions 1→1)") is None, (
            "impact's no-op filter and amend's no-change fold agree: no real change → no alert"
        )

        await conn.execute(_TMP0304_DROP)


async def test_tmp04_ambiguous_amend_missing_effect_date_no_version_no_event():
    """TMP-04 (LOW-3) — the AMBIGUOUS-amend honest-degrade. A REAL text change (text_hash differs) whose
    new_effect_date is MISSING (None) has no clean window boundary, so amend() deliberately does NOT version it:
    no new provision_version row, the old head is left OPEN, and — because _has_real_change excludes 'ambiguous'
    — NO 'amended'/'reindex-required' event fires (no false 'your obligation changed'). The change is counted as
    'ambiguous', NOT 'appended'. (A backdated date degrades the same way; this pins the missing-date leg.)

    This is a KNOWN, DOCUMENTED gap (see the amend() comment): a genuine amendment with a missing ucinnost date
    looks like a no-op to the operator today. The recorded follow-up is a distinct low-severity 'amend-ambiguous'
    diagnostic event (operator-visible, separate from 'amended') — NOT built here, only recorded."""
    from datetime import date as _date

    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TMP-04 ambiguous-amend proof skipped by design")
    await probe.close()
    dsn = _throwaway_dsn()
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        assert await conn.fetchval("SELECT current_database()") not in _FORBIDDEN_DBS
        await conn.execute(_TMP0304_SQL)

        label = "§ 1"
        old_text, new_text = "obligation v1 (the OLD law)", "obligation v2 REALLY CHANGED (but no ucinnost)"
        old_from = _date(2020, 1, 1)
        doc_id, pid = await _seed_doc_prov_v1(conn, label=label, old_text=old_text, vf=old_from)
        # a REAL text change (text_hash differs) — but the amendment arrives with NO effect date (None)
        await _replace_provision_text(conn, doc_id, label, new_text)

        import pythia.ingest.provision_versioning as _pvz

        orig = _pvz.get_settings
        _pvz.get_settings = lambda: _settings(True)  # GATE ON — the path that COULD emit
        try:
            async with conn.transaction():
                res = await _pvz.amend(
                    conn,
                    source_document_id=doc_id,
                    new_effect_date=None,
                    prior_heads={label: (1, _h(old_text), old_from)},
                    source_event=uuid4(),
                    approval_id=None,
                )
        finally:
            _pvz.get_settings = orig

        # a real change with a missing boundary is classified 'ambiguous' — NOT 'appended' (no false version)
        assert res["ambiguous"] == 1 and res["appended"] == 0, (
            f"missing-effect-date real change → ambiguous, NOT versioned, got {res}"
        )
        assert _pvz._has_real_change(res) is False, "ambiguous is NOT a real change (the no-false-alert fold)"
        assert res.get("emitted") is not True, "an ambiguous amend must NOT mark itself emitted"

        # still exactly ONE version row — the old head is left OPEN, no v2 written, no inverted window
        assert await conn.fetchval("SELECT count(*) FROM provision_version WHERE source_document_id=$1", doc_id) == 1, (
            "missing-date ambiguous amend creates no new version row (old head stays open)"
        )
        head = await conn.fetchrow(
            "SELECT version_no, valid_to, is_head FROM provision_version WHERE source_document_id=$1", doc_id
        )
        assert head["version_no"] == 1 and head["valid_to"] is None and head["is_head"] is True, (
            "the v1 head is untouched (open, still head) — never closed with a missing/inverted boundary"
        )

        # and NO event fired → no false 'obligation changed' alert (the deliberate honest-degrade)
        assert await conn.fetchval("SELECT count(*) FROM nexus_event") == 0, (
            "ambiguous amend emits NO 'amended'/'reindex-required' event (missing date is silently degraded, "
            "the recorded follow-up is a distinct 'amend-ambiguous' diagnostic — not built here)"
        )

        await conn.execute(_TMP0304_DROP)


# ───────────────────────────────────────────────────────────────── the DEFAULT-OFF GATE for TMP-03 + TMP-04


async def test_tmp0304_flag_off_amend_is_byte_identical_no_event():
    """GATE (the crux) — with the flag OFF, amend forms the SAME v2 (the version reconciliation is NOT gated —
    point-in-time correctness must always work) BUT writes NO source_event, NO approval link, and emits NO
    event. So committing TMP-03/04 leaves an OFF corpus's amend behaviour byte-identical to pre-TMP-04: the
    only ON-only effects are provenance + emission."""
    from datetime import date as _date

    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TMP-03/04 OFF-gate proof skipped by design")
    await probe.close()
    dsn = _throwaway_dsn()
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        assert await conn.fetchval("SELECT current_database()") not in _FORBIDDEN_DBS
        await conn.execute(_TMP0304_SQL)

        label = "§ 1"
        old_text, new_text = "v1", "v2 AMENDED"
        old_from, new_from = _date(2020, 1, 1), _date(2021, 6, 1)
        doc_id, pid = await _seed_doc_prov_v1(conn, label=label, old_text=old_text, vf=old_from)
        await _replace_provision_text(conn, doc_id, label, new_text)

        approval_id, source_event = uuid4(), uuid4()
        await conn.execute(
            "INSERT INTO discovery_queue (id, external_id, status, metadata) VALUES ($1,$2,'approved','{}')",
            approval_id,
            "TMP0304/0001",
        )

        import pythia.ingest.provision_versioning as _pvz

        orig = _pvz.get_settings
        _pvz.get_settings = lambda: _settings(False)  # GATE OFF
        try:
            async with conn.transaction():
                # even though caller PASSED source_event/approval_id, the OFF gate must ignore them
                res = await _pvz.amend(
                    conn,
                    source_document_id=doc_id,
                    new_effect_date=new_from,
                    prior_heads={label: (1, _h(old_text), old_from)},
                    source_event=source_event,
                    approval_id=approval_id,
                )
        finally:
            _pvz.get_settings = orig

        # the v2 still forms (PIT correctness is unconditional) — but NO provenance keys, NO emitted flag
        assert res == {"appended": 1, "added": 0, "readded": 0, "removed": 0, "noop": 0, "ambiguous": 0}, (
            f"OFF amend is the pre-TMP-04 counts dict (no source_event/approval_id/emitted keys), got {res}"
        )
        # source_event NULL on the new version row (OFF must not stamp it)
        se = await conn.fetchval(
            "SELECT source_event FROM provision_version WHERE source_document_id=$1 AND version_no=2", doc_id
        )
        assert se is None, "OFF path must NOT stamp source_event (byte-identical to pre-TMP-04)"
        # the approval row was NOT linked
        meta = await conn.fetchval("SELECT metadata FROM discovery_queue WHERE id=$1", approval_id)
        import json as _json

        meta = _json.loads(meta) if isinstance(meta, str) else meta
        assert "amend_materialized" not in (meta or {}), "OFF path must NOT write the approval link"
        # NO event fired
        assert await conn.fetchval("SELECT count(*) FROM nexus_event") == 0, "OFF amend emits NO event"

        await conn.execute(_TMP0304_DROP)


async def test_tmp03_seed_off_path_overlay_is_noop_without_db():
    """GATE — the seed overlay's guard is pure: OFF or a non-past as_of returns the seeds UNCHANGED before any
    DB work (the guard short-circuits). conn=None would explode if the overlay tried to query — so this proves
    the OFF / current-as_of retrieval path never even ATTEMPTS the version lookup (no live DB risk on OFF)."""
    from datetime import date as _date

    import pythia.graph.seed as _sm

    seeds = [_sm.Seed("00000000-0000-0000-0000-000000000001", 1.0)]
    orig = _sm.get_settings

    # OFF + past as_of → unchanged, no DB touched (conn=None proves the query is never reached)
    _sm.get_settings = lambda: _settings(False)
    try:
        out = await _sm._overlay_asof_text(None, seeds, _date(2020, 1, 1))
        assert out is seeds and out[0].asof_text is None
    finally:
        _sm.get_settings = orig

    # ON + None/today as_of → still short-circuits before any DB work (the _past_as_of guard)
    _sm.get_settings = lambda: _settings(True)
    try:
        out2 = await _sm._overlay_asof_text(None, seeds, None)
        assert out2 is seeds and out2[0].asof_text is None
        out3 = await _sm._overlay_asof_text(None, seeds, _date.today())
        assert out3 is seeds and out3[0].asof_text is None
    finally:
        _sm.get_settings = orig


# ───────────────────────────────────────────────── TMP-03 LOW-2: end-to-end OFF parity (REAL retrieval fns)
# A model-free throwaway schema carrying everything the two MODEL-FREE public retrieval functions read:
#   • sparse_seed (FTS)        — provision.tsv (czech tsvector) + the masks (is_anchor_only/valid_from/valid_to
#                                /jurisdiction/legal_hierarchy);
#   • four_corners (graph CTE) — pythia_edge(src,dst,edge_type,weight,rel_id) + provision joined for the masks.
# dense_seed/hybrid_seed are NOT covered here: their signature gives no way to pass a precomputed query vector
# (dense_seed encodes the query internally via the multi-GB bge-m3 model), and the DB-SAFETY/perf rule forbids
# loading it — so the model-bound leg is HONESTLY deferred (its OFF parity is proven at the helper level by
# test_tmp03_seed_off_path_overlay_is_noop_without_db + the _hybrid_overlay/dense overlay guards, and by the
# SQL diff: the OFF path simply never calls _overlay_asof_text). We do NOT load the model.
_OFFPARITY_SQL = (
    _TMP0304_DROP
    + """
DROP TABLE IF EXISTS pythia_edge CASCADE;
CREATE TABLE source_document (
    id uuid PRIMARY KEY, external_id text, publisher text, temporal_status text);
CREATE TABLE provision (
    id uuid PRIMARY KEY,
    source_document_id uuid NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
    label text, text text, text_hash text, text_format text,
    is_anchor_only boolean NOT NULL DEFAULT false, valid_from date, valid_to date,
    jurisdiction text, legal_hierarchy text, tsv tsvector);
CREATE TABLE provision_version (
    id uuid PRIMARY KEY,
    source_document_id uuid NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
    provision_label text NOT NULL, version_no int NOT NULL,
    valid_from date, valid_to date, text text NOT NULL, text_hash text, text_format text,
    is_head boolean NOT NULL DEFAULT true, source_event uuid,
    detected_at timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX uq_pv ON provision_version (source_document_id, provision_label, version_no);
CREATE UNIQUE INDEX uq_pv_head ON provision_version (source_document_id, provision_label) WHERE is_head;
CREATE TABLE pythia_edge (
    src uuid, dst uuid, edge_type text, rel_id smallint, weight double precision);
"""
)


async def test_tmp03_low2_off_parity_real_retrieval_byte_identical_over_2_version_lineage():
    """TMP-03 (LOW-2) — END-TO-END OFF parity on the REAL public retrieval functions over a 2-version lineage.
    With temporal_substrate_enabled OFF, run sparse_seed (FTS) and four_corners (graph CTE) for real and assert:
      (a) every returned Seed.asof_text / NeighborHit.asof_text is None (the overlay never fired); and
      (b) the ordered (id, score) result list is BYTE-IDENTICAL to a baseline re-run (OFF is deterministic and
          untouched by TMP-03 — committing TMP-03/04 changes nothing a caller sees on the OFF path).
    dense_seed/hybrid_seed are model-bound (bge-m3) — honestly deferred (see _OFFPARITY_SQL header)."""
    from datetime import date as _date

    from pythia.graph import cte_expand as cte
    from pythia.graph import seed as seedmod

    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TMP-03 LOW-2 OFF-parity proof skipped by design (never runs on live)")
    await probe.close()
    dsn = _throwaway_dsn()
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        assert await conn.fetchval("SELECT current_database()") not in _FORBIDDEN_DBS
        await conn.execute(_OFFPARITY_SQL)
        # sparse_seed hardcodes the 'czech' text-search config (live has a czech_hunspell-backed one; the
        # throwaway scratch DB does not). Create a lightweight 'czech' config = a copy of the built-in 'simple'
        # parser/dict so to_tsvector('czech',…)/to_tsquery('czech',…) are valid here. THROWAWAY-ONLY DDL
        # (hard-guarded above); the exact stemming is irrelevant to the parity assertions (deterministic FTS).
        if not await conn.fetchval("SELECT 1 FROM pg_ts_config WHERE cfgname='czech'"):
            await conn.execute("CREATE TEXT SEARCH CONFIGURATION czech ( COPY = pg_catalog.simple )")

        # one doc, two §§. §1 is AMENDED (forms a real v1→v2 lineage so the overlay WOULD have text to serve at
        # a past as_of were the gate ON); §2 stays single-version. An edge §1→§2 so four_corners returns a hit.
        doc_id = uuid4()
        await conn.execute(
            "INSERT INTO source_document (id, external_id, publisher, temporal_status) VALUES ($1,$2,$3,$4)",
            doc_id,
            "TMP0304/LOW2",
            "Sbírka zákonů",
            "in-force",
        )
        old_from, new_from = _date(2020, 1, 1), _date(2021, 6, 1)
        # the FTS query word ('oznamovaci') appears in both §§ so sparse_seed returns both, ordered by ts_rank
        q = "oznamovaci povinnost fond"
        s1_old = "oznamovaci povinnost fond v1 (puvodni)"
        s1_new = "oznamovaci povinnost fond v2 (zmeneno) oznamovaci oznamovaci"
        s2_txt = "oznamovaci povinnost definice fond"

        p1, p2 = uuid4(), uuid4()
        for pid, label, text, vf in ((p1, "§ 1", s1_old, old_from), (p2, "§ 2", s2_txt, old_from)):
            await conn.execute(
                "INSERT INTO provision (id, source_document_id, label, text, text_hash, text_format, "
                "is_anchor_only, valid_from, jurisdiction, legal_hierarchy, tsv) "
                "VALUES ($1,$2,$3,$4,$5,'gfm',false,$6,'CZ','STATUTE',to_tsvector('czech',$3))",
                pid,
                doc_id,
                label,
                text,
                _h(text),
                vf,
            )
            await conn.execute(
                """INSERT INTO provision_version (id, source_document_id, provision_label, version_no,
                       valid_from, valid_to, text, text_hash, text_format, is_head)
                   VALUES ($1,$2,$3,1,$4::date,NULL,$5,$6,'gfm',true)""",
                uuid4(),
                doc_id,
                label,
                vf,
                text,
                _h(text),
            )
        # the §1 amend: replace its text (head text now NEW), reconcile → v2. p1 gets a fresh id (re-ingest).
        await conn.execute("DELETE FROM provision WHERE id=$1", p1)
        p1 = uuid4()
        await conn.execute(
            "INSERT INTO provision (id, source_document_id, label, text, text_hash, text_format, "
            "is_anchor_only, valid_from, jurisdiction, legal_hierarchy, tsv) "
            "VALUES ($1,$2,$3,$4,$5,'gfm',false,$6,'CZ','STATUTE',to_tsvector('czech',$4))",
            p1,
            doc_id,
            "§ 1",
            s1_new,
            _h(s1_new),
            new_from,
        )
        async with conn.transaction():
            # amend reconciles the WHOLE re-inserted provision set vs prior_heads: §1 changed (appended), §2
            # unchanged (its prior head text_hash matches → noop). §2 MUST be in prior_heads or amend would try
            # to re-add it and collide on uq_pv_head with its existing v1 head.
            res = await pvz.amend(
                conn,
                source_document_id=doc_id,
                new_effect_date=new_from,
                prior_heads={"§ 1": (1, _h(s1_old), old_from), "§ 2": (1, _h(s2_txt), old_from)},
            )
        assert res["appended"] == 1 and res["noop"] == 1, (
            f"the §1 amend forms a real v2 lineage (§2 unchanged → noop), got {res}"
        )
        # an edge §1→§2 (a plain 'references', rel_id 1 — NOT a non-traversable 27/28) so four_corners reaches §2
        await conn.execute(
            "INSERT INTO pythia_edge (src, dst, edge_type, rel_id, weight) VALUES ($1,$2,'references',1,0.9)", p1, p2
        )

        # force the OFF gate explicitly on BOTH retrieval modules (default is OFF, but be robust to ambient env)
        orig_seed, orig_cte = seedmod.get_settings, cte.get_settings
        seedmod.get_settings = lambda: _settings(False)
        cte.get_settings = lambda: _settings(False)
        try:
            # --- model-free leg 1: sparse_seed (FTS). Run TWICE with a CURRENT as_of (the live default surface).
            sp_a = await seedmod.sparse_seed(q, k=8)
            sp_b = await seedmod.sparse_seed(q, k=8)
            assert sp_a, "sparse_seed must return hits (the FTS query matches both §§)"
            assert all(s.asof_text is None for s in sp_a), "OFF sparse_seed must never populate asof_text"
            assert [(s.provision_id, s.score) for s in sp_a] == [(s.provision_id, s.score) for s in sp_b], (
                "OFF sparse_seed is deterministic + byte-identical across runs (TMP-03 changes nothing OFF)"
            )

            # also run sparse_seed with a PAST as_of: OFF, the overlay STILL never fires (the gate, not the date,
            # decides) → asof_text stays None and the result is identical to the current-as_of run's id set.
            sp_past = await seedmod.sparse_seed(q, k=8, as_of=_date(2020, 6, 1))
            assert all(s.asof_text is None for s in sp_past), (
                "OFF sparse_seed at a PAST as_of must STILL leave asof_text None (gate OFF → overlay never runs)"
            )

            # --- model-free leg 2: four_corners (graph CTE) from §1. Run TWICE; assert byte-identity + no overlay.
            fc_a = await cte.four_corners([str(p1)], max_hops=2, k=8, conn=conn)
            fc_b = await cte.four_corners([str(p1)], max_hops=2, k=8, conn=conn)
            assert fc_a, "four_corners must reach §2 via the §1→§2 edge"
            assert all(h.asof_text is None for h in fc_a), "OFF four_corners must never populate NeighborHit.asof_text"
            assert [(h.provision_id, h.score) for h in fc_a] == [(h.provision_id, h.score) for h in fc_b], (
                "OFF four_corners is byte-identical across runs (the overlay is a no-op on the OFF path)"
            )
            # a PAST as_of on four_corners, OFF → overlay still never fires
            fc_past = await cte.four_corners([str(p1)], max_hops=2, k=8, as_of=_date(2020, 6, 1), conn=conn)
            assert all(h.asof_text is None for h in fc_past), (
                "OFF four_corners at a PAST as_of must STILL leave asof_text None (gate OFF → overlay never runs)"
            )
        finally:
            seedmod.get_settings, cte.get_settings = orig_seed, orig_cte

        await conn.execute(_TMP0304_DROP + "DROP TABLE IF EXISTS pythia_edge CASCADE;")
