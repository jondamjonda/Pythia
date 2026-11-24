"""Keep stored Sbírka acts current: detect a new consolidated version, amend, diff, reindex, notify.

Covers acts with ``publisher='Sbírka zákonů'`` only (``_PUB`` filters every query here), so it keeps
the Sbírka slice current rather than the whole corpus; EUR-Lex, ČNB and regional acts get no automatic
currency check from this module. Scheduling lives in ``pythia.ingest.scheduler``.

Run (from engine/):
  .venv/bin/python -m pythia.ingest.refresh 21/1992          # check and refresh one act
  .venv/bin/python -m pythia.ingest.refresh --all            # check and refresh every stored act
  .venv/bin/python -m pythia.ingest.refresh 21/1992 --check  # detect only (no write)
  .venv/bin/python -m pythia.ingest.refresh 21/1992 --force  # re-ingest even if unchanged
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime

import httpx

from pythia.config import get_settings
from pythia.db import close_pool, get_pool
from pythia.ingest.enrich_parity import _BACKFILL_DEFINED_TERMS, _FILL_DEONTIC, _FILL_RESOLVED
from pythia.ingest.esbirka import _FETCH_LIMITS, _FETCH_TIMEOUT, CzechActSpec, _resolve_version, ingest_act

_DDL = """
CREATE TABLE IF NOT EXISTS document_version (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    external_id text NOT NULL, version_path text, raw_checksum text,
    valid_from date, valid_to date, n_provisions int, change_summary text,
    detected_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_docver_ext ON document_version (external_id, detected_at DESC);
CREATE TABLE IF NOT EXISTS refresh_run (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
    n_checked int, n_changed int, n_reindexed int, n_errors int, detail jsonb);
CREATE TABLE IF NOT EXISTS nexus_event (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    kind text NOT NULL, payload jsonb, created_at timestamptz NOT NULL DEFAULT now(),
    consumed_at timestamptz);
CREATE INDEX IF NOT EXISTS ix_event_unconsumed ON nexus_event (created_at) WHERE consumed_at IS NULL;
"""

_PUB = "Sbírka zákonů"


async def _stored(conn, external_id: str):
    return await conn.fetchrow(
        f"""SELECT sd.id, sd.title, sd.legal_hierarchy, sd.document_type, sd.raw_checksum,
                   sd.metadata->>'version_path' AS vp, sd.metadata->>'short' AS short,
                   (SELECT count(*) FROM provision p WHERE p.source_document_id = sd.id) AS np
              FROM source_document sd WHERE sd.external_id = $1 AND sd.publisher = '{_PUB}'""",
        external_id,
    )


def _spec_from(external_id: str, row) -> CzechActSpec:
    number, year = external_id.split("/")
    return CzechActSpec(
        int(year),
        int(number),
        row["short"] or external_id,
        row["title"],
        row["legal_hierarchy"] or "cz-act",
        row["document_type"] or "act",
    )


async def _provision_hashes(conn, external_id: str) -> dict[str, str]:
    rows = await conn.fetch(
        f"""SELECT p.label, p.text_hash FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
             WHERE sd.external_id = $1 AND sd.publisher = '{_PUB}'""",
        external_id,
    )
    return {r["label"]: r["text_hash"] for r in rows if r["label"]}


# --- Preserving defined terms across the amendment re-ingest -----------------------------------------------
# ingest_act(amend=True) calls _delete_existing(keep_document=True), which deletes the act's provision rows;
# the defined_term.definition_provision_id foreign key (ON DELETE CASCADE) takes the LLM-extracted
# defined_term rows with them. The _reindex backfill only copies the defined_term table into
# provision.defined_terms[] and cannot re-extract, so without these helpers the definitions would be lost
# for that act. They snapshot the rows before the delete, keyed by provision label (the re-ingest reuses
# labels but mints new provision UUIDs), and re-attach them to the new provision ids afterwards. A label
# that no longer exists after the amendment (a genuinely removed provision) is dropped on purpose. No-op
# for acts without defined_term rows.


async def _snapshot_defined_terms(conn, external_id: str) -> list[dict]:
    """Capture the act's defined_term rows, with their host provision's label, before the amend delete."""
    rows = await conn.fetch(
        f"""SELECT dt.term, p.label AS prov_label, dt.scope_document_id::text AS scope_doc,
                   dt.definition_text, dt.extracted_by, dt.confidence
              FROM defined_term dt
              JOIN provision p ON p.id = dt.definition_provision_id
              JOIN source_document sd ON sd.id = p.source_document_id
             WHERE sd.external_id = $1 AND sd.publisher = '{_PUB}' AND p.label IS NOT NULL""",
        external_id,
    )
    return [dict(r) for r in rows]


async def _restore_defined_terms(conn, external_id: str, snapshot: list[dict]) -> int:
    """Re-attach snapshotted defined_term rows to the act's new provisions, matched by label.

    Also re-stamps provision.defined_terms[] and is_definition so the array column reflects the restore.
    Returns the number of rows restored.
    """
    if not snapshot:
        return 0
    new_ids = {
        r["label"]: r["id"]
        for r in await conn.fetch(
            f"""SELECT p.id::text AS id, p.label FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
             WHERE sd.external_id = $1 AND sd.publisher = '{_PUB}' AND p.label IS NOT NULL""",
            external_id,
        )
    }
    restored, terms_by_prov = 0, {}
    for row in snapshot:
        pid = new_ids.get(row["prov_label"])
        if pid is None:  # provision removed by the amendment; nowhere to attach
            continue
        await conn.execute(
            """INSERT INTO defined_term
                   (term, scope_document_id, definition_provision_id, definition_text,
                    extracted_by, confidence, created_at)
               VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6, now())""",
            row["term"],
            row["scope_doc"],
            pid,
            row["definition_text"],
            row["extracted_by"],
            row["confidence"],
        )
        terms_by_prov.setdefault(pid, []).append(row["term"])
        restored += 1
    for pid, terms in terms_by_prov.items():  # mirror into the array column (idempotent)
        await conn.execute(
            "UPDATE provision SET is_definition = true, defined_terms = $2 WHERE id = $1::uuid", pid, sorted(set(terms))
        )
    return restored


async def _emit(conn, kind: str, payload: dict) -> None:
    await conn.execute(
        "INSERT INTO nexus_event (kind, payload) VALUES ($1, $2::jsonb)", kind, json.dumps(payload, ensure_ascii=False)
    )
    print(f"   ⚡ event {kind}: {payload}")


async def check_one(external_id: str) -> dict:
    """Detect a change cheaply: compare the current consolidated version path with the stored one."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        st = await _stored(conn, external_id)
    if not st:
        return {"external_id": external_id, "status": "absent"}
    number, year = external_id.split("/")
    async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, limits=_FETCH_LIMITS, follow_redirects=True) as client:
        new_vp, _meta = await _resolve_version(client, int(year), int(number), None)
    return {"external_id": external_id, "stored_vp": st["vp"], "current_vp": new_vp, "changed": new_vp != st["vp"]}


async def _reindex() -> dict:
    """Reindex the whole corpus after an amendment; idempotent.

    Embeds new provisions, re-links citations, materialises the graph, tags roles and rebuilds the
    graph-intelligence layer. Each step only processes missing or empty rows, so it is cheap when little
    changed. Finishes by running the case-law and graph-intelligence integrity checks and reports them.
    """
    from pythia.graph.materialize import materialize_layer_a
    from pythia.graph.role_classifier import classify_roles
    from pythia.ingest.embed_bge import embed_all, reembed_stale
    from pythia.ingest.resolve_internal import resolve_internal_xrefs
    from pythia.ingest.resolve_xrefs import resolve_external_xrefs
    from pythia.ingest.structural_edges import apply_structural

    from pythia.graph.intelligence import reindex_intelligence  # graph-intelligence layer (centrality, communities)

    n_emb = await embed_all(only_missing=True)  # embed the NEW provisions (bge-m3)
    # Off by default (temporal_substrate_enabled). When on, also re-embed exactly the provisions whose
    # embedded_text_hash no longer matches text_hash, never the whole corpus. This guards an in-place amend
    # path (a text UPDATE that would leave a non-null, stale vector). The bulk amend deletes and reinserts
    # provisions, so their embedding_bge is NULL and the only_missing pass above already covers them.
    n_reemb: int | None = None
    if get_settings().temporal_substrate_enabled:
        n_reemb = await reembed_stale()  # re-embed only provisions whose text changed
    await resolve_internal_xrefs(rematerialize=False)  # link intra-act citations
    await resolve_external_xrefs(rematerialize=False)  # link cross-act citations
    edges = (await materialize_layer_a())["total_edges"]  # rebuild the typed graph once
    await apply_structural(apply=True)  # re-derive the text-based structural edges
    await classify_roles(only_empty=True)  # cue-tag new provisions
    pool = await get_pool()
    async with pool.acquire() as conn:  # deontic + defined_terms + resolved_text
        for sql in (_BACKFILL_DEFINED_TERMS, _FILL_DEONTIC, _FILL_RESOLVED):
            await conn.execute(sql)
    # Rebuild centrality and communities as the final graph-mutating step. The order matters: PageRank and
    # Louvain must see the complete edge set (materialise, apply_structural and the backfills run first),
    # otherwise the navigation layer scores a partial graph. A graph fingerprint check makes this a near
    # no-op when the amendment changed no edges.
    intel = await reindex_intelligence()  # also invalidates the in-memory PageRank default cache
    from pythia.ingest.n5_check import check as _n5_check  # verify temporal and case-law invariants
    from pythia.ingest.n6_check import check as _n6_check  # and graph-intelligence freshness

    nv = await _n5_check()
    n6 = await _n6_check()
    if not nv["ok"]:
        bad = [k for k, v in nv["checks"].items() if not v]
        print(f"   ⚠ n5_check FAILED after reindex — investigate: {bad}")
    if not n6["ok"]:
        bad6 = [k for k, v in n6["checks"].items() if not v]
        print(f"   ⚠ n6_check FAILED after reindex — intelligence layer stale/inconsistent: {bad6}")
    out = {
        "embedded": n_emb,
        "edges": edges,
        "invariants_ok": nv["ok"] and n6["ok"],
        "n5_ok": nv["ok"],
        "n6_ok": n6["ok"],
        "intelligence": {
            "mode": intel["mode"],
            "nodes": intel["n"],
            "communities": intel["n_comm"],
            "edge_count": intel.get("edge_count"),
            "top_hub": intel.get("top_hub"),
        },
    }
    if n_reemb is not None:  # key present only when the flag is on
        out["reembedded_stale"] = n_reemb
    return out


async def refresh_one(external_id: str, *, force: bool = False, reindex: bool = True) -> dict:
    """Check one stored act and amend it if e-Sbírka has a new consolidated version.

    The check cheaply re-resolves the current version (``_resolve_version``: a few small JSON fetches,
    no full download) and compares its path with the stored one. An unchanged act is a no-op apart from
    bumping ``last_checked_at``. On a change (or with `force`) the act is re-ingested, a provision-level
    diff (added, changed and removed by text_hash) is computed, a ``document_version`` lineage row is
    appended, the corpus is reindexed unless `reindex` is false (new provisions are embedded with
    bge-m3; see ``_reindex``), and a ``graph-changed`` event is emitted that later triggers GNN
    re-adaptation.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_DDL)
        st = await _stored(conn, external_id)
    if not st:
        return {"external_id": external_id, "result": "absent"}
    chk = await check_one(external_id)
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE source_document SET last_checked_at = now() WHERE external_id = $1 AND publisher = '{_PUB}'",
            external_id,
        )
    if not chk["changed"] and not force:
        return {"external_id": external_id, "result": "unchanged", "version_path": st["vp"]}

    # Amend: capture the old shape, then re-ingest without losing history. The source_document and its version
    # history are kept, and ingest_act(amend=True) reconciles provision_version (changed: close the old
    # version and append a new one; added: seed; removed: close). Then diff for the lineage summary.
    async with pool.acquire() as conn:
        old = await _provision_hashes(conn, external_id)
        # Snapshot the LLM-extracted defined_term rows before the amend delete cascades them away.
        dt_snapshot = await _snapshot_defined_terms(conn, external_id)
    await ingest_act(_spec_from(external_id, st), amend=True)
    # Re-attach the snapshot to the new provisions (the _reindex backfill cannot regenerate definitions) and
    # warn if the act now has fewer defined_term rows than before the amendment.
    async with pool.acquire() as conn:
        n_restored = await _restore_defined_terms(conn, external_id, dt_snapshot)
        dt_after = await conn.fetchval(
            f"""SELECT count(*) FROM defined_term dt
                  JOIN provision p ON p.id = dt.definition_provision_id
                  JOIN source_document sd ON sd.id = p.source_document_id
                 WHERE sd.external_id = $1 AND sd.publisher = '{_PUB}'""",
            external_id,
        )
    if dt_after < len(dt_snapshot):
        print(
            f"   ⚠ defined_term ALARM {external_id}: dropped {len(dt_snapshot) - dt_after} of "
            f"{len(dt_snapshot)} defined terms across the amend (restored {n_restored}; "
            f"removed-provision defs have no home). Re-run enrich_definitions --acts {external_id} to refill."
        )
    async with pool.acquire() as conn:
        new = await _provision_hashes(conn, external_id)
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        changed = sorted(key for key in new if key in old and new[key] != old[key])
        summary = f"+{len(added)} ~{len(changed)} -{len(removed)} (provisions {len(old)}→{len(new)})"
        await conn.execute(
            f"""INSERT INTO document_version (external_id, version_path, raw_checksum, valid_from, valid_to,
                                             n_provisions, change_summary)
                SELECT external_id, metadata->>'version_path', raw_checksum, valid_from, valid_to, $2, $3
                  FROM source_document WHERE external_id = $1 AND publisher = '{_PUB}'""",
            external_id,
            len(new),
            summary,
        )
        new_vp = await conn.fetchval(
            f"SELECT metadata->>'version_path' FROM source_document WHERE external_id = $1 AND publisher = '{_PUB}'",
            external_id,
        )
    reidx = await _reindex() if reindex else None
    async with pool.acquire() as conn:
        await _emit(
            conn,
            "graph-changed",
            {
                "external_id": external_id,
                "diff": summary,
                "old_version": st["vp"],
                "new_version": new_vp,
                "reindex": reidx,
                "at": datetime.now(UTC).isoformat(),
            },
        )
    return {
        "external_id": external_id,
        "result": "amended",
        "old_vp": st["vp"],
        "new_vp": new_vp,
        "diff": summary,
        "added": added[:8],
        "removed": removed[:8],
        "changed": changed[:8],
        "reindex": reidx,
    }


async def refresh_all(*, force: bool = False, reindex: bool = True, limit: int | None = None) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_DDL)
        ext_ids = [
            r["external_id"]
            for r in await conn.fetch(
                f"SELECT external_id FROM source_document WHERE publisher = '{_PUB}' ORDER BY external_id"
            )
        ][:limit]
        run_id = await conn.fetchval("INSERT INTO refresh_run (started_at) VALUES (now()) RETURNING id")
    results, n_changed, n_err = [], 0, 0
    for ext in ext_ids:
        try:
            r = await refresh_one(ext, force=force, reindex=False)  # defer the reindex; it runs once at the end
            results.append(r)
            if r["result"] == "amended":
                n_changed += 1
                print(f"   ✎ {ext}: {r['diff']}")
        except Exception as exc:  # noqa: BLE001
            n_err += 1
            results.append({"external_id": ext, "result": "error", "error": repr(exc)[:140]})
            print(f"   ✗ {ext}: {repr(exc)[:80]}")
    reidx = await _reindex() if (reindex and n_changed) else None
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE refresh_run SET finished_at = now(), n_checked = $2, n_changed = $3,
                      n_reindexed = $4, n_errors = $5, detail = $6::jsonb WHERE id = $1""",
            run_id,
            len(ext_ids),
            n_changed,
            (1 if reidx else 0),
            n_err,
            json.dumps(results, ensure_ascii=False),
        )
        if n_changed:
            await _emit(conn, "refresh-complete", {"checked": len(ext_ids), "changed": n_changed, "reindex": reidx})
    return {"run_id": str(run_id), "checked": len(ext_ids), "changed": n_changed, "errors": n_err, "results": results}


async def _main(argv: list[str]) -> None:
    force = "--force" in argv
    check_only = "--check" in argv
    pos = [a for a in argv if not a.startswith("--")]
    try:
        if "--all" in argv:
            res = await refresh_all(force=force)
            print(f"\nrefresh_all: checked {res['checked']} · changed {res['changed']} · errors {res['errors']}")
        elif pos:
            if check_only:
                print(await check_one(pos[0]))
            else:
                res = await refresh_one(pos[0], force=force)
                print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print("usage: refresh.py [external_id] [--all] [--check] [--force]")
    finally:
        await close_pool()


# Scheduling lives in pythia.ingest.scheduler, which owns both this corpus refresh and the daily
# temporal-status recompute:
#   .venv/bin/python -m pythia.ingest.scheduler --print-crontab   # print ready-to-paste crontab lines
#   .venv/bin/python -m pythia.ingest.scheduler daemon            # long-lived APScheduler loop
if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1:]))
