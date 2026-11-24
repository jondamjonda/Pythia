"""Deprecated: use `rebuild_corpus.py`, which reads `corpus/corpus_manifest.json`.

This script used to hard-code a list of 9 financial-regulation acts and was one of two separate
ingest runners that had drifted apart. That list was never the whole corpus: every ingestable act
is listed in the committed `corpus/corpus_manifest.json`, which includes those 9. The act list and
the required rebuild order now live in `rebuild_corpus.py` (see docs/REBUILD.md).

It remains as a thin wrapper so old invocations still work: it forwards to
`rebuild_corpus.py --ingest-only`, passing any arguments along. `rebuild_corpus.py` enforces the
database safety check: it refuses to run unless the configured database URL (PYTHIA_DATABASE_URL)
targets the pythia database.

Run instead (from engine/):
    python scripts/rebuild_corpus.py --ingest-only   # e-Sbírka and EUR-Lex ingest only
    python scripts/rebuild_corpus.py                 # full rebuild
"""

import pathlib
import subprocess
import sys

if __name__ == "__main__":
    print(__doc__)
    print(">> delegating to: python scripts/rebuild_corpus.py --ingest-only\n", flush=True)
    subprocess.run(
        [
            sys.executable,
            str(pathlib.Path(__file__).resolve().parent / "rebuild_corpus.py"),
            "--ingest-only",
            *sys.argv[1:],
        ]
    )
