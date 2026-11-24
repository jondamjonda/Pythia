"""A2 — the amend-path PROOF HARNESS (Temporal-Dimension wave 1, SAFE-NOW).

The version-lineage `amend()` reconciler (pythia.ingest.provision_versioning) has NEVER fired on real
data: today every provision sits at version_no=1, so the close-old-head / append-new-head logic — the
spine of the whole temporal dimension — has no in-corpus proof. This harness proves that, given two
versions of one (source_document_id, provision_label), `amend()` forms a CORRECT v2: it demotes the old
head, appends a head v2, leaves a non-inverted half-open window, and serves the right text at every
point in time.

ABSOLUTE SAFETY — this NEVER touches the live corpus:
  • It runs ONLY against a clearly-throwaway database. It looks for env PYTHIA_AMENDTEST_URL, else a DB
    named 'pythia_amendtest' derived from the configured DSN. If neither is reachable the whole module
    SKIPS (the expected default; nothing is created and no live row is read for write).
  • A HARD GUARD reads `current_database()` off the actual connection and refuses to proceed if it is
    'pythia' or 'regsuper'. The harness can ONLY ever CREATE/DROP/INSERT inside the throwaway DB.
  • The DDL is the module's own `provision_version` schema (incl. uq_pv_head + ix_pv_pit). The throwaway
    tables are dropped+recreated at setup, so the harness is self-contained and idempotent.

Run:  .venv/bin/python -m pytest tests/test_amend_proof.py -q
With no throwaway DB this SKIPS cleanly — that is the expected default outcome.
"""

from __future__ import annotations

import hashlib
import os
from datetime import date
from uuid import uuid4

import asyncpg
import pytest

from pythia.config import get_settings
from pythia.ingest import provision_versioning as pvz

# Names that must NEVER be written to. The guard is belt-and-suspenders: even if a misconfigured DSN
# points the "throwaway" URL at a live DB, the current_database() check below aborts before any DDL.
_FORBIDDEN_DBS = ("pythia", "regsuper")

# Minimal source_document so the provision_version FK (REFERENCES source_document(id)) is satisfiable.
# Only the columns the FK + the harness touch — NOT the production schema (this is a throwaway scratch DB).
_SETUP_SQL = """
DROP TABLE IF EXISTS provision_version;
DROP TABLE IF EXISTS provision;                 -- this harness CREATEs its own `provision` below; drop any
DROP TABLE IF EXISTS source_document CASCADE;    -- leftover (incl. another throwaway-DB test's FK-bearing one)
CREATE TABLE source_document (
    id          uuid PRIMARY KEY,
    external_id text,
    publisher   text
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
    source_event       uuid,
    detected_at        timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_pv ON provision_version (source_document_id, provision_label, version_no);
CREATE INDEX ix_pv_pit ON provision_version (source_document_id, provision_label, valid_from);
CREATE UNIQUE INDEX uq_pv_head
    ON provision_version (source_document_id, provision_label) WHERE is_head;
"""


def _h(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def _throwaway_dsn() -> str | None:
    """The throwaway DSN, or None when no throwaway target is configured.

    Order: explicit PYTHIA_AMENDTEST_URL, else the configured DSN re-pointed at a database literally
    named 'pythia_amendtest'. Never returns a live DSN to *use* — the connect-time guard enforces that —
    but we also refuse here to even hand back a name in the forbidden set."""
    env = os.environ.get("PYTHIA_AMENDTEST_URL")
    if env:
        dsn = env.replace("postgresql+asyncpg://", "postgresql://")
    else:
        base = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
        head, _, tail = base.rpartition("/")
        if not head:
            return None
        # keep any ?query string off the db name, then swap the db name for the throwaway one
        tail_db = tail.split("?", 1)[0]
        suffix = tail[len(tail_db) :]
        if tail_db in _FORBIDDEN_DBS:
            dsn = f"{head}/pythia_amendtest{suffix}"
        else:
            return None  # the configured DSN is already non-standard — require an explicit env opt-in
    return dsn


async def _connect_throwaway() -> asyncpg.Connection | None:
    """Connect to the throwaway DB, or None if it is unconfigured/unreachable. Applies the HARD GUARD:
    a successful connection whose current_database() is forbidden is closed and treated as 'no DB'
    (so we never even hold a writable handle to live)."""
    dsn = _throwaway_dsn()
    if dsn is None:
        return None
    try:
        conn = await asyncpg.connect(dsn)
    except (asyncpg.PostgresError, OSError):
        return None
    dbname = await conn.fetchval("SELECT current_database()")
    if dbname in _FORBIDDEN_DBS:
        await conn.close()
        raise AssertionError(
            f"amend proof harness resolved to live DB {dbname!r} — refusing to run on a non-throwaway DB"
        )
    return conn


async def _pit(conn, doc_id, label, d):
    """Point-in-time text via the half-open [valid_from, valid_to) window (uses ix_pv_pit)."""
    return await conn.fetchval(
        "SELECT text FROM provision_version WHERE source_document_id=$1 AND provision_label=$2 "
        "AND valid_from<=$3 AND (valid_to IS NULL OR valid_to>$3)",
        doc_id,
        label,
        d,
    )


async def _seed_v1(conn, doc_id, label, text, vf) -> None:
    """Seed the v1 head exactly as provision_versioning.apply() does (version_no=1, is_head, open window)."""
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


async def _insert_provision_head_text(conn, doc_id, label, text) -> None:
    """amend() reads the freshly re-inserted text from `provision`. The throwaway DB has no `provision`
    table, so we create the minimal shape amend() SELECTs (label/text/text_hash/text_format) and replace
    its single row — mirroring a non-destructive re-ingest's 'provisions replaced' step."""
    # include valid_from: amend()'s final step pushes the head's effect date onto provision.valid_from
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS provision (
               id uuid PRIMARY KEY, source_document_id uuid NOT NULL,
               label text, text text, text_hash text, text_format text, valid_from date)"""
    )
    await conn.execute("DELETE FROM provision WHERE source_document_id=$1", doc_id)
    await conn.execute(
        "INSERT INTO provision (id, source_document_id, label, text, text_hash, text_format) "
        "VALUES ($1,$2,$3,$4,$5,'gfm')",
        uuid4(),
        doc_id,
        label,
        text,
        _h(text),
    )


async def test_amend_forms_correct_v2_on_a_throwaway_db():
    """Drive the REAL amend() reconciler over two versions of one provision and prove the v2 it forms is
    correct: head flips, windows are non-inverted, point-in-time serves old-then-new."""
    conn = await _connect_throwaway()
    if conn is None:
        pytest.skip(
            "no throwaway DB (set PYTHIA_AMENDTEST_URL or create a 'pythia_amendtest' DB) — "
            "amend proof harness skipped by design; it must NEVER run against live pythia/regsuper"
        )
    try:
        # belt-and-suspenders: assert the guard already held (and re-state the contract for the reader)
        dbname = await conn.fetchval("SELECT current_database()")
        assert dbname not in _FORBIDDEN_DBS, "amend proof harness must run on a throwaway DB"

        await conn.execute(_SETUP_SQL)

        doc_id = uuid4()
        label = "§ 1"
        old_text, new_text = "old text v1", "new text v2 AMENDED"
        old_from = date(2020, 1, 1)
        new_from = date(2021, 6, 1)  # the amendment's účinnost — the close/open boundary

        await conn.execute(
            "INSERT INTO source_document (id, external_id, publisher) VALUES ($1,$2,$3)",
            doc_id,
            "TEST-AMENDPROOF/0001",
            "Sbírka zákonů",
        )

        # 1) seed the v1 head (the only version that exists in the live corpus today)
        await _seed_v1(conn, doc_id, label, old_text, old_from)

        # 2) the re-ingest: capture the prior head, replace the provision row with the NEW text, amend()
        prior_heads = {label: (1, _h(old_text), old_from)}
        await _insert_provision_head_text(conn, doc_id, label, new_text)
        async with conn.transaction():
            res = await pvz.amend(conn, source_document_id=doc_id, new_effect_date=new_from, prior_heads=prior_heads)

        # --- the proof: amend() reported exactly one appended change, nothing else ---
        assert res == {"appended": 1, "added": 0, "readded": 0, "removed": 0, "noop": 0, "ambiguous": 0}, res

        rows = await conn.fetch(
            "SELECT version_no, valid_from, valid_to, is_head, text FROM provision_version "
            "WHERE source_document_id=$1 AND provision_label=$2 ORDER BY version_no",
            doc_id,
            label,
        )

        # (a) a v2 row exists; exactly two versions
        assert [r["version_no"] for r in rows] == [1, 2], "expected versions 1 and 2"
        v1, v2 = rows[0], rows[1]

        # (b) is_head flipped: old demoted, new promoted — and head-uniqueness holds (exactly one head)
        assert v1["is_head"] is False and v2["is_head"] is True
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM provision_version WHERE source_document_id=$1 AND provision_label=$2 AND is_head",
                doc_id,
                label,
            )
            == 1
        )
        assert v2["text"] == new_text and "AMENDED" in v2["text"]

        # (c) windows are non-inverted and non-overlapping: old.valid_to == new.valid_from == boundary,
        #     new.valid_from >= old.valid_from, new head still open, NO inverted [from > to) window anywhere
        assert v1["valid_to"] == new_from, "old head must close at the new effect date"
        assert v2["valid_from"] == new_from and v2["valid_to"] is None
        assert v2["valid_from"] >= v1["valid_from"], "new window must not start before the old one"
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM provision_version WHERE source_document_id=$1 "
                "AND valid_to IS NOT NULL AND valid_to <= valid_from",
                doc_id,
            )
            == 0
        ), "inverted window written"

        # (d) point-in-time (the index ix_pv_pit drives this query): OLD text inside the old window,
        #     NEW text at a current date; the boundary itself is half-open (belongs to the new version)
        assert await _pit(conn, doc_id, label, date(2020, 6, 1)) == old_text
        assert await _pit(conn, doc_id, label, date(2021, 5, 31)) == old_text  # day before boundary
        assert await _pit(conn, doc_id, label, new_from) == new_text  # boundary → new
        assert await _pit(conn, doc_id, label, date.today()) == new_text  # current → new
    finally:
        # clean the throwaway tables (throwaway DB only — guard above guarantees that) and close
        await conn.execute("DROP TABLE IF EXISTS provision")
        await conn.execute("DROP TABLE IF EXISTS provision_version")
        await conn.execute("DROP TABLE IF EXISTS source_document")
        await conn.close()
