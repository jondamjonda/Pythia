r"""Link EU directives to the Czech acts that transpose them.

Builds candidate (Czech act, directive) pairs from the EU's official record of national implementing
measures, corroborated by the Czech text, into `transposition_map`. A separate review (an adversarial
panel of LLM judges) marks the genuine principal transpositions `verified`, and `materialize.py`
emits the transposes / transposed_by edges (rel_id 7 and 8) from the verified rows, so they survive
its rebuild. Dry run by default, idempotent and reversible (`--down`).

Run (from engine/):
      .venv/bin/python -m pythia.ingest.transposition            # dry run: show candidates
      .venv/bin/python -m pythia.ingest.transposition --apply    # (re)build candidates, keeping verified flags
      .venv/bin/python -m pythia.ingest.transposition --down     # drop the map
  then verify the candidates, mark them verified and run `python -m pythia.graph.materialize`.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import date

import httpx

from pythia.db import close_pool, get_pool

_DDL = """
CREATE TABLE IF NOT EXISTS transposition_map (
    cz_external_id text NOT NULL,
    eu_celex       text NOT NULL,
    has_cue        boolean NOT NULL DEFAULT false,
    has_cites      boolean NOT NULL DEFAULT false,
    has_sparql     boolean NOT NULL DEFAULT false,
    notified       date,
    verified       boolean NOT NULL DEFAULT false,
    note           text,
    provenance     jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (cz_external_id, eu_celex)
);
"""

_CUE = re.compile(r"zapracov\w*", re.IGNORECASE)  # zapracovává / zapracovávají / zapracovat ("incorporates")
_DIR_REF = re.compile(r"\b((?:19|20)\d{2})/(\d{1,4})\b")  # "2011/61" gives (year, number)
_SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"
_CZE = "<http://publications.europa.eu/resource/authority/country/CZE>"
_UA = "pythia-ingest/0.1 (Nexus legal-graph; +https://example.invalid)"


def to_celex(year: str, num: str) -> str:
    """Convert a directive's "YYYY/NN" number to its CELEX id, e.g. ('2011', '61') gives '32011L0061'."""
    return f"3{year}L{int(num):04d}"


def cited_directives(text: str, in_corpus_dirs: set[str]) -> set[str]:
    """Return the in-corpus directive CELEX ids cited anywhere in `text`, footnotes included."""
    return {to_celex(y, n) for y, n in _DIR_REF.findall(text or "") if to_celex(y, n) in in_corpus_dirs}


async def _sparql_mne(client: httpx.AsyncClient, celex: str) -> list[dict]:
    """Fetch CELLAR's Czech national implementing measures for a directive.

    Queries the EU Publications Office SPARQL endpoint for measures linked to the directive through
    `measure_national_implementing_implements_resource_legal` with country CZE. Each record's
    `idlocal` (`resource_legal_id_local`) is the Czech act's id in the same form as our external_id.
    """
    q = (
        f"PREFIX cdm: <http://publications.europa.eu/ontology/cdm#> "
        f"PREFIX xsd: <http://www.w3.org/2001/XMLSchema#> "
        f"SELECT DISTINCT ?idlocal ?datenotif WHERE {{ "
        f'?work cdm:resource_legal_id_celex "{celex}"^^xsd:string . '
        f"?mne cdm:measure_national_implementing_implements_resource_legal ?work ; "
        f"cdm:measure_national_implementing_implemented_by_country {_CZE} . "
        f"OPTIONAL {{ ?mne cdm:resource_legal_id_local ?idlocal . }} "
        f"OPTIONAL {{ ?mne cdm:measure_national_implementing_date_notification ?datenotif . }} }}"
    )
    r = await client.get(
        _SPARQL, params={"query": q}, headers={"Accept": "application/sparql-results+json", "User-Agent": _UA}
    )
    r.raise_for_status()
    out = []
    for b in r.json().get("results", {}).get("bindings", []):
        il = b.get("idlocal", {}).get("value")
        if il:
            out.append({"idlocal": il, "datenotif": (b.get("datenotif", {}) or {}).get("value")})
    return out


async def build(*, apply: bool = False) -> dict:
    """Generate transposition candidates and, with `apply=True`, rebuild `transposition_map`.

    Transposition cannot be derived cleanly from text or date heuristics: Czech acts mention directive
    numbers in footnotes, a base statute gets re-linked to later versions of a directive, and AML or
    consumer acts cite directives incidentally. So candidates come from the official record (high
    recall): CELLAR's national implementing measures for each in-corpus directive. A pair is kept when
    the Czech act is in the corpus and shows some connection: it declares transposition (the
    `zapracováv` cue, "incorporates") or cites the directive's number. Each candidate records its
    corroborating signals. The table is fully rebuilt, carrying over existing `verified` flags and
    notes.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        dirs = {
            r["external_id"]
            for r in await conn.fetch("SELECT external_id FROM source_document WHERE document_type='eu_smernice'")
        }
        cz_acts = {
            r["external_id"]
            for r in await conn.fetch("SELECT external_id FROM source_document WHERE jurisdiction='CZ'")
        }
        # Acts that declare they transpose EU directives, plus directive citations across each act's text.
        cue_acts = {
            r["external_id"]
            for r in await conn.fetch(
                """SELECT DISTINCT sd.external_id FROM provision p JOIN source_document sd ON sd.id=p.source_document_id
                WHERE sd.jurisdiction='CZ' AND p.text ~* 'zapracov'"""
            )
        }
        cites: set[tuple[str, str]] = set()
        for r in await conn.fetch(
            """SELECT sd.external_id ext, string_agg(p.text, ' ') alltext FROM provision p
                     JOIN source_document sd ON sd.id=p.source_document_id
                    WHERE sd.jurisdiction='CZ' GROUP BY sd.external_id"""
        ):
            for celex in cited_directives(r["alltext"], dirs):
                cites.add((r["ext"], celex))

    # Official implementing-measure records (high recall), kept only with at least one corroborating signal.
    mne: dict[tuple[str, str], str | None] = {}
    sparql_ok = False
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60.0, connect=15.0)) as client:
            for celex in sorted(dirs):
                for m in await _sparql_mne(client, celex):
                    if m["idlocal"] in cz_acts:
                        mne[(m["idlocal"], celex)] = m["datenotif"]
        sparql_ok = True
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ SPARQL unavailable ({exc!r}) — candidate generation needs it; aborting build")
        return {"mode": "ABORTED", "sparql_ok": False, "candidates": []}

    cand = []
    for (cz, celex), notif in sorted(mne.items()):
        has_cue, has_cites = cz in cue_acts, (cz, celex) in cites
        if not (has_cue or has_cites):
            continue  # drop records with no textual connection (noise)
        cand.append((cz, celex, has_cue, has_cites, notif))

    if apply:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(_DDL)
            prev = {
                (r["cz_external_id"], r["eu_celex"]): (r["verified"], r["note"])
                for r in await conn.fetch("SELECT cz_external_id, eu_celex, verified, note FROM transposition_map")
            }
            await conn.execute("DELETE FROM transposition_map")
            await conn.executemany(
                """INSERT INTO transposition_map
                       (cz_external_id, eu_celex, has_cue, has_cites, has_sparql, notified, verified, note, provenance)
                   VALUES ($1,$2,$3,$4,true,$5,$6,$7,$8::jsonb)""",
                [
                    (
                        cz,
                        celex,
                        hc,
                        hci,
                        (date.fromisoformat(notif) if notif else None),
                        *prev.get((cz, celex), (False, None)),
                        json.dumps({"signals": [s for s, on in (("cue", hc), ("cites", hci)) if on]}),
                    )
                    for cz, celex, hc, hci, notif in cand
                ],
            )
    return {
        "mode": "APPLIED" if apply else "DRY-RUN (no write)",
        "sparql_ok": sparql_ok,
        "n_mne": len(mne),
        "candidates": cand,
    }


async def _main() -> None:
    if "--down" in sys.argv:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS transposition_map")
        print("⏪ reverted: transposition_map dropped (re-run materialize to clear the edges).")
        await close_pool()
        return
    res = await build(apply=("--apply" in sys.argv))
    print(f"=== N4-B1b transposition linker — {res['mode']} · SPARQL {'OK' if res['sparql_ok'] else 'FAIL'} ===")
    print(
        f"  official MNE pairs (in-corpus)={res.get('n_mne', 0)} → candidates (cue or cites)={len(res['candidates'])}"
    )
    print("  candidate                      cue cites notified")
    for cz, celex, hc, hci, notif in res["candidates"]:
        print(f"    {cz:9s} → {celex:11s}  {'✓' if hc else '·'}   {'✓' if hci else '·'}   {notif or ''}")
    if res["mode"].startswith("DRY"):
        print("\n  (dry-run — re-run with --apply; then verify the candidates and mark `verified` before materialize)")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
