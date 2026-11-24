"""Clause-level embedding index below the section (§) level, in an additive sibling table.

Large multi-clause provisions embed better clause by clause than as one section. This module
splits them into a reversible sibling table, `provision_clause`, and embeds each clause in the same
bge-m3 space as provisions, without a destructive re-chunk: it never writes `provision` rows or
`provision.embedding_bge`. Retrieval reads the table only when clause_read_enabled is on
(PYTHIA_CLAUSE_READ_ENABLED, default off; consumed by seed.py); otherwise it is inert.

Run (from engine/):
  .venv/bin/python -m pythia.ingest.clause_index --build            # split candidates into clause rows
  .venv/bin/python -m pythia.ingest.clause_index --embed            # fill clause_embedding (resumable)
  .venv/bin/python -m pythia.ingest.clause_index --build --embed    # both, in order
  .venv/bin/python -m pythia.ingest.clause_index --rebuild-acts 89/2012 240/2013   # re-split only those acts
  .venv/bin/python -m pythia.ingest.clause_index --drop             # drop the table (full rollback)
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys

from pythia.db import close_pool, get_pool
from pythia.ingest.embed_bge import MAX_CHARS, _vec, model
from pythia.ingest._resource_signals import (
    FreeDiskExhausted,
    KillSwitchEngaged,
    check_between_batches,
    write_mps_heartbeat,
)

DIM = 1024  # bge-m3 dense; must match provision.embedding_bge so the cosines are comparable.

# Clause marker: the bold paragraph (odst.) or letter (písm.) markers **(1)**, **a)** or **1** in the
# stored markdown. Used to split in Python and, as its SQL twin below, to count markers when selecting
# candidates, so the splitter and the candidate predicate always agree.
CLAUSE_RE = re.compile(r"\*\*\(?[0-9a-z]+\)?\*\*")
# SQL form of the same pattern, for counting markers with `regexp_matches(text, ..., 'g')`.
_CLAUSE_RE_SQL = r"\*\*\(?[0-9a-z]+\)?\*\*"

# Candidate provisions: text longer than 1500 characters with at least four clause markers, about 13%
# of provisions. A re-chunking experiment found that for these, embedding each clause separately beats
# embedding the whole section by a mean of +0.18 cosine; about half of all provisions are atomic
# anyway and would gain nothing.
_MIN_LEN = 1500
_MIN_MARKERS = 4
_MIN_FRAGMENT = 40  # skip trivial clause fragments shorter than 40 characters

_ENC_BATCH = 16  # MPS-safe bge-m3 encode batch, as in embed_bge
_DB_BATCH = 256  # clause rows updated per database round trip, as in embed_bge


async def ensure_clause_table(conn) -> None:
    """Create the sibling table, its unique key and its HNSW vector index if missing (idempotent).

    Plain asyncpg DDL with IF NOT EXISTS, like embed_bge.ensure_column. Uses uuid_generate_v4(), as
    the other ensure-table helpers do, and vector(1024) from the already-enabled pgvector extension.
    The provision table is referenced only as the ON DELETE CASCADE foreign-key target, so
    re-ingesting an act drops its clause rows; --rebuild-acts rebuilds them for just those acts.
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS provision_clause (
            id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
            provision_id uuid NOT NULL REFERENCES provision(id) ON DELETE CASCADE,
            clause_path text NOT NULL,
            clause_label text,
            clause_text text NOT NULL,
            clause_text_hash text,
            clause_embedding vector(%d),
            ordinal int
        )
        """
        % DIM
    )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS provision_clause_pid_path_uq ON provision_clause (provision_id, clause_path)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS provision_clause_embedding_idx ON provision_clause "
        "USING hnsw (clause_embedding vector_cosine_ops)"
    )
    # Partial index so a resumed --embed finds the unembedded backlog quickly.
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS provision_clause_unembedded_idx ON provision_clause (id) "
        "WHERE clause_embedding IS NULL"
    )


def split_clauses(provision_text: str) -> list[tuple[str, str, str, int]]:
    """Split a section's text on its bold clause markers into rows for the sibling table.

    Each clause is prefixed with the section's heading line (its first line), the minimal context a
    clause needs, and that combined text is what gets embedded. Returns an empty list for atomic,
    short or single-marker text. Otherwise returns a list of
    (clause_path, clause_label, clause_text, ordinal) tuples:

      clause_path:  "<ordinal>:<marker>", unique within the provision and the upsert key (the ordinal
                    disambiguates repeated markers).
      clause_label: the cleaned marker, e.g. "(1)" or "a)", for display and debugging.
      clause_text:  "<heading>\\n<clause segment>", the text that is embedded.
      ordinal:      0-based clause index within the provision.
    """
    marks = list(CLAUSE_RE.finditer(provision_text))
    if len(marks) < 2:  # atomic or single-marker text: nothing to split
        return []
    heading = (provision_text.splitlines() or [""])[0].strip()
    out: list[tuple[str, str, str, int]] = []
    ordinal = 0
    for i, m in enumerate(marks):
        start = m.start()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(provision_text)
        seg = provision_text[start:end].strip()
        if len(seg) < _MIN_FRAGMENT:  # skip trivial fragments
            continue
        label = m.group(0).replace("*", "").strip()  # "**(1)**" becomes "(1)"
        clause_text = f"{heading}\n{seg}" if heading else seg
        clause_path = f"{ordinal}:{label}"
        out.append((clause_path, label, clause_text, ordinal))
        ordinal += 1
    return out


# Candidate predicate: long sections with at least four bold clause markers. It takes no parameters,
# so --build and --rebuild-acts can share it.
_CANDIDATE_PREDICATE = ("length(p.text) > %d AND (SELECT count(*) FROM regexp_matches(p.text, '%s', 'g')) >= %d") % (
    _MIN_LEN,
    _CLAUSE_RE_SQL,
    _MIN_MARKERS,
)


async def build_clauses(
    conn, *, candidate_predicate: str = _CANDIDATE_PREDICATE, act_ids: list[str] | None = None
) -> dict:
    """Split every candidate provision into clause rows in provision_clause.

    Writes only provision_clause. Idempotent via ON CONFLICT (provision_id, clause_path) DO UPDATE:
    re-running is a no-op for unchanged text and refreshes changed text in place. clause_embedding
    is left NULL for embed_clauses to fill, and clause_text_hash = md5(clause_text) gives cheap
    change detection.

    Args:
        conn: an asyncpg connection.
        candidate_predicate: the WHERE clause over `provision p`; defaults to the candidate slice.
        act_ids: if given, restrict to these acts' provisions (used by --rebuild-acts).
    """
    await ensure_clause_table(conn)
    sql = (
        "SELECT p.id::text AS id, p.text AS text FROM provision p "
        "JOIN source_document sd ON sd.id = p.source_document_id "
        "WHERE " + candidate_predicate
    )
    params: list = []
    if act_ids:
        sql += " AND sd.external_id = ANY($1::text[])"
        params.append(act_ids)
    rows = await conn.fetch(sql, *params)

    n_prov, n_split, n_clauses = len(rows), 0, 0
    for r in rows:
        clauses = split_clauses(r["text"])
        if not clauses:
            continue
        n_split += 1
        # md5 in SQL so the stored hash matches a checksum recomputed in the database.
        await conn.executemany(
            """
            INSERT INTO provision_clause
                (provision_id, clause_path, clause_label, clause_text, clause_text_hash, ordinal)
            VALUES ($1::uuid, $2, $3, $4, md5($4), $5)
            ON CONFLICT (provision_id, clause_path) DO UPDATE SET
                clause_label = EXCLUDED.clause_label,
                clause_text  = EXCLUDED.clause_text,
                clause_text_hash = EXCLUDED.clause_text_hash,
                ordinal      = EXCLUDED.ordinal,
                -- only re-NULL the vector when the text actually changed, so unchanged re-runs DON'T
                -- discard a valid embedding (keeps --embed work minimal & --build truly idempotent).
                clause_embedding = CASE
                    WHEN provision_clause.clause_text_hash IS DISTINCT FROM EXCLUDED.clause_text_hash
                    THEN NULL ELSE provision_clause.clause_embedding END
            """,
            [(r["id"], cp, cl, ct, od) for (cp, cl, ct, od) in clauses],
        )
        n_clauses += len(clauses)
    res = {"candidates": n_prov, "provisions_split": n_split, "clauses_written": n_clauses}
    print(f"build · candidates={n_prov:,} split={n_split:,} clauses={n_clauses:,}", flush=True)
    return res


async def embed_clauses(conn, *, db_batch: int = _DB_BATCH, enc_batch: int = _ENC_BATCH) -> int:
    """Fill clause_embedding for every clause row that has none, and return the number embedded.

    Resumable and batched, and honours the same cross-process control signals and MPS heartbeat as
    embed_bge.embed_all. Uses the shared bge-m3 model with normalize_embeddings=True so clause
    vectors are cosine-comparable with provision.embedding_bge. Does not touch provision.
    """
    await ensure_clause_table(conn)
    rows = await conn.fetch(
        f"SELECT id::text AS id, left(clause_text, {MAX_CHARS}) AS txt "
        "FROM provision_clause WHERE clause_embedding IS NULL"
    )
    if not rows:
        print("nothing to embed (all clauses already have clause_embedding)", flush=True)
        return 0
    print(f"embedding {len(rows):,} clauses with bge-m3 (dim={DIM}) …", flush=True)
    m = model()
    from pythia.ingest.embed_bge import _device  # the device the singleton bound to (mps/cpu)

    done = 0
    for i in range(0, len(rows), db_batch):
        # Poll the cross-process control signals before encoding: abort cleanly if the kill switch is
        # engaged or free disk fell below the floor. Each batch is its own UPDATE, so progress is kept.
        check_between_batches()
        chunk = rows[i : i + db_batch]
        embs = m.encode(
            [r["txt"] for r in chunk], normalize_embeddings=True, batch_size=enc_batch, show_progress_bar=False
        )
        params = [(r["id"], _vec(e)) for r, e in zip(chunk, embs)]
        await conn.executemany("UPDATE provision_clause SET clause_embedding = $2::vector WHERE id = $1::uuid", params)
        done += len(chunk)
        # Refresh the MPS heartbeat each batch so the app sees the live allocation.
        write_mps_heartbeat(_device)
        print(f"  {done:,}/{len(rows):,}", flush=True)
    return done


async def drop_clause_table(conn) -> None:
    """Drop provision_clause, which is a complete rollback.

    Provision rows and provision.embedding_bge are never modified by this module, so dropping the
    sibling table restores the exact baseline.
    """
    await conn.execute("DROP TABLE IF EXISTS provision_clause")
    print("dropped provision_clause — baseline restored", flush=True)


async def _main() -> None:
    ap = argparse.ArgumentParser(description="Additive, reversible sub-§ clause index (default-OFF read path).")
    ap.add_argument("--build", action="store_true", help="split candidate provisions into clause rows")
    ap.add_argument("--embed", action="store_true", help="fill clause_embedding for NULL-vector rows")
    ap.add_argument(
        "--rebuild-acts",
        nargs="+",
        metavar="ACT_ID",
        default=None,
        help="re-split only these acts' provisions (e.g. 89/2012 240/2013), then build",
    )
    ap.add_argument("--drop", action="store_true", help="DROP TABLE provision_clause (reversibility test)")
    a = ap.parse_args()
    if not (a.build or a.embed or a.rebuild_acts or a.drop):
        ap.error("nothing to do — pass at least one of --build / --embed / --rebuild-acts / --drop")

    pool = await get_pool()
    try:
        if a.drop:
            async with pool.acquire() as conn:
                await drop_clause_table(conn)
            return  # --drop is terminal: nothing else runs after a rollback
        if a.rebuild_acts:
            async with pool.acquire() as conn:
                await build_clauses(conn, act_ids=a.rebuild_acts)
        elif a.build:
            async with pool.acquire() as conn:
                await build_clauses(conn)
        if a.embed:
            async with pool.acquire() as conn:
                n = await embed_clauses(conn)
            print(f"done · embedded {n:,} clauses", flush=True)
    except (KillSwitchEngaged, FreeDiskExhausted) as exc:
        # A control signal aborted the run between batches; work so far is committed per batch.
        print(f"ABORTED · {exc}", flush=True)
        await close_pool()
        sys.exit(2)
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
