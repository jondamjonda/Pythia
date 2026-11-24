"""Provision version history and the provenance bundle: schema, backfill and forward population.

Creates the ``provision_version`` table (head plus history, keyed by ``(source_document_id,
provision_label)``) and the validated ``provenance`` JSONB bundle on ``source_document``. Also holds
the effect-date backfill harness, the non-destructive amendment path and the stored temporal-status
recomputes. Like ``classify_taxonomy`` and ``temporal_complete``, run it after any ingest.

Run (from engine/): .venv/bin/python -m pythia.ingest.provision_versioning [--apply] [--down]
(dry run by default)
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import date
from uuid import uuid4

from pythia.config import get_settings
from pythia.db import close_pool, get_pool
from pythia.graph import provenance as prov
from pythia.graph.taxonomy import _temporal_status

# Point-in-time read of one provision at date D:
#   SELECT text FROM provision_version WHERE source_document_id=$1 AND provision_label=$2
#     AND valid_from<=D AND (valid_to IS NULL OR valid_to>D)
# Amendments append new versions through amend() instead of overwriting.
_DDL = """
ALTER TABLE source_document ADD COLUMN IF NOT EXISTS provenance jsonb;
CREATE TABLE IF NOT EXISTS provision_version (
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
CREATE UNIQUE INDEX IF NOT EXISTS uq_pv ON provision_version (source_document_id, provision_label, version_no);
CREATE INDEX IF NOT EXISTS ix_pv_pit  ON provision_version (source_document_id, provision_label, valid_from);
-- exactly ONE head per (document, label) — DB-enforced so the amend path can never create two heads.
CREATE UNIQUE INDEX IF NOT EXISTS uq_pv_head ON provision_version (source_document_id, provision_label) WHERE is_head;
"""
# --down is a schema rollback only: it drops provision_version and the provenance column. Data corrections
# made to pre-existing columns (source_document.is_authoritative, provision.valid_from) intentionally persist.
_DOWN = """
DROP TABLE IF EXISTS provision_version;
ALTER TABLE source_document DROP COLUMN IF EXISTS provenance;
"""

# Per-provision point-in-time status (provision.pit_status), derived from the head version window.
#
# Two levels, named differently so they cannot be confused:
#   - source_document.temporal_status (document level: in-force, repealed, ANNULLED_BY_CC, ...) answers
#     "is this act in force?". Set by classify_taxonomy, checked by d1_check and n5_check, untouched here.
#   - provision.pit_status (provision level: IN_FORCE, SUPERSEDED, NOT_YET_IN_FORCE) answers "is this
#     provision, at its current head version, in force today?". A provision can be SUPERSEDED (its head
#     closed by an amendment) inside an act that is still in force.
#
# pit_status is a pure projection of the head provision_version window against today, subordinate to the
# version lineage and trust resolution and never an authority of its own. Recomputing reads only stored
# dates (network-free, idempotent), and the amend path never clears it (staleness is tracked via the hash).
#
# Not consumed yet: no reader filters on pit_status. The live current-law filter is the valid_from/valid_to
# date predicate in NexusPort.candidates(). The column and the ix_provision_in_force partial index are
# prepared so a future, separately gated current-only retrieval filter can be index-native; likewise the
# ProvisionRow pit_status / stale_vector fields (ports.py) have no consumer yet. Off by default
# (temporal_substrate_enabled): the column is only created when recompute_provision_temporal_status is
# called explicitly (operator activation, the scheduler with the flag on, or tests), so a corpus that never
# opts in never gains it.
_PIT_IN_FORCE = "IN_FORCE"
_PIT_SUPERSEDED = "SUPERSEDED"
_PIT_NOT_YET = "NOT_YET_IN_FORCE"

_PIT_DDL = """
ALTER TABLE provision ADD COLUMN IF NOT EXISTS pit_status text;
-- SUBSTRATE for a FUTURE separately-gated current_only=pit_status filter — serves NO query today (the live
-- current-law filter is the valid_from/valid_to date predicate in NexusPort.candidates()). Prepared, not wired.
CREATE INDEX IF NOT EXISTS ix_provision_in_force ON provision (id) WHERE pit_status = 'IN_FORCE';
"""


def _md(raw) -> dict:
    return dict(json.loads(raw) if isinstance(raw, str) else (raw or {}))


async def apply(*, apply: bool = False) -> dict:
    """Version every document and provision not yet versioned; a dry run unless `apply`.

    The live ``provision`` row remains the head, so no rows migrate. Only unversioned rows are touched,
    so the same pass backfills the existing corpus and populates new ingests. An applied run:

      - writes ``source_document.provenance``: the validated bundle (institution, source, the legally
        distinct dates mapped per source, family-aware ``is_authoritative``, baseline), and corrects the
        ``source_document.is_authoritative`` column to match it;
      - seeds one head ``provision_version`` row (version_no 1) per provision with the version's
        in-force window, and backfills ``provision.valid_from`` from it.

    A dry run returns the pending document and provision counts.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        have_col = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns WHERE table_name='source_document' AND column_name='provenance'"
        )
        # Structural section anchors (is_anchor_only) carry no versionable text, so they get no
        # provision_version rows and must not count as pending re-seeds (otherwise d1_check's no-pending
        # re-seed invariant would flag every anchor forever). The predicate is guarded on the column
        # existing, so this is correct both before and after the anchor column is added.
        anchor_excl = (
            " AND p.is_anchor_only = false"
            if await conn.fetchval(
                "SELECT 1 FROM information_schema.columns WHERE table_name='provision' AND column_name='is_anchor_only'"
            )
            else ""
        )
        if not apply:
            pending_docs = (
                await conn.fetchval("SELECT count(*) FROM source_document WHERE provenance IS NULL")
                if have_col
                else await conn.fetchval("SELECT count(*) FROM source_document")
            )
            pending_prov = (
                await conn.fetchval(
                    f"""SELECT count(*) FROM provision p WHERE NOT EXISTS (SELECT 1 FROM provision_version pv
                       WHERE pv.source_document_id=p.source_document_id AND pv.provision_label=p.label)
                    {anchor_excl}"""
                )
                if await conn.fetchval("SELECT to_regclass('provision_version')")
                else await conn.fetchval(f"SELECT count(*) FROM provision p WHERE true{anchor_excl}")
            )
            return {"mode": "DRY-RUN (no write)", "docs": pending_docs, "provisions": pending_prov}

        async with conn.transaction():
            await conn.execute(_DDL)
            # 1) Provenance bundle per document; correct the is_authoritative column to match.
            doc_rows = await conn.fetch(
                """SELECT id, external_id, title, publisher, language, issuing_authority, bindingness,
                          valid_from, valid_to, temporal_status, raw_checksum, raw_storage_uri, fetched_at,
                          metadata
                     FROM source_document WHERE provenance IS NULL"""
            )
            eff_from: dict = {}
            for d in doc_rows:
                b = prov.build(
                    external_id=d["external_id"],
                    title=d["title"],
                    publisher=d["publisher"],
                    language=d["language"],
                    issuing_authority=d["issuing_authority"],
                    bindingness=d["bindingness"],
                    valid_from=d["valid_from"],
                    valid_to=d["valid_to"],
                    temporal_status=d["temporal_status"],
                    raw_checksum=d["raw_checksum"],
                    raw_storage_uri=d["raw_storage_uri"],
                    fetched_at=d["fetched_at"],
                    metadata=_md(d["metadata"]),
                )
                if not prov.is_valid(b):
                    raise ValueError(f"provenance bundle FAILED conformance for {d['external_id']}: {b}")
                eff_from[d["id"]] = b["valid_from"]
                await conn.execute(
                    "UPDATE source_document SET provenance=$2::jsonb, is_authoritative=$3 WHERE id=$1",
                    d["id"],
                    json.dumps(b, ensure_ascii=False),
                    b["is_authoritative"],
                )

            # 2) Seed the head (version_no 1) for each provision not yet versioned and backfill
            #    provision.valid_from. Structural anchors have no versionable text and are never seeded.
            prov_rows = await conn.fetch(
                f"""SELECT p.id, p.source_document_id, p.label, p.text, p.text_hash, p.text_format,
                          sd.valid_to, (sd.provenance->>'valid_from')::date AS eff
                     FROM provision p JOIN source_document sd ON sd.id=p.source_document_id
                    WHERE NOT EXISTS (SELECT 1 FROM provision_version pv
                          WHERE pv.source_document_id=p.source_document_id AND pv.provision_label=p.label)
                    {anchor_excl}"""
            )
            versions, valids = [], []
            for r in prov_rows:
                vf = r["eff"]  # the per-source effect date (účinnost), or None
                versions.append(
                    (
                        uuid4(),
                        r["source_document_id"],
                        r["label"],
                        1,
                        vf,
                        r["valid_to"],
                        r["text"],
                        r["text_hash"],
                        r["text_format"],
                        True,
                    )
                )
                valids.append((r["id"], vf))
            if versions:
                await conn.executemany(
                    """INSERT INTO provision_version (id, source_document_id, provision_label, version_no,
                           valid_from, valid_to, text, text_hash, text_format, is_head)
                       VALUES ($1,$2,$3,$4,$5::date,$6,$7,$8,$9,$10)""",
                    versions,
                )
                await conn.executemany(
                    "UPDATE provision SET valid_from=$2::date WHERE id=$1 AND valid_from IS NULL", valids
                )
        return {"mode": "APPLIED", "docs": len(doc_rows), "provisions": len(versions)}


# ---------------------------------------------------------------------------
# Effect-date backfill harness: the shared, idempotent path used by the per-source date connectors
# (sbirkapp, esbirka, cnb_vestnik, eurlex). It rebuilds the provenance bundle from the document's current
# stored fields (so nothing else drifts), keeps the column, bundle, provision and provision_version dates
# in step, recomputes temporal_status from the newly known date, and only fills dates that are currently
# NULL, so re-running is a no-op and nothing is fabricated. Date parameters are datetime.date objects.
# ---------------------------------------------------------------------------

_UNSET = object()

_DOC_FIELDS = """SELECT id, external_id, title, publisher, language, issuing_authority, bindingness,
                        valid_from, valid_to, temporal_status, raw_checksum, raw_storage_uri, fetched_at,
                        metadata, provenance
                   FROM source_document WHERE id=$1"""


async def rebuild_provenance(
    conn,
    *,
    doc_id,
    valid_from=_UNSET,
    date_application=_UNSET,
    recompute_status: bool = False,
    only_new_key: bool = False,
) -> dict:
    """Rebuild one document's provenance bundle from its current stored fields and write it back.

    Reads the same fields as ``apply``, optionally overrides valid_from, injects date_application or
    recomputes temporal_status, asserts the bundle is valid, then writes provenance, is_authoritative,
    valid_from and temporal_status together.

    Nothing is fabricated: every override is an explicit caller decision, and an omitted override keeps
    the stored value. With `only_new_key` (the guard for the date_application schema migration) it raises
    if the rebuilt bundle differs from the stored one in anything but 'date_application'. Returns the new
    bundle.
    """
    d = await conn.fetchrow(_DOC_FIELDS, doc_id)
    vf = d["valid_from"] if valid_from is _UNSET else valid_from
    md = _md(d["metadata"])
    if date_application is not _UNSET:
        md = {
            **md,
            "date_application": (
                date_application.isoformat() if isinstance(date_application, date) else date_application
            ),
        }
    ts = _temporal_status(vf, d["valid_to"], date.today()) if recompute_status else d["temporal_status"]
    b = prov.build(
        external_id=d["external_id"],
        title=d["title"],
        publisher=d["publisher"],
        language=d["language"],
        issuing_authority=d["issuing_authority"],
        bindingness=d["bindingness"],
        valid_from=vf,
        valid_to=d["valid_to"],
        temporal_status=ts,
        raw_checksum=d["raw_checksum"],
        raw_storage_uri=d["raw_storage_uri"],
        fetched_at=d["fetched_at"],
        metadata=md,
    )
    if not prov.is_valid(b):
        raise ValueError(f"provenance bundle FAILED conformance for {d['external_id']}: {b}")
    if only_new_key:
        old = _md(d["provenance"])
        drift = {k for k in set(old) | set(b) if old.get(k) != b.get(k)} - {"date_application"}
        if drift:
            raise ValueError(f"rebuild drifted beyond date_application for {d['external_id']}: {sorted(drift)}")
    await conn.execute(
        "UPDATE source_document SET provenance=$2::jsonb, is_authoritative=$3, valid_from=$4, "
        "temporal_status=$5 WHERE id=$1",
        doc_id,
        json.dumps(b, ensure_ascii=False),
        b["is_authoritative"],
        vf,
        ts,
    )
    return b


async def _sync_provision_dates(conn, doc_id, vf) -> None:
    """Copy the effect date down to the document's provisions and head provision_version rows.

    Only NULL dates are filled, so this is idempotent and never overwrites a known date; a plain UPDATE
    cannot violate uq_pv_head.
    """
    await conn.execute(
        "UPDATE provision SET valid_from=$2::date WHERE source_document_id=$1 AND valid_from IS NULL", doc_id, vf
    )
    await conn.execute(
        "UPDATE provision_version SET valid_from=$2::date "
        "WHERE source_document_id=$1 AND is_head AND valid_from IS NULL",
        doc_id,
        vf,
    )


async def set_effect_date(conn, doc_id, vf, *, valid_to=_UNSET, recompute_status: bool = True) -> bool:
    """Set a document's effect date (valid_from) on the column, bundle, provisions and head versions.

    temporal_status is recomputed, so a future date correctly becomes NOT_YET_EFFECTIVE. ``vf=None`` is a
    no-op: an unknown date stays NULL, which remains distinguishable from "not in force". Returns whether
    anything was written.

    The optional `valid_to` (end of validity, or None) is written to the column first, because the rebuild
    reads valid_to from the column when recomputing temporal_status. It is then copied to the head
    provision_version rows so point-in-time reads close after the end date.
    """
    if vf is None:
        return False
    if valid_to is not _UNSET:
        await conn.execute("UPDATE source_document SET valid_to=$2 WHERE id=$1", doc_id, valid_to)
    await rebuild_provenance(conn, doc_id=doc_id, valid_from=vf, recompute_status=recompute_status)
    await _sync_provision_dates(conn, doc_id, vf)
    if valid_to is not _UNSET and valid_to is not None:
        # The end date must reach both the head version and provision.valid_to: the retrieval temporal masks
        # (cte_expand, ppr, seed) filter on provision.valid_to, so closing only the head would still serve a
        # repealed act as in force after its end date.
        await conn.execute(
            "UPDATE provision_version SET valid_to=$2::date "
            "WHERE source_document_id=$1 AND is_head AND valid_to IS NULL",
            doc_id,
            valid_to,
        )
        await conn.execute(
            "UPDATE provision SET valid_to=$2::date WHERE source_document_id=$1 AND valid_to IS NULL", doc_id, valid_to
        )
    return True


async def backfill_eurlex_dates(*, apply: bool = False) -> dict:
    """Backfill EU documents' entry-into-force and end-of-validity dates from ``eurlex._EU_DATES``.

    The dates come from the CELLAR RDF notice and were cross-checked against the dates EUR-Lex displays.
    Uses the shared harness; idempotent (fills only NULLs).
    """
    from pythia.ingest.eurlex import _EU_DATES

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, external_id FROM source_document "
            "WHERE provenance->>'source'='eurlex' AND (provenance->>'valid_from') IS NULL"
        )
        plan = []
        for r in rows:
            pair = _EU_DATES.get(r["external_id"])
            vf = date.fromisoformat(pair[0]) if pair else None
            vt = date.fromisoformat(pair[1]) if (pair and pair[1]) else None
            plan.append((r["id"], r["external_id"], vf, vt))
        sample = {ext: (vf.isoformat() if vf else None, vt.isoformat() if vt else None) for _, ext, vf, vt in plan}
        provs_to_fix = (
            await conn.fetchval(
                "SELECT count(*) FROM provision p JOIN source_document sd ON sd.id=p.source_document_id "
                "WHERE sd.provenance->>'source'='eurlex' AND p.valid_from IS NULL "
                "AND sd.id = ANY($1::uuid[])",
                [pid for pid, _e, vf, _vt in plan if vf is not None],
            )
            or 0
        )
        if not apply:
            return {
                "mode": "DRY-RUN",
                "docs_to_fix": sum(1 for *_, vf, _vt in plan if vf),
                "provisions_to_fix": provs_to_fix,
                "sample": sample,
            }
        fixed = 0
        for pid, _ext, vf, vt in plan:
            if vf is None:
                continue
            async with conn.transaction():
                await set_effect_date(conn, pid, vf, valid_to=vt)
            fixed += 1
        return {"mode": "APPLIED", "docs_fixed": fixed, "provisions_to_fix": provs_to_fix, "sample": sample}


async def set_application_date(conn, doc_id, da) -> bool:
    """Record a soft-law application date in provenance.date_application only.

    valid_from is left untouched so soft law is never presented as hard law. ``da=None`` is a no-op.
    """
    if da is None:
        return False
    await rebuild_provenance(conn, doc_id=doc_id, date_application=da)
    return True


# ---------------------------------------------------------------------------
# Non-destructive amendment: accumulate version history across re-ingests instead of overwriting it.
# The amend path (refresh.py) re-ingests without deleting the source_document, so its provision_version rows
# survive the cascade, and calls capture_heads before re-inserting provisions and amend afterwards. amend
# reconciles the head versions against the re-inserted provisions: a changed label closes its old head
# (valid_to = the new effect date, is_head false) and appends a new head (version N+1); an added label seeds
# version 1; a removed label closes its head. The old head is demoted before the new one is inserted, so
# uq_pv_head (one head per label) is never violated. An unchanged re-ingest is a no-op (equal text_hash).
# ---------------------------------------------------------------------------


async def capture_heads(conn, external_id: str, publisher: str = "Sbírka zákonů") -> dict:
    """Snapshot the current head version per provision label, taken before the re-ingest.

    The label is the key that stays stable when provisions are re-inserted. Returns
    ``{label: (version_no, text_hash, valid_from)}``.
    """
    rows = await conn.fetch(
        """SELECT pv.provision_label AS label, pv.version_no, pv.text_hash, pv.valid_from
             FROM provision_version pv JOIN source_document sd ON sd.id = pv.source_document_id
            WHERE sd.external_id = $1 AND sd.publisher = $2 AND pv.is_head""",
        external_id,
        publisher,
    )
    return {r["label"]: (r["version_no"], r["text_hash"], r["valid_from"]) for r in rows}


# nexus_event kinds amend() emits when temporal_substrate_enabled is on. 'amended' feeds the audit and
# change-diff views (what changed and by how much); 'reindex-required' feeds the GNN-stale rail, drained by
# graph_changed_consumer and monitoring._STALE_KINDS exactly like 'graph-changed'.
AMENDED_EVENT = "amended"
REINDEX_REQUIRED_EVENT = "reindex-required"

# Change keys meaning a version really moved (a head was appended, added, re-added or removed). 'noop'
# (unchanged text_hash) and 'ambiguous' (missing or backdated boundary, old head left open) are not real
# changes; excluding them prevents false alerts, consistent with impact.NOOP_CHANGE_SUMMARY ('+0 ~0 -0').
_REAL_CHANGE_KEYS = ("appended", "added", "readded", "removed")


def _has_real_change(c: dict) -> bool:
    """Return True if the amend counts contain a real version change, not just noop or ambiguous rows.

    This mirrors the '+0 ~0 -0' no-op filter in ``impact.parse_change_summary`` (``NOOP_CHANGE_SUMMARY``
    in pythia/graph/impact.py): a re-ingest that changed nothing, or only produced ambiguous rows that
    left every head open, must never emit 'amended' or 'reindex-required', so the change-detection and
    alert chain cannot report a false "your obligation changed".
    """
    return any(c.get(k, 0) for k in _REAL_CHANGE_KEYS)


async def amend(
    conn,
    *,
    source_document_id,
    new_effect_date,
    prior_heads: dict,
    source_event=None,
    approval_id=None,
    emit: bool = True,
) -> dict:
    """Reconcile provision_version with the freshly re-inserted provision rows of an amended act.

    Runs in the caller's transaction after the new provisions are inserted, with the source_document row
    preserved so the prior head rows still exist. `new_effect_date` (the new version's účinnost, i.e.
    effective date) is the boundary at which old versions close and new ones open.

    Correctness guards:
      - A change is only written with a boundary that is not backdated (new_effect_date on or after the
        prior head's valid_from). A missing or backdated boundary is counted as 'ambiguous' and the old
        head is left open, so no inverted window (valid_from after valid_to) is ever written; such a
        window would vanish from point-in-time reads.
      - A removed label's head is closed and demoted (is_head false), so a later re-add is seen as a fresh
        version after the repeal gap rather than a change that overwrites the repeal boundary, and the
        'removed' count is idempotent (a re-run no longer sees the closed head).
      - version_no is max(existing) + 1 per label, so a re-add after a repeal cannot collide on uq_pv.
      - Structural section anchors (is_anchor_only) carry no versionable text. ``apply`` excludes them
        from the version-1 seed, so amend excludes them too; otherwise a re-ingest would version every
        anchor row ('added' for an anchor without a prior head, 'appended' for a changed one). The
        exclusion uses the same column-existence guard as ``apply``.

    Provenance and events are gated by ``temporal_substrate_enabled`` (off by default). With the flag off,
    new version rows get a NULL source_event, no approval link is written and no nexus_event is emitted.
    With it on:
      - each new version row (appended, added, re-added) gets provision_version.source_event set to
        `source_event`, the id of the authorising event or decision;
      - if `approval_id` is given, the authorising discovery_queue row records which document and effect
        date this amendment materialised;
      - on a real change (see ``_has_real_change``) amend emits an 'amended' event (external_id, counts,
        effect date, source_event, approval link) and a 'reindex-required' event for the GNN-stale
        monitor, audit feed and change diff. ``emit=False`` keeps the provenance writes but suppresses the
        events, for callers that emit once at the end of a batch.

    Returns the change counts (appended, added, readded, removed, noop, ambiguous), plus provenance keys
    when the flag is on.
    """
    gated = get_settings().temporal_substrate_enabled  # gates provenance stamping and amend events
    anchor_excl = (
        " AND p.is_anchor_only = false"
        if await conn.fetchval(
            "SELECT 1 FROM information_schema.columns WHERE table_name='provision' AND column_name='is_anchor_only'"
        )
        else ""
    )
    new_rows = await conn.fetch(
        "SELECT p.label, p.text, p.text_hash, p.text_format FROM provision p "
        f"WHERE p.source_document_id = $1 AND p.label IS NOT NULL{anchor_excl}",
        source_document_id,
    )
    new = {r["label"]: r for r in new_rows}
    c = {"appended": 0, "added": 0, "readded": 0, "removed": 0, "noop": 0, "ambiguous": 0}
    # The authorising event id stamped on each new version row; NULL unless the flag is on.
    se = source_event if gated else None

    async def _next_version_no(label) -> int:
        n = await conn.fetchval(
            "SELECT max(version_no) FROM provision_version WHERE source_document_id=$1 AND provision_label=$2",
            source_document_id,
            label,
        )
        return (n or 0) + 1

    async def _insert_head(label, version_no, r):
        await conn.execute(
            """INSERT INTO provision_version (id, source_document_id, provision_label, version_no,
                   valid_from, valid_to, text, text_hash, text_format, is_head, source_event)
               VALUES ($1,$2,$3,$4,$5::date,NULL,$6,$7,$8,true,$9)""",
            uuid4(),
            source_document_id,
            label,
            version_no,
            new_effect_date,
            r["text"],
            r["text_hash"],
            r["text_format"],
            se,
        )

    for label, r in new.items():
        prev = prior_heads.get(label)
        if prev is None:  # added, or re-added after a repeal
            nv = await _next_version_no(label)
            await _insert_head(label, nv, r)
            c["readded" if nv > 1 else "added"] += 1
        elif prev[1] == r["text_hash"]:  # unchanged: the head is still valid, no new version
            c["noop"] += 1
        elif new_effect_date is None or (prev[2] is not None and new_effect_date < prev[2]):
            # Ambiguous: the text really changed (text_hash differs) but there is no clean window boundary
            # (účinnost missing or backdated). Deliberately not versioned: a new head without a valid boundary
            # would create an inverted window that vanishes from point-in-time reads. The old head stays open
            # and nothing is emitted (_has_real_change excludes 'ambiguous'), so no false "obligation changed"
            # alert fires. Known gap: a genuine amendment whose účinnost date is missing currently looks like
            # a no-op to the operator. A separate low-severity 'amend-ambiguous' diagnostic event (distinct
            # from 'amended', so it never trips the alert chain) would surface it; it is not implemented yet.
            c["ambiguous"] += 1  # leave the old head open
        else:  # changed: close the old head, append a new one
            # Demote any current head for this label, not only an open one. A temporal backfill
            # (set_effect_date) may have set the live head's valid_to (e.g. the end of a consolidation window)
            # while leaving is_head true; demoting only rows with valid_to IS NULL would then match nothing and
            # the new-head INSERT would collide on uq_pv_head. Closing at new_effect_date gives the half-open
            # window [old valid_from, new_effect_date), which also fixes an off-by-one from a backfill that
            # stored an inclusive last day.
            await conn.execute(
                "UPDATE provision_version SET is_head=false, valid_to=$3::date "
                "WHERE source_document_id=$1 AND provision_label=$2 AND is_head",
                source_document_id,
                label,
                new_effect_date,
            )  # demote first so uq_pv_head never sees two heads
            await _insert_head(label, await _next_version_no(label), r)  # max + 1: defence in depth against uq_pv
            c["appended"] += 1

    for label, prev in prior_heads.items():  # removed: close and demote the repealed head
        if label not in new:
            if new_effect_date is None or (prev[2] is not None and new_effect_date < prev[2]):
                c["ambiguous"] += 1
            else:
                status = await conn.execute(  # demote any current head, as in the changed branch
                    "UPDATE provision_version SET is_head=false, valid_to=$3::date "
                    "WHERE source_document_id=$1 AND provision_label=$2 AND is_head",
                    source_document_id,
                    label,
                    new_effect_date,
                )
                if int(status.split()[-1]) > 0:  # count only a head actually closed (idempotent)
                    c["removed"] += 1

    # Copy the head version's effect date onto the new provision rows (NULL only, so idempotent).
    await conn.execute(
        """UPDATE provision p SET valid_from = pv.valid_from
             FROM provision_version pv
            WHERE pv.source_document_id = $1 AND pv.is_head AND pv.provision_label = p.label
              AND p.source_document_id = $1 AND p.valid_from IS NULL""",
        source_document_id,
    )

    # Provenance link and event emission; skipped entirely when the flag is off.
    if gated:
        c["source_event"] = str(se) if se is not None else None
        real = _has_real_change(c)  # ignores noop and ambiguous rows
        # Link the authorising approval, if given: record on its discovery_queue row which document and
        # effect date this amendment materialised (the human-in-the-loop trail).
        if approval_id is not None and await conn.fetchval("SELECT to_regclass('public.discovery_queue')"):
            await _link_approval(conn, approval_id, source_document_id, new_effect_date, c)
            c["approval_id"] = str(approval_id)
        # Emit only on a real change: an all-noop or ambiguous re-ingest emits nothing, so the change-detection
        # and alert chain (impact, version_diff) never reports a false "your obligation changed".
        if emit and real:
            ext = await conn.fetchval("SELECT external_id FROM source_document WHERE id=$1", source_document_id)
            payload = {
                "source_document_id": str(source_document_id),
                "external_id": ext,
                "counts": {k: c[k] for k in (*_REAL_CHANGE_KEYS, "noop", "ambiguous")},
                "new_effect_date": new_effect_date.isoformat() if new_effect_date else None,
                "source_event": c.get("source_event"),
                "approval_id": c.get("approval_id"),
            }
            await _emit_amend_events(conn, payload)
            c["emitted"] = True
    return c


async def _link_approval(conn, approval_id, source_document_id, new_effect_date, counts: dict) -> None:
    """Record on the authorising discovery_queue row what this amendment materialised.

    Stores the amended source_document, the effect-date boundary and the change counts. The jsonb merge is
    additive and never overwrites the operator's decision or note. A missing queue row is a no-op, since an
    amendment can be authorised by something other than a queued discovery.
    """
    await conn.execute(
        """UPDATE discovery_queue
              SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                  'amend_materialized', jsonb_build_object(
                      'source_document_id', $2::text,
                      'new_effect_date', $3::text,
                      'counts', $4::jsonb,
                      'at', now()::text))
            WHERE id = $1""",
        approval_id,
        str(source_document_id),
        new_effect_date.isoformat() if new_effect_date else None,
        json.dumps({k: counts.get(k, 0) for k in (*_REAL_CHANGE_KEYS, "noop", "ambiguous")}, ensure_ascii=False),
    )


async def _emit_amend_events(conn, payload: dict) -> None:
    """Emit the 'amended' and 'reindex-required' events through refresh's nexus_event writer.

    Reusing the existing event store means the monitors that already drain 'graph-changed'
    (graph_changed_consumer, monitoring._STALE_KINDS) pick these up without new plumbing. Both are written
    in the caller's transaction, so a reader never sees one without the other.
    """
    from pythia.ingest.refresh import _emit  # the canonical nexus_event writer

    # refresh_one creates nexus_event (via refresh._DDL) before amending, but a direct amend caller may not
    # have. Create just that table so a direct amend can emit; gen_random_uuid avoids the uuid-ossp
    # dependency of refresh._DDL's other tables. Idempotent.
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS nexus_event (
               id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
               kind text NOT NULL, payload jsonb, created_at timestamptz NOT NULL DEFAULT now(),
               consumed_at timestamptz);
           CREATE INDEX IF NOT EXISTS ix_event_unconsumed ON nexus_event (created_at) WHERE consumed_at IS NULL;"""
    )
    await _emit(conn, AMENDED_EVENT, payload)
    await _emit(
        conn,
        REINDEX_REQUIRED_EVENT,
        {
            "external_id": payload.get("external_id"),
            "source_document_id": payload.get("source_document_id"),
            "reason": "version lineage amended — GNN/index view is stale",
        },
    )


async def migrate_add_date_application(*, apply: bool = False) -> dict:
    """Rebuild every stored provenance bundle so it carries the 'date_application' key (None by default).

    A schema migration for the bundle, guarded so it cannot change any other field. Idempotent.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        ids = [
            r["id"]
            for r in await conn.fetch(
                "SELECT id FROM source_document WHERE provenance IS NOT NULL ORDER BY external_id"
            )
        ]
        missing = await conn.fetchval(
            "SELECT count(*) FROM source_document "
            "WHERE provenance IS NOT NULL AND NOT (provenance ? 'date_application')"
        )
        if not apply:
            return {"mode": "DRY-RUN", "docs": len(ids), "missing_date_application": missing}
        for doc_id in ids:
            async with conn.transaction():
                await rebuild_provenance(conn, doc_id=doc_id, only_new_key=True)
        return {"mode": "APPLIED", "docs": len(ids), "was_missing": missing}


async def recompute_temporal_status(*, apply: bool = False) -> dict:
    """Re-derive the stored temporal_status for documents whose status has changed with the date.

    temporal_status is derived from valid_from and valid_to against today and frozen at ingest, so it goes
    stale as dates pass (a NOT_YET_EFFECTIVE regulation becomes IN_FORCE; an act past its valid_to becomes
    REPEALED). This updates the column and the bundle together, for documents that have a bundle. Run it
    periodically (see ``pythia.ingest.scheduler``). Retrieval masks already correct for this at query time;
    this refreshes the stored status shown in the UI. Idempotent.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, external_id, valid_from, valid_to, temporal_status FROM source_document "
            "WHERE provenance IS NOT NULL"
        )
        today = date.today()
        changes = [
            (r["id"], r["external_id"], r["temporal_status"], _temporal_status(r["valid_from"], r["valid_to"], today))
            for r in rows
        ]
        changes = [ch for ch in changes if ch[2] != ch[3]]
        if not apply:
            return {"mode": "DRY-RUN", "would_change": [(e, o, n) for _i, e, o, n in changes]}
        for doc_id, _e, _o, _n in changes:
            async with conn.transaction():
                await rebuild_provenance(conn, doc_id=doc_id, recompute_status=True)
        return {"mode": "APPLIED", "changed": len(changes)}


def _pit_status(valid_from: date | None, valid_to: date | None, head_closed: bool, as_of: date | None = None) -> str:
    """Return one provision's point-in-time status from its head version window.

    Pure and deterministic; mirrors the document-level taxonomy rule at provision grain. Inputs are the
    head provision_version's dates and whether that row is closed (is_head false, meaning a later version
    superseded it). `as_of` defaults to today.

      - valid_from after as_of: NOT_YET_IN_FORCE (enacted but not yet effective).
      - head closed, or valid_to on or before as_of: SUPERSEDED (a later version took over or the end date
        passed); the provision-level analogue of a repealed document.
      - otherwise: IN_FORCE (the currently applicable text).

    A NULL valid_from with an open head counts as IN_FORCE: an unknown lower bound is not "not yet", and
    the document-level masks already exclude genuinely future law.
    """
    today = as_of or date.today()
    if valid_from is not None and valid_from > today:
        return _PIT_NOT_YET
    if head_closed or (valid_to is not None and valid_to <= today):
        return _PIT_SUPERSEDED
    return _PIT_IN_FORCE


async def recompute_provision_temporal_status(*, apply: bool = False, as_of: date | None = None) -> dict:
    """Derive provision.pit_status for every provision from its head provision_version window.

    This is the per-provision facet, distinct from the document-level source_document.temporal_status that
    d1_check guards (see the comment above ``_PIT_DDL``). It only projects stored version dates: no version
    row is created or removed, it is network-free, and it is idempotent (a second run with no date crossing
    changes nothing). A provision with no version row (e.g. a structural anchor, or a corpus not yet
    versioned) is left NULL rather than marked IN_FORCE. The amend path does not call this; staleness is
    tracked through the text hash.

    Calling it creates the pit_status column (via ``_PIT_DDL``). It is the explicit activation path for
    operators and tests; the daily scheduler (``scheduler.run_temporal``) only calls it when
    temporal_substrate_enabled is on, so with the flag off (the default) a scheduled run never adds the column.
    """
    today = as_of or date.today()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_PIT_DDL)
        # Read the head row (unique per label via uq_pv_head) when there is one, else the latest version, and
        # let _pit_status decide from its window. head_closed means the latest version is no longer head, i.e.
        # the provision was superseded or removed.
        rows = await conn.fetch(
            """SELECT p.id::text AS id, p.pit_status AS cur,
                      pv.valid_from, pv.valid_to,
                      (pv.id IS NOT NULL AND NOT pv.is_head) AS head_closed,
                      (pv.id IS NULL) AS no_version
                 FROM provision p
                 LEFT JOIN LATERAL (
                     SELECT v.id, v.valid_from, v.valid_to, v.is_head
                       FROM provision_version v
                      WHERE v.source_document_id = p.source_document_id
                        AND v.provision_label = p.label
                      ORDER BY v.is_head DESC, v.version_no DESC
                      LIMIT 1) pv ON true"""
        )
        plan = []
        for r in rows:
            if r["no_version"]:
                new = None  # no version lineage: leave NULL rather than guess
            else:
                new = _pit_status(r["valid_from"], r["valid_to"], bool(r["head_closed"]), today)
            if r["cur"] != new:
                plan.append((r["id"], new))
        if not apply:
            from collections import Counter

            dist = Counter(n for _i, n in plan)
            return {"mode": "DRY-RUN", "would_change": len(plan), "to": dict(dist)}
        if plan:
            await conn.executemany("UPDATE provision SET pit_status = $2 WHERE id = $1::uuid", plan)
        return {"mode": "APPLIED", "changed": len(plan)}


_VERSION_SUFFIX_RE = re.compile(r"/(\d{4}-\d{2}-\d{2})$")


async def backfill_esbirka_version_dates(*, apply: bool = False) -> dict:
    """Recover valid_from for e-Sbírka acts whose version nodes omit 'účinnost-znění-od'.

    Some acts (e.g. 89/2012 Sb.) leave out the field. The consolidated version's effective date is also
    encoded in the version-path suffix, which matches the explicit field for every consolidated (KONSOL)
    version. The promulgation sentinel 0000-00-00 parses to None and stays NULL. Idempotent.
    """
    from pythia.ingest.esbirka import _parse_date

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, external_id, metadata->>'version_path' AS vp FROM source_document "
            "WHERE provenance->>'source'='esbirka' AND (provenance->>'valid_from') IS NULL"
        )
        plan = []
        for r in rows:
            m = _VERSION_SUFFIX_RE.search(r["vp"] or "")
            vf = _parse_date(m.group(1)) if m else None
            plan.append((r["id"], r["external_id"], vf))
        sample = {ext: (vf.isoformat() if vf else None) for _, ext, vf in plan}
        provs_to_fix = (
            await conn.fetchval(
                "SELECT count(*) FROM provision p JOIN source_document sd ON sd.id=p.source_document_id "
                "WHERE sd.provenance->>'source'='esbirka' AND p.valid_from IS NULL "
                "AND sd.id = ANY($1::uuid[])",
                [pid for pid, _, vf in plan if vf is not None],
            )
            or 0
        )
        if not apply:
            return {
                "mode": "DRY-RUN",
                "docs_to_fix": sum(1 for *_, vf in plan if vf),
                "sample": sample,
                "provisions_to_fix": provs_to_fix,
            }
        fixed = 0
        for pid, ext, vf in plan:
            if vf is None:
                continue
            async with conn.transaction():
                await set_effect_date(conn, pid, vf)
            fixed += 1
        return {"mode": "APPLIED", "docs_fixed": fixed, "provisions_to_fix": provs_to_fix, "sample": sample}


async def _main() -> None:
    if "--down" in sys.argv:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(_DOWN)
        print("⏪ reverted: provision_version dropped + source_document.provenance dropped.")
        await close_pool()
        return
    res = await apply(apply=("--apply" in sys.argv))
    print(f"=== D1 provision-versioning — {res['mode']} ===")
    print(f"  provenance bundles: {res['docs']} documents   ·   version_no=1 seeds: {res['provisions']} provisions")
    if res["mode"].startswith("DRY"):
        print("\n  (preview only — re-run with --apply; --down to revert)")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
