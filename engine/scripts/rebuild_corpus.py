"""Rebuild the corpus from source: the single corpus-rebuild driver.

Reads corpus/corpus_manifest.json (the committed list of acts), re-ingests the e-Sbírka and EUR-Lex
layers, then runs the normalisation and reindex steps in the order they depend on each other.

Read docs/REBUILD.md first. The primary recovery path is restoring the latest database dump, not
running this script. This is a secondary rebuild and not hermetic: it needs network access to
e-Sbírka and EUR-Lex / CELLAR, and the optional definition layer (enrich_definitions, not run here)
comes from a non-deterministic local LLM, so only the dump preserves those rows exactly.

The order matters; do not change it. The first `_reindex()` fails the case-law integrity check
(`n5_check`) until provision_versioning has created version rows:

    ingest, classify_taxonomy --apply, provision_versioning --apply, cascade_provision_tier --apply,
    _reindex() pass 1 (n5_ok=false is expected), _reindex() pass 2 (checks turn green),
    then the integrity checks d1_check, n5_check and n6_check as a hard gate

Refuses to run unless the configured database URL (PYTHIA_DATABASE_URL, which defaults to the local
pythia database) targets a pythia database. Try it on a disposable database first.

Run (from engine/):
    python scripts/rebuild_corpus.py                    # full rebuild
    python scripts/rebuild_corpus.py --ingest-only      # step 1 only
    python scripts/rebuild_corpus.py --normalise-only   # skip ingest, run the chain on existing data
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import subprocess
import sys

# This script lives in scripts/; put engine/ on sys.path so `pythia` imports from a plain checkout.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pythia.config import get_settings  # noqa: E402
from pythia.ingest.esbirka import CzechActSpec, ingest_act  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent.parent  # engine/
MANIFEST = HERE / "corpus" / "corpus_manifest.json"


def _guard_db() -> None:
    """Exit unless the configured database URL targets the pythia database."""
    url = get_settings().database_url or ""
    if "pythia" not in url:
        sys.exit(f"REFUSING: DATABASE_URL does not target the pythia DB (got: {url!r}). Aborting.")


def _run_cli(module: str) -> None:
    """Run a normalisation module with --apply; raise on a non-zero exit."""
    print(f"\n=== {module} --apply ===", flush=True)
    subprocess.run([sys.executable, "-m", module, "--apply"], cwd=HERE, check=True)


async def _ingest(manifest: dict) -> None:
    """Step 1: ingest the acts (idempotent and resumable).

    e-Sbírka acts go through CzechActSpec and ingest_act, skipping acts that already have
    provisions. The EU layer goes through the eurlex registry CLI, which ingests only missing
    EU_FINANCIAL acts.
    """
    esbirka = [a for a in manifest["acts"] if a["connector"] == "esbirka"]
    print(f"STEP 1a — ingest {len(esbirka)} esbirka acts (resumable; per-act errors are logged, not fatal)")
    from pythia.db import get_pool

    pool = await get_pool()
    ok = skip = fail = 0
    for a in esbirka:
        ext = a["external_id"]
        try:
            number, year = ext.split("/")  # external_id is NUMBER/YEAR,
            async with pool.acquire() as c:  # skip acts already ingested
                n = await c.fetchval(
                    "SELECT count(*) FROM provision p JOIN source_document sd ON sd.id=p.source_document_id "
                    "WHERE sd.external_id=$1",
                    ext,
                )
            if n and n > 0:
                skip += 1
                continue
            spec = CzechActSpec(
                int(year),
                int(number),  # but CzechActSpec takes (year, number): note the swap
                a["external_id"],
                a["title"],
                "cz-act",
                "act",
            )
            await ingest_act(spec, store=True)
            ok += 1
        except Exception as exc:  # one missing or renamed act must not abort the batch
            fail += 1
            print(f"  ✗ {ext}: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
    print(f"STEP 1a done — ingested {ok}, skipped(present) {skip}, failed {fail}")
    print("STEP 1b — ingest the EU layer (eurlex EU_FINANCIAL registry, missing-only)")
    subprocess.run([sys.executable, "-m", "pythia.ingest.eurlex", "--apply"], cwd=HERE, check=True)


async def _normalise_and_reindex() -> int:
    """Steps 2 to 7: normalise, reindex twice and run the integrity checks; return the exit code."""
    for module in (
        "pythia.ingest.classify_taxonomy",  # step 2: re-tier constitutional and decree acts
        "pythia.ingest.provision_versioning",  # step 3: must precede the first _reindex
        "pythia.ingest.cascade_provision_tier",
    ):  # step 4: propagate the tier down to provisions
        _run_cli(module)
    from pythia.ingest.refresh import _reindex

    print("\n=== _reindex() PASS 1 (expected n5_ok=false — NORMAL) ===", flush=True)
    await _reindex()
    print("\n=== _reindex() PASS 2 (flips the gate green) ===", flush=True)
    r2 = await _reindex()
    print("\n=== GATE: d1/n5/n6 ===", flush=True)
    from pythia.ingest.d1_check import check as d1
    from pythia.ingest.n5_check import check as n5
    from pythia.ingest.n6_check import check as n6

    res = {
        "d1": (await d1())["ok"],
        "n5": (await n5())["ok"],
        "n6": (await n6())["ok"],
        "reindex2_ok": r2.get("invariants_ok"),
    }
    print("  ", res)
    return 0 if all(res.values()) else 2


async def main() -> None:
    _guard_db()
    args = sys.argv[1:]
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    print(f"corpus_manifest.json: {manifest['counts']}")
    if "--normalise-only" not in args:
        await _ingest(manifest)
    if "--ingest-only" in args:
        print("--ingest-only: stopping before normalisation.")
        return
    code = await _normalise_and_reindex()
    sys.exit(code)


if __name__ == "__main__":
    asyncio.run(main())
