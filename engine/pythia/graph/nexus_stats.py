"""Corpus, graph and freshness statistics for the operations dashboard.

A read-only projection rendered by the knowledge-graph panel of the web app: the corpus by axis, the
typed graph, centrality and communities, embedding coverage, the GNN checkpoint, database size, and
graph freshness. Freshness compares the live edge-set hash with the last build's fingerprint; it is
the headline signal, because graph faults show up here even while every endpoint still returns 200.
Results are cached for about 45 seconds and every query is index-backed.
"""

from __future__ import annotations

import pathlib
import time as _time
from typing import Any

from pythia.db import get_pool
from pythia.graph.intelligence import current_fingerprint, last_fingerprint

_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_TTL = 45.0
_GB = 1024**3


def _gnn_checkpoint() -> dict[str, Any]:
    """Describe the newest GNN checkpoint in train/ (stage 2 preferred over stage 1)."""
    tr = pathlib.Path(__file__).resolve().parent.parent.parent / "train"
    for name in ("gnn_stage2.pt", "gnn_stage1.pt"):
        p = tr / name
        if p.exists():
            st = p.stat()
            return {
                "present": True,
                "checkpoint": name,
                "size_bytes": st.st_size,
                "trained_at": _time.strftime("%Y-%m-%d", _time.gmtime(st.st_mtime)),
                "stage": 2 if "stage2" in name else 1,
            }
    return {"present": False, "checkpoint": None, "stage": 0}


async def _rows(conn, sql: str, *args) -> list[dict]:
    return [dict(r) for r in await conn.fetch(sql, *args)]


async def nexus_events(limit: int = 50) -> list[dict[str, Any]]:
    """Return recent nexus_event rows (ingest, reindex, materialise, check) for the panel's run log.

    nexus_event is the event store the ingest paths already write to.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT kind, payload, created_at FROM nexus_event ORDER BY created_at DESC LIMIT $1", limit
        )
    out = []
    for r in rows:
        p = r["payload"]
        if isinstance(p, str):
            import json as _json

            try:
                p = _json.loads(p)
            except ValueError:
                p = {}
        out.append({"kind": r["kind"], "payload": p or {}, "created_at": r["created_at"].isoformat()})
    return out


async def nexus_health() -> dict[str, Any]:
    """Return a cheap liveness summary: corpus reachable, plus the graph freshness flag."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        live = await current_fingerprint(conn)
        last = await last_fingerprint(conn)
        docs = await conn.fetchval("SELECT count(*) FROM source_document")
    fresh = bool(last and last.get("edge_hash") == live["edge_hash"] and last.get("edge_count") == live["edge_count"])
    return {
        "ok": True,
        "documents": docs,
        "fresh": fresh,
        "edge_count": live["edge_count"],
        "last_built_at": (last or {}).get("built_at"),
    }


async def nexus_stats(*, force: bool = False) -> dict[str, Any]:
    """Return the full statistics document, cached for _TTL seconds unless `force` is set."""
    now = _time.time()
    if not force and _CACHE["data"] is not None and now - _CACHE["ts"] < _TTL:
        return _CACHE["data"]

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Freshness: compare the live edge-set content hash with the last persisted build's.
        live = await current_fingerprint(conn)
        last = await last_fingerprint(conn)
        fresh = bool(
            last and last.get("edge_hash") == live["edge_hash"] and last.get("edge_count") == live["edge_count"]
        )

        corpus_h = await _rows(
            conn, "SELECT legal_hierarchy AS k, count(*) AS n FROM source_document GROUP BY 1 ORDER BY 2 DESC"
        )
        corpus_j = await _rows(
            conn, "SELECT jurisdiction AS k, count(*) AS n FROM source_document GROUP BY 1 ORDER BY 2 DESC"
        )
        corpus_t = await _rows(
            conn, "SELECT temporal_status AS k, count(*) AS n FROM source_document GROUP BY 1 ORDER BY 2 DESC"
        )
        caselaw_courts = await _rows(
            conn,
            "SELECT issuing_authority AS k, count(*) AS n FROM source_document WHERE legal_hierarchy='CASE_LAW' GROUP BY 1 ORDER BY 2 DESC LIMIT 12",
        )

        prov_total = await conn.fetchval("SELECT count(*) FROM provision")
        prov_inforce = await conn.fetchval(
            "SELECT count(*) FROM provision WHERE (valid_to IS NULL OR valid_to > CURRENT_DATE) "
            "AND (valid_from IS NULL OR valid_from <= CURRENT_DATE)"
        )

        edges_type = await _rows(
            conn, "SELECT edge_type AS k, count(*) AS n FROM pythia_edge GROUP BY 1 ORDER BY 2 DESC"
        )
        edges_src = await _rows(conn, "SELECT source AS k, count(*) AS n FROM pythia_edge GROUP BY 1 ORDER BY 2 DESC")
        dangling = await conn.fetchval("SELECT count(*) FROM pythia_edge WHERE dst IS NULL")
        self_loops = await conn.fetchval("SELECT count(*) FROM pythia_edge WHERE src = dst")

        n_comm = await conn.fetchval("SELECT count(DISTINCT community) FROM provision WHERE community IS NOT NULL")
        scored = await conn.fetchval("SELECT count(*) FROM provision WHERE centrality IS NOT NULL")
        isolated_inforce = await conn.fetchval(
            "SELECT count(*) FROM provision WHERE centrality IS NULL "
            "AND (valid_to IS NULL OR valid_to > CURRENT_DATE) AND (valid_from IS NULL OR valid_from <= CURRENT_DATE)"
        )
        community_sizes = await _rows(
            conn,
            "SELECT community AS k, count(*) AS n FROM provision WHERE community IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 8",
        )
        top_hubs = await _rows(
            conn,
            # ::float8 so JSON carries a number, not a Decimal string (the UI calls .toFixed on it)
            "SELECT p.label AS label, sd.external_id AS ext, round(p.centrality::numeric,6)::float8 AS centrality "
            "FROM provision p JOIN source_document sd ON sd.id=p.source_document_id "
            "WHERE p.centrality IS NOT NULL AND p.legal_hierarchy IS DISTINCT FROM 'CASE_LAW' "
            "ORDER BY p.centrality DESC LIMIT 8",
        )

        emb_total = await conn.fetchval("SELECT count(*) FROM provision WHERE embedding_bge IS NOT NULL")
        emb_gap = await conn.fetchval(
            "SELECT count(*) FROM provision WHERE embedding_bge IS NULL "
            "AND (valid_to IS NULL OR valid_to > CURRENT_DATE) AND (valid_from IS NULL OR valid_from <= CURRENT_DATE)"
        )
        emb_by_h = await _rows(
            conn,
            "SELECT legal_hierarchy AS k, count(*) AS total, count(embedding_bge) AS embedded "
            "FROM provision GROUP BY 1 ORDER BY 2 DESC",
        )

        # Areas of law (legal_domain): documents and provisions per area. NULL or empty domains and the
        # literal 'uncategorized' tag merge into one bucket. Provisions count toward every area their
        # document is tagged with, so the figures mean "law covering this area", not a disjoint split.
        areas = await _rows(
            conn,
            """
          SELECT CASE WHEN d IS NULL OR d = '' OR lower(d) = 'uncategorized'
                      THEN 'uncategorized' ELSE d END AS area,
                 count(DISTINCT sd.id) AS docs, count(p.id) AS provisions
            FROM source_document sd
            LEFT JOIN LATERAL unnest(
                 CASE WHEN sd.legal_domain IS NULL OR cardinality(sd.legal_domain) = 0
                      THEN ARRAY[NULL]::text[] ELSE sd.legal_domain END) AS d ON true
            LEFT JOIN provision p ON p.source_document_id = sd.id
           GROUP BY 1 ORDER BY provisions DESC NULLS LAST""",
        )
        # Key instruments: the largest acts by provision count.
        instruments = await _rows(
            conn,
            """
          SELECT sd.external_id AS ext, sd.title AS title, sd.jurisdiction AS jurisdiction,
                 sd.legal_hierarchy AS hierarchy, count(p.id) AS provisions
            FROM source_document sd LEFT JOIN provision p ON p.source_document_id = sd.id
           WHERE sd.legal_hierarchy IN ('STATUTE', 'REGIONAL_MUNICIPAL_PREDPIS')
           GROUP BY sd.id ORDER BY count(p.id) DESC LIMIT 24""",
        )

        # Extraction coverage: which extraction waves actually ran on which act. coverage_counts returns
        # a zeroed present=False block when the table does not exist, so a corpus without the coverage
        # schema never fails here.
        from pythia.ingest.extraction_coverage import coverage_counts

        extraction_cov = await coverage_counts(conn)

        db_size = await conn.fetchval("SELECT pg_database_size('pythia')")
        top_tables = await _rows(
            conn,
            "SELECT relname AS k, pg_total_relation_size(c.oid) AS bytes, pg_indexes_size(c.oid) AS idx_bytes "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relkind='r' ORDER BY 2 DESC LIMIT 10",
        )
        cache_hit = await conn.fetchval(
            "SELECT round(100.0*sum(heap_blks_hit)/NULLIF(sum(heap_blks_hit+heap_blks_read),0),1) "
            "FROM pg_statio_user_tables"
        )

    out = {
        "freshness": {
            "fresh": fresh,
            "live_edge_hash": live["edge_hash"],
            "live_edge_count": live["edge_count"],
            "last_edge_hash": (last or {}).get("edge_hash"),
            "last_built_at": (last or {}).get("built_at"),
            "staleness_edges": abs(live["edge_count"] - (last or {}).get("edge_count", live["edge_count"])),
        },
        "corpus": {
            "documents": sum(r["n"] for r in corpus_h),
            "by_hierarchy": corpus_h,
            "by_jurisdiction": corpus_j,
            "by_temporal": corpus_t,
            "case_law_by_court": caselaw_courts,
        },
        "provisions": {"total": prov_total, "in_force": prov_inforce},
        "graph": {
            "edges_total": sum(r["n"] for r in edges_type),
            "by_type": edges_type,
            "by_source": edges_src,
            "dangling": dangling,
            "self_loops": self_loops,
        },
        "intelligence": {
            "n_communities": n_comm,
            "scored": scored,
            "isolated_in_force": isolated_inforce,
            "community_sizes_top": community_sizes,
            "top_hubs": top_hubs,
        },
        "embedding": {
            "dim": 1024,
            "model": "bge-m3",
            "embedded": emb_total,
            "coverage_gap_in_force": emb_gap,
            "by_hierarchy": emb_by_h,
        },
        "areas": areas,  # legal_domain breakdown (areas of law, with sizes)
        "instruments": instruments,  # the biggest acts (name + provision count)
        "extraction_coverage": extraction_cov,  # which extraction waves ran per act (absent-safe)
        "gnn": _gnn_checkpoint(),
        "db": {
            "size_bytes": db_size,
            "size_gb": round(db_size / _GB, 2) if db_size else None,
            "cache_hit_pct": float(cache_hit) if cache_hit is not None else None,
            "top_tables": [
                {"table": t["k"], "bytes": int(t["bytes"]), "index_bytes": int(t["idx_bytes"])} for t in top_tables
            ],
        },
    }
    _CACHE["ts"] = now
    _CACHE["data"] = out
    return out
