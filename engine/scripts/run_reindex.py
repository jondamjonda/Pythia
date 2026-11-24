"""Rebuild all derived data without re-fetching any act.

Runs `pythia.ingest.refresh._reindex()`: embed provisions that have no embedding yet, resolve
internal and external citations, materialise the graph, rebuild the graph-intelligence layer
(centrality, communities) and run the integrity checks.

Use it after a bulk ingest, so the new provisions get embeddings and citations that were dangling
(by act name, or to EU acts) resolve to their targets. This is only the final reindex step; the
full rebuild order is in scripts/rebuild_corpus.py (see docs/REBUILD.md).

Run (from engine/):  .venv/bin/python scripts/run_reindex.py
Exits 0 when the integrity checks pass, 2 otherwise.
"""

import asyncio
import json
import pathlib
import sys
import time

# This script lives in scripts/; put engine/ on sys.path so `pythia` imports from a plain checkout.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pythia.ingest.refresh import _reindex  # noqa: E402


async def main() -> None:
    t0 = time.monotonic()
    print("[reindex] starting…", flush=True)
    res = await _reindex()
    dt = time.monotonic() - t0
    print(f"[reindex] DONE in {dt:.0f}s", flush=True)
    print("[reindex] result: " + json.dumps(res, ensure_ascii=False, default=str), flush=True)
    sys.exit(0 if res.get("invariants_ok") else 2)


asyncio.run(main())
