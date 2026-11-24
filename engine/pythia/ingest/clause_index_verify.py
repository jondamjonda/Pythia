"""Verification harness for the clause index built by `pythia/ingest/clause_index.py`.

Runs four acceptance checks against the live database around a clause build: (a) --checksum shows
`provision` is untouched, (b) --restore-smoke shows dropping the table restores the exact baseline,
(c) --reproduce-proof shows the stored clause vectors reproduce the measured +0.18 gain, and
(d) --idempotency shows a second build is a no-op. It never writes `provision`, and writes
`provision_clause` only through clause_index's own idempotent build path.

Run (from engine/), in this order around `python -m pythia.ingest.clause_index --build --embed`:

  .venv/bin/python -m pythia.ingest.clause_index_verify --emit-baseline baseline.json   # before any build
  .venv/bin/python -m pythia.ingest.clause_index_verify --checksum                       # before (save line)
  .venv/bin/python -m pythia.ingest.clause_index --build --embed                         # the build
  .venv/bin/python -m pythia.ingest.clause_index_verify --checksum                       # after (diff line)
  .venv/bin/python -m pythia.ingest.clause_index_verify --idempotency                    # (d)
  .venv/bin/python -m pythia.ingest.clause_index_verify --reproduce-proof                # (c)
  .venv/bin/python -m pythia.ingest.clause_index --drop                                  # revert
  .venv/bin/python -m pythia.ingest.clause_index_verify --restore-smoke --baseline-file baseline.json  # (b)

Exits non-zero on any failed assertion, so a `&&` chain halts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics as st
import sys

from pythia.db import close_pool, get_pool

# Reuse the builder's candidate thresholds and clause regex so the verification samples exactly the
# rows the builder created.
from pythia.ingest.clause_index import (
    _MIN_LEN,
    _MIN_MARKERS,
    _CLAUSE_RE_SQL,
    build_clauses,
    ensure_clause_table,
)

_PROOF_ACT = "89/2012"  # the Civil Code, the act the original experiment measured
_PROOF_GATE = 0.03  # minimum mean gain for clause chunks to count as helping
_PROOF_EXPECTED = 0.18  # mean cosine gain measured by the original experiment
_PROOF_TOL = 0.06  # accepted deviation from +0.18 (re-encoding and sampling jitter); stays above the gate


# --------------------------------------------------------------------------- (a) provision checksum
async def provision_checksum() -> dict:
    """Return a fingerprint of `provision` that a clause build must not change.

    The fingerprint is md5(string_agg(text_hash ORDER BY id)) plus count(embedding_bge). text_hash is
    the per-provision content hash maintained by ingestion; the embedding count shows that no vector
    was added or removed. Read-only: for --checksum, run it before and after the build and diff the
    two printed lines, which must be equal.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT md5(string_agg(text_hash, '' ORDER BY id)) AS hash,
                      count(*)              AS n_provisions,
                      count(embedding_bge)  AS n_embedded
                 FROM provision"""
        )
    return {
        "provision_text_hash_md5": row["hash"],
        "n_provisions": row["n_provisions"],
        "n_embedded": row["n_embedded"],
    }


# --------------------------------------------------------------------------- (b) baseline emit / restore
async def emit_baseline(path: str) -> None:
    """Record the pre-build dense_seed result and provision checksum to `path`.

    Run with the clause read flag off, before provision_clause exists, so --restore-smoke can later
    prove the results are identical after the table is dropped.
    """
    from pythia.graph import seed as seed_mod
    from pythia.config import get_settings as _gs

    assert not _gs().clause_read_enabled, (
        "emit-baseline must run with the clause read flag OFF (the default) so the baseline is the "
        "true pre-clause behaviour; unset PYTHIA_CLAUSE_READ_ENABLED"
    )
    seeds = await seed_mod.dense_seed(_BASELINE_QUERY, k=_BASELINE_K)
    chk = await provision_checksum()
    payload = {
        "query": _BASELINE_QUERY,
        "k": _BASELINE_K,
        "seeds": [[s.provision_id, round(s.score, 6)] for s in seeds],
        "provision_checksum": chk,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"baseline written → {path}  ({len(seeds)} seeds, flag OFF)", flush=True)


_BASELINE_QUERY = "nabízení investičního fondu marketing notifikace"  # seed.py's own demo query
_BASELINE_K = 8


async def restore_smoke(baseline_path: str) -> None:
    """Assert that dropping provision_clause restored the exact baseline.

    Run after --drop. With the clause read flag forced off in-process, dense_seed must equal the
    baseline recorded by --emit-baseline before the table existed, and the provision checksum must
    match. This proves that with the flag off seed.py behaves exactly as without the clause index,
    and that dropping the table fully reverts it.
    """
    from pythia.graph import seed as seed_mod
    from pythia.config import PythiaSettings

    # Force the read flag off in-process regardless of the environment, so this tests the off path.
    # seed.py imports `get_settings` by name, so patch it there for the duration.
    real = seed_mod.get_settings
    seed_mod.get_settings = lambda: PythiaSettings(clause_read_enabled=False)
    try:
        with open(baseline_path, encoding="utf-8") as f:
            base = json.load(f)
        seeds = await seed_mod.dense_seed(base["query"], k=base["k"])
        got = [[s.provision_id, round(s.score, 6)] for s in seeds]
        exp = [[pid, round(sc, 6)] for pid, sc in base["seeds"]]
        assert got == exp, (
            "RESTORE FAILED — dense_seed (flag OFF, post-drop) != pre-build baseline.\n"
            f"  expected {len(exp)} seeds: {exp}\n  got      {len(got)} seeds: {got}"
        )
        chk = await provision_checksum()
        assert chk == base["provision_checksum"], (
            f"RESTORE FAILED — provision checksum drift: {chk} != {base['provision_checksum']}"
        )
        # provision_clause must be gone (--drop runs before this check).
        pool = await get_pool()
        async with pool.acquire() as conn:
            present = await conn.fetchval("SELECT to_regclass('public.provision_clause') IS NOT NULL")
        assert not present, "RESTORE FAILED — provision_clause still exists; run clause_index --drop first"
    finally:
        seed_mod.get_settings = real
    print(
        f"restore-smoke OK — flag-OFF dense_seed byte-identical to baseline ({len(seeds)} seeds), "
        "provision checksum unchanged, provision_clause dropped",
        flush=True,
    )


# --------------------------------------------------------------------------- (c) reproduce +0.18
def _parse_vec(txt: str):
    """Parse a pgvector text literal such as '[0.1,0.2,...]' into a numpy float32 vector."""
    import numpy as np

    return np.fromstring(txt.strip()[1:-1], sep=",", dtype="float32")


def _cos(a, b) -> float:
    import numpy as np

    a, b = np.asarray(a, dtype="float32"), np.asarray(b, dtype="float32")
    return float(a @ b / ((a @ a) ** 0.5 * (b @ b) ** 0.5 + 1e-9))


async def reproduce_proof() -> dict:
    """Reproduce the clause-chunk gain from the stored clause vectors in provision_clause.

    For each large multi-clause section of the Civil Code (89/2012) that the builder split, with no
    re-splitting in memory:

      - the clause vector is the stored provision_clause.clause_embedding (what retrieval serves);
      - the whole-section vector and each clause probe (clause text without the heading) are
        re-encoded with bge-m3;
      - delta = cos(probe, stored clause vector) - cos(probe, whole-section vector).

    A whole-section embedding dilutes each clause's meaning, so a positive delta means the clause
    vector represents its clause better. Asserts a mean delta within tolerance of +0.18 and, as the
    hard gate, above 0.03.
    """
    from pythia.ingest.embed_bge import MAX_CHARS, model

    pool = await get_pool()
    async with pool.acquire() as conn:
        # the builder's candidate condition (long text, at least four markers), largest first
        secs = await conn.fetch(
            f"""SELECT p.id::text AS id, p.text AS text
                  FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
                 WHERE sd.external_id = $1 AND length(p.text) > {_MIN_LEN}
                   AND (SELECT count(*) FROM regexp_matches(p.text, '{_CLAUSE_RE_SQL}', 'g')) >= {_MIN_MARKERS}
                 ORDER BY length(p.text) DESC""",
            _PROOF_ACT,
        )
        # the stored clause rows for those sections (the vectors retrieval serves)
        clause_rows = await conn.fetch(
            """SELECT pc.provision_id::text AS pid, pc.ordinal, pc.clause_text,
                      pc.clause_embedding::text AS emb
                 FROM provision_clause pc
                 JOIN provision p ON p.id = pc.provision_id
                 JOIN source_document sd ON sd.id = p.source_document_id
                WHERE sd.external_id = $1 AND pc.clause_embedding IS NOT NULL
                ORDER BY pc.provision_id, pc.ordinal""",
            _PROOF_ACT,
        )

    by_prov: dict[str, list] = {}
    for r in clause_rows:
        by_prov.setdefault(r["pid"], []).append(r)
    blob_text = {s["id"]: s["text"] for s in secs}

    m = model()
    deltas, n_sections, n_clauses, clause_wins = [], 0, 0, 0
    for pid, clauses in by_prov.items():
        if pid not in blob_text or len(clauses) < 2:
            continue
        n_sections += 1
        # probe: the clause text without the heading line prepended at build time
        probes = [c["clause_text"].split("\n", 1)[-1][:MAX_CHARS] for c in clauses]
        blob_v = m.encode(
            [blob_text[pid][:MAX_CHARS]], normalize_embeddings=True, batch_size=16, show_progress_bar=False
        )[0]
        probe_vs = m.encode(probes, normalize_embeddings=True, batch_size=16, show_progress_bar=False)
        for c, pv in zip(clauses, probe_vs):
            n_clauses += 1
            cos_own = _cos(pv, _parse_vec(c["emb"]))  # stored clause vector
            cos_blob = _cos(pv, blob_v)
            d = cos_own - cos_blob
            deltas.append(d)
            if d > 0:
                clause_wins += 1

    mean_d = st.mean(deltas) if deltas else 0.0
    res = {
        "sections": n_sections,
        "clauses": n_clauses,
        "clause_wins": clause_wins,
        "mean_delta": round(mean_d, 4),
        "median_delta": round(st.median(deltas), 4) if deltas else 0.0,
    }
    print(
        f"reproduce-proof · sections={n_sections} clauses={n_clauses} "
        f"clause_wins={clause_wins}/{n_clauses} mean_Δcos={mean_d:+.4f} "
        f"(expected ≈ +{_PROOF_EXPECTED}, gate > {_PROOF_GATE})",
        flush=True,
    )
    assert deltas, "REPRODUCE FAILED — no clause rows for 89/2012; build+embed the clause index first"
    assert mean_d > _PROOF_GATE, (
        f"REPRODUCE FAILED — mean Δcos {mean_d:+.4f} did not clear the proof gate {_PROOF_GATE}"
    )
    assert abs(mean_d - _PROOF_EXPECTED) <= _PROOF_TOL, (
        f"REPRODUCE FAILED — mean Δcos {mean_d:+.4f} not within ±{_PROOF_TOL} of the proven +{_PROOF_EXPECTED}"
    )
    print("reproduce-proof OK — stored clause vectors reproduce the proven +0.18 dilution win", flush=True)
    return res


# --------------------------------------------------------------------------- (d) idempotency
async def _clause_snapshot(conn) -> dict:
    row = await conn.fetchrow(
        """SELECT count(*) AS n,
                  md5(string_agg(clause_text_hash, '' ORDER BY id)) AS hash
             FROM provision_clause"""
    )
    return {"n": row["n"], "hash": row["hash"]}


async def idempotency() -> dict:
    """Assert that a second build is a no-op.

    Snapshots (count, md5(string_agg(clause_text_hash))), rebuilds, snapshots again and compares.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await ensure_clause_table(conn)
        before = await _clause_snapshot(conn)
        assert before["n"], "IDEMPOTENCY FAILED — provision_clause is empty; build it first"
        await build_clauses(conn)  # the second build
        after = await _clause_snapshot(conn)
    assert after == before, f"IDEMPOTENCY FAILED — second build changed provision_clause: {before} → {after}"
    print(f"idempotency OK — second build is a no-op (rows={before['n']}, hash unchanged)", flush=True)
    return {"before": before, "after": after}


# --------------------------------------------------------------------------- CLI
async def _main() -> None:
    ap = argparse.ArgumentParser(description="Verification harness for the sub-§ clause index (B1a-07).")
    ap.add_argument(
        "--checksum",
        action="store_true",
        help="(a) print provision text_hash md5 + embedding count (run before & after build)",
    )
    ap.add_argument(
        "--emit-baseline",
        metavar="PATH",
        default=None,
        help="(b) record pre-build dense_seed baseline (run with the flag OFF, before build)",
    )
    ap.add_argument(
        "--restore-smoke",
        action="store_true",
        help="(b) assert flag-OFF dense_seed == baseline after --drop (needs --baseline-file)",
    )
    ap.add_argument(
        "--baseline-file",
        metavar="PATH",
        default=None,
        help="baseline JSON written by --emit-baseline (for --restore-smoke)",
    )
    ap.add_argument(
        "--reproduce-proof",
        action="store_true",
        help="(c) reproduce the proven +0.18 from STORED clause vectors (89/2012)",
    )
    ap.add_argument("--idempotency", action="store_true", help="(d) assert a second build_clauses is a no-op")
    a = ap.parse_args()
    if not (a.checksum or a.emit_baseline or a.restore_smoke or a.reproduce_proof or a.idempotency):
        ap.error(
            "nothing to do — pass one of --checksum/--emit-baseline/--restore-smoke/--reproduce-proof/--idempotency"
        )
    try:
        if a.checksum:
            chk = await provision_checksum()
            print(
                f"CHECKSUM provision_text_hash_md5={chk['provision_text_hash_md5']} "
                f"n_provisions={chk['n_provisions']} n_embedded={chk['n_embedded']}",
                flush=True,
            )
        if a.emit_baseline:
            await emit_baseline(a.emit_baseline)
        if a.idempotency:
            await idempotency()
        if a.reproduce_proof:
            await reproduce_proof()
        if a.restore_smoke:
            if not a.baseline_file:
                ap.error("--restore-smoke needs --baseline-file PATH (the --emit-baseline output)")
            await restore_smoke(a.baseline_file)
    except AssertionError as exc:
        print(f"FAIL · {exc}", flush=True)
        await close_pool()
        sys.exit(1)
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
