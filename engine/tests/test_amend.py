"""D1-A — non-destructive amend: version history ACCUMULATES across re-ingests (DB integration).

Exercises pythia.ingest.provision_versioning.amend under the PRODUCTION model (source_document preserved, only
provisions replaced) on a self-contained synthetic act. Covers the forward path AND the two latent defects the
final QC reproduced: a removed-then-readded label must not resurrect repealed text (the repeal gap must stay
empty), and a backdated účinnost must never write an inverted [valid_from > valid_to) window."""

from __future__ import annotations

import hashlib
from datetime import date
from uuid import uuid4

from pythia.db import get_pool
from pythia.ingest import provision_versioning as pvz

_EXT = "TEST-AMEND/9999"
_PUB = "Sbírka zákonů"


def _h(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


async def _cleanup(conn) -> None:
    for tbl in ("provision_version", "provision"):
        await conn.execute(
            f"DELETE FROM {tbl} WHERE source_document_id IN "
            "(SELECT id FROM source_document WHERE external_id=$1 AND publisher=$2)",
            _EXT,
            _PUB,
        )
    await conn.execute("DELETE FROM source_document WHERE external_id=$1 AND publisher=$2", _EXT, _PUB)


async def _insert_provisions(conn, doc_id, provs) -> None:
    await conn.execute("DELETE FROM provision WHERE source_document_id=$1", doc_id)
    for i, (label, text) in enumerate(provs):
        await conn.execute(
            """INSERT INTO provision (id, source_document_id, hierarchy_path, ordinal, label, eid, text,
                   text_format, ingestion_quality, text_hash, language, legal_hierarchy, jurisdiction, metadata)
               VALUES ($1,$2,$3::ltree,$4,$5,$6,$7,'gfm',1.0,$8,'cs','cz-act','CZ','{}'::jsonb)""",
            uuid4(),
            doc_id,
            f"p{i + 1}",
            i,
            label,
            f"par_{i + 1}",
            text,
            _h(text),
        )


async def _seed_act(conn, provs, vf):
    """A fresh synthetic act with its v1 head versions (exactly as provision_versioning.apply seeds them)."""
    doc_id = await conn.fetchval(
        """INSERT INTO source_document (external_id, title, publisher, jurisdiction, legal_hierarchy,
               document_type, language, raw_storage_uri, raw_format, raw_checksum, fetched_at, last_checked_at,
               valid_from, status, metadata)
           VALUES ($1,'Test act',$2,'CZ','cz-act','act','cs','file:///test','test','x',now(),now(),
                   $3,'ingested','{}'::jsonb) RETURNING id""",
        _EXT,
        _PUB,
        vf,
    )
    await _insert_provisions(conn, doc_id, provs)
    for label, text in provs:
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
    return doc_id


async def _pit(conn, doc_id, label, d):
    """Point-in-time text of one provision label (the half-open [valid_from, valid_to) window)."""
    return await conn.fetchval(
        "SELECT text FROM provision_version WHERE source_document_id=$1 AND provision_label=$2 "
        "AND valid_from<=$3 AND (valid_to IS NULL OR valid_to>$3)",
        doc_id,
        label,
        d,
    )


async def _reingest(conn, doc_id, provs, new_effect):
    """Simulate a non-destructive re-ingest: capture heads, replace provisions, reconcile versions."""
    prior = await pvz.capture_heads(conn, _EXT)
    await _insert_provisions(conn, doc_id, provs)
    await conn.execute("UPDATE source_document SET valid_from=$2 WHERE id=$1", doc_id, new_effect)
    return await pvz.amend(conn, source_document_id=doc_id, new_effect_date=new_effect, prior_heads=prior)


async def test_amend_accumulates_history_and_is_idempotent():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _cleanup(conn)
        try:
            doc_id = await _seed_act(conn, [("§ 1", "alpha one"), ("§ 2", "beta two")], date(2020, 1, 1))
            # §1 changes, §2 unchanged, §3 added — effective 2021-06-01
            async with conn.transaction():
                res = await _reingest(
                    conn,
                    doc_id,
                    [("§ 1", "alpha one AMENDED"), ("§ 2", "beta two"), ("§ 3", "gamma new")],
                    date(2021, 6, 1),
                )
            assert res == {"appended": 1, "added": 1, "readded": 0, "removed": 0, "noop": 1, "ambiguous": 0}

            rows = await conn.fetch(
                "SELECT version_no, valid_from, valid_to, is_head, text FROM provision_version "
                "WHERE source_document_id=$1 AND provision_label='§ 1' ORDER BY version_no",
                doc_id,
            )
            assert len(rows) == 2
            assert rows[0]["valid_to"] == date(2021, 6, 1) and rows[0]["is_head"] is False
            assert rows[1]["valid_from"] == date(2021, 6, 1) and rows[1]["is_head"] is True
            assert "AMENDED" in rows[1]["text"]

            # half-open point-in-time: old before the boundary, new on/after it
            assert await _pit(conn, doc_id, "§ 1", date(2021, 5, 31)) == "alpha one"
            assert await _pit(conn, doc_id, "§ 1", date(2021, 6, 1)) == "alpha one AMENDED"

            # idempotency: a no-change re-ingest is all no-ops — no spurious v3
            async with conn.transaction():
                res2 = await _reingest(
                    conn,
                    doc_id,
                    [("§ 1", "alpha one AMENDED"), ("§ 2", "beta two"), ("§ 3", "gamma new")],
                    date(2021, 6, 1),
                )
            assert res2 == {"appended": 0, "added": 0, "readded": 0, "removed": 0, "noop": 3, "ambiguous": 0}
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM provision_version WHERE source_document_id=$1 AND provision_label='§ 1'",
                    doc_id,
                )
                == 2
            )
        finally:
            await _cleanup(conn)


async def test_amend_remove_then_readd_keeps_the_repeal_gap_empty():
    """The QC's resurrection scenario: §7 repealed, then later reinstated — the repealed window must stay empty
    and the readd must be a new version, not a 'change' that overwrites the repeal boundary."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _cleanup(conn)
        try:
            doc_id = await _seed_act(conn, [("§ 7", "g1 original")], date(2020, 1, 1))
            # remove §7 effective 2021-01-01
            async with conn.transaction():
                r1 = await _reingest(conn, doc_id, [], date(2021, 1, 1))
            assert r1["removed"] == 1
            assert await _pit(conn, doc_id, "§ 7", date(2021, 6, 1)) is None  # repealed → empty
            # a no-change re-run must NOT re-count the removal (idempotent)
            async with conn.transaction():
                r1b = await _reingest(conn, doc_id, [], date(2021, 1, 1))
            assert r1b["removed"] == 0
            # re-add §7 with new text effective 2022-01-01
            async with conn.transaction():
                r2 = await _reingest(conn, doc_id, [("§ 7", "g2 reinstated")], date(2022, 1, 1))
            assert r2["readded"] == 1 and r2["appended"] == 0  # a fresh version, not a 'change'
            # the repeal gap [2021-01-01, 2022-01-01) stays EMPTY — old text is not resurrected
            assert await _pit(conn, doc_id, "§ 7", date(2021, 6, 1)) is None
            assert await _pit(conn, doc_id, "§ 7", date(2020, 6, 1)) == "g1 original"  # before repeal
            assert await _pit(conn, doc_id, "§ 7", date(2022, 6, 1)) == "g2 reinstated"  # after readd
            # no inverted/overlapping windows
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM provision_version WHERE source_document_id=$1 "
                    "AND valid_to IS NOT NULL AND valid_to <= valid_from",
                    doc_id,
                )
                == 0
            )
        finally:
            await _cleanup(conn)


async def test_amend_backdated_effect_date_is_ambiguous_not_inverted():
    """A retroactively-effective republication (new účinnost before the prior version's start) must NOT write a
    negative-width window that vanishes from point-in-time; it is recorded as ambiguous (old head left open)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _cleanup(conn)
        try:
            doc_id = await _seed_act(conn, [("§ 1", "v1 text")], date(2020, 1, 1))
            async with conn.transaction():
                res = await _reingest(conn, doc_id, [("§ 1", "v1 text CHANGED")], date(2019, 6, 1))  # backdated
            assert res["ambiguous"] == 1 and res["appended"] == 0
            # the old head stays open and queryable; no inverted window exists
            rows = await conn.fetch(
                "SELECT version_no, valid_from, valid_to, is_head FROM provision_version "
                "WHERE source_document_id=$1 AND provision_label='§ 1'",
                doc_id,
            )
            assert len(rows) == 1 and rows[0]["is_head"] is True and rows[0]["valid_to"] is None
            assert await _pit(conn, doc_id, "§ 1", date(2020, 6, 1)) == "v1 text"
        finally:
            await _cleanup(conn)
