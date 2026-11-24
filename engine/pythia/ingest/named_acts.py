"""Mine Czech act citations made by name and record them as numbered cross-references.

Czech statutes cite other acts by name ("podle Ústavy", "ve smyslu občanského zákoníku", "zákoník
práce") far more often than as "č. N/YYYY Sb.", and the numbered miner (`esbirka._mine_xrefs` /
`_EXT_ACT`) records only the numbered form, so name-only citations stay unlinked. This module
detects a bounded set of act names and stores each hit as an external cross_reference in the
numbered shape, which `resolve_xrefs` and `materialize_layer_a` then handle like any other citation.

Usage, from engine/ (idempotent and reversible; every row carries detected_by='named-act'):

    dry-run :  .venv/bin/python -m pythia.ingest.named_acts --dry-run
    apply   :  .venv/bin/python -m pythia.ingest.named_acts            (writes rows; then run resolve_xrefs)
    undo    :  .venv/bin/python -m pythia.ingest.named_acts --unmine   (deletes only named-act rows)
"""

from __future__ import annotations

import argparse
import asyncio
import re
from dataclasses import dataclass

from pythia.db import close_pool, get_pool

DETECTED_BY = "named-act"  # tags every row this module writes, for idempotency and undo
CONF_NAMED = 0.55  # same coarse prior as the numbered _EXT_ACT miner


# ---------------------------------------------------------------------------
# Name-to-act map: a bounded, high-confidence list of in-corpus acts.
# ---------------------------------------------------------------------------
# Each entry pairs a compiled, inflection-aware pattern for the act's Czech colloquial or short
# name with the act's corpus external_id. Patterns are anchored on distinctive stems rather than
# fuzzy matching and are written to be unambiguous for a current act; see each entry's ambiguity
# note.
#
# The map is deliberately conservative, because a wrong name-to-act mapping invents a false legal
# citation, which is worse than a missing one. It lists only current, unambiguous names, and
# historical-name traps are handled by surface form: "trestní zákoník" (40/2009) requires the -ík
# ending so it never fires on the repealed "trestní zákon" (140/1961); "občanský zákoník" maps to
# the current 89/2012 (the repealed 40/1964 shared the name, is not in the corpus, and an explicit
# "č. 40/1964 Sb." citation is caught by the numbered miner); "správní řád" (500/2004) is guarded
# against the longer "soudní řád správní" (150/2002) so the two never cross-fire.
#
# `external_id` is "<number>/<year>" exactly as stored in source_document.external_id, and the
# synthesized reference ("zákona č. <id> Sb.") is the string `resolve_xrefs.parse_target()`
# already parses for numbered citations.
@dataclass(frozen=True)
class NamedAct:
    pattern: re.Pattern[str]
    external_id: str
    title: str
    confidence: str  # 'high' or 'medium'; documentation only (the stored value is CONF_NAMED)
    ambiguity_note: str


_I = re.IGNORECASE

# Czech declines these names. Each pattern admits the common case endings of its anchor stem
# (-a, -u, -ě, -em, -ík, -íku and so on) but stays tight enough to avoid spurious substring hits.
# The module tests check every pattern against its historical-name trap.
_NAMED_ACTS: tuple[NamedAct, ...] = (
    # ---- constitutional acts ----
    NamedAct(
        # Case-sensitive (no _I): the capitalized proper noun 'Ústava/Ústavy/Ústavě/Ústavou/Ústavu' is
        # the Constitution (1/1993). The word boundary after the noun ending excludes the adjective
        # 'ústavní'; the required capital Ú excludes the common noun 'ústav' (institute).
        re.compile(r"\bÚstav(?:a|y|ě|ou|u)\b"),
        "1/1993",
        "Ústavní zákon č. 1/1993 Sb., Ústava České republiky",
        "high",
        "Constitution 1/1993 (ingested 2026-06-19; the stale inventory had it absent, hence the original "
        "exclusion). Case-sensitive capitalized-noun pattern separates it from lowercase 'ústav' (institute) "
        "and the adjective 'ústavní'. Pairs with the Listina (2/1993) entry just below.",
    ),
    NamedAct(
        # Full name only: 'Listin(a/y/ě/ou/u) základních práv a svobod' is the Charter of Fundamental
        # Rights and Freedoms (2/1993). The bare 'Listiny' / 'Listinu' is not used: a precision
        # spot-check found it dominated by the common noun 'listina' (deed, instrument) capitalized at
        # the start of a numbered paragraph ('(1) Listiny ukládané…'), about 50% false positives. The
        # trailing 'a svobod' also rules out the EU Charter ('Listiny základních práv Evropské unie').
        # The short 'čl. N Listiny' form appears mostly in CASE_LAW provisions, which this miner
        # excludes anyway, so requiring the full name costs little recall.
        re.compile(r"\bListin(?:a|y|ě|ou|u)\s+základních\s+práv\s+a\s+svobod", _I),
        "2/1993",
        "Usnesení č. 2/1993 Sb., Listina základních práv a svobod",
        "high",
        "Charter 2/1993 (ingested 2026-06-21; earlier excluded because e-Sbírka returned an empty fragment "
        "until the /dokument/prilohy/ annex-path walker fix). Full-name pattern is ~100% precise and excludes "
        "both the common noun 'listina' (deed) and the EU Charter. Pairs with the Ústava entry above.",
    ),
    # ---- the great codes (codexes) ----
    NamedAct(
        re.compile(r"\bobčansk(?:ého|ý|ém|ému|ým)\s+zákoník\w*", _I),
        "89/2012",
        "Zákon č. 89/2012 Sb., občanský zákoník",
        "high",
        "občanský zákoník historically also = the REPEALED 40/1964; map to the CURRENT 89/2012 only. "
        "An explicit 'č. 40/1964 Sb.' citation is caught by the numbered miner first (40/1964 not in corpus).",
    ),
    NamedAct(
        # ZOK (Business Corporations Act); a different phrase from the repealed 'obchodní zákoník'
        # (513/1991), so the two cannot collide.
        re.compile(r"\bzákon(?:a|u|ě|em)?\s+o\s+obchodních\s+korporacích", _I),
        "90/2012",
        "Zákon č. 90/2012 Sb., o obchodních korporacích (ZOK)",
        "high",
        "Do NOT confuse with the repealed 'obchodní zákoník' (513/1991, not in corpus) — that is a "
        "different phrase ('obchodní zákoník', not 'o obchodních korporacích') and never matches here.",
    ),
    NamedAct(
        # Requires the -ík stem so it never fires on the repealed 'trestní zákon' (140/1961).
        re.compile(r"\btrestní(?:ho|m|mu|ho)?\s+zákoník\w*", _I),
        "40/2009",
        "Zákon č. 40/2009 Sb., trestní zákoník",
        "high",
        "trestní ZÁKONÍK (-ík) = current 40/2009. The bare 'trestní zákon' (no -ík) is the REPEALED "
        "140/1961 (not in corpus) — the required -ík stem keeps the two apart.",
    ),
    NamedAct(
        re.compile(r"\btrestní(?:ho|m|mu)?\s+řád\w*", _I),
        "141/1961",
        "Zákon č. 141/1961 Sb., trestní řád",
        "high",
        "trestní řád = 141/1961, still the CURRENT criminal-procedure code (old act, not repealed).",
    ),
    NamedAct(
        re.compile(r"\bobčansk(?:ého|ý|ém|ému|ým)\s+soudní(?:ho|m|mu)?\s+řád\w*", _I),
        "99/1963",
        "Zákon č. 99/1963 Sb., občanský soudní řád (o.s.ř.)",
        "high",
        "občanský SOUDNÍ řád (o.s.ř.) = 99/1963 — note the 'soudní', which distinguishes it from the "
        "'občanský zákoník' (89/2012). The pattern requires the full three-word stem.",
    ),
    NamedAct(
        re.compile(r"\bzákoník\w*\s+práce", _I),
        "262/2006",
        "Zákon č. 262/2006 Sb., zákoník práce",
        "high",
        "zákoník práce = current 262/2006 (the predecessor 65/1965 is long repealed, not in corpus).",
    ),
    NamedAct(
        re.compile(r"\bdaňov(?:ého|ý|ém|ému|ým)\s+řád\w*", _I),
        "280/2009",
        "Zákon č. 280/2009 Sb., daňový řád",
        "high",
        "daňový řád = current 280/2009 (predecessor 337/1992 'o správě daní a poplatků' had a different "
        "name and is not in corpus) — unambiguous.",
    ),
    NamedAct(
        # 'správní řád' (500/2004) is guarded against the longer 'soudní řád správní' (150/2002): the
        # negative lookbehinds block the 'správní(ho)' inside 'soudní(ho) řád(u) správní(ho)'.
        re.compile(
            r"(?<!soudního řádu )(?<!soudní řád )(?<!soudním řádem )"
            r"\bsprávní(?:ho|m|mu)?\s+řád\w*",
            _I,
        ),
        "500/2004",
        "Zákon č. 500/2004 Sb., správní řád",
        "high",
        "správní řád = current 500/2004 (predecessor 71/1967 repealed, not in corpus). Guarded so the "
        "'správní' inside 'soudní řád správní' (150/2002) does NOT also trigger this — see soudní řád správní.",
    ),
    NamedAct(
        re.compile(r"\bsoudní(?:ho|m|mu)?\s+řád\w*\s+správní\w*", _I),
        "150/2002",
        "Zákon č. 150/2002 Sb., soudní řád správní (s.ř.s.)",
        "high",
        "soudní řád správní (s.ř.s.) = 150/2002 — distinct from 'správní řád' (500/2004). Matched as the "
        "full three-word phrase; longest-match-first ordering ensures it wins over bare 'správní řád'.",
    ),
    NamedAct(
        re.compile(r"\binsolvenční(?:ho|m|mu)?\s+zákon\w*", _I),
        "182/2006",
        "Zákon č. 182/2006 Sb., insolvenční zákon (o úpadku a způsobech jeho řešení)",
        "high",
        "insolvenční zákon = 182/2006 — single current act of that colloquial name.",
    ),
    NamedAct(
        re.compile(r"\bexekuční(?:ho|m|mu)?\s+řád\w*", _I),
        "120/2001",
        "Zákon č. 120/2001 Sb., exekuční řád (o soudních exekutorech a exekuční činnosti)",
        "high",
        "exekuční řád = 120/2001 — current and unambiguous.",
    ),
    # ---- finance and tax: acts whose short name is the 'zákon o <X>' form of their title ----
    # The phrase is the act's own subject line, so a match is high-confidence; the 'zákon o' lead-in
    # is still required so bare topical mentions of <X> do not match.
    NamedAct(
        re.compile(r"\bzákon(?:a|u|ě|em)?\s+o\s+bankách", _I),
        "21/1992",
        "Zákon č. 21/1992 Sb., o bankách",
        "high",
        "zákon o bankách = 21/1992 — unambiguous.",
    ),
    NamedAct(
        re.compile(r"\bzákon(?:a|u|ě|em)?\s+o\s+(?:České\s+národní\s+bance|ČNB)", _I),
        "6/1993",
        "Zákon č. 6/1993 Sb., o České národní bance",
        "high",
        "zákon o ČNB / o České národní bance = 6/1993 — unambiguous.",
    ),
    NamedAct(
        re.compile(r"\bzákon(?:a|u|ě|em)?\s+o\s+podnikání\s+na\s+kapitálovém\s+trhu", _I),
        "256/2004",
        "Zákon č. 256/2004 Sb., o podnikání na kapitálovém trhu (ZPKT)",
        "high",
        "ZPKT — zákon o podnikání na kapitálovém trhu = 256/2004.",
    ),
    NamedAct(
        re.compile(r"\bzákon(?:a|u|ě|em)?\s+o\s+investičních\s+společnostech\s+a\s+investičních\s+fondech", _I),
        "240/2013",
        "Zákon č. 240/2013 Sb., o investičních společnostech a investičních fondech (ZISIF)",
        "high",
        "ZISIF — zákon o investičních společnostech a investičních fondech = 240/2013.",
    ),
    NamedAct(
        re.compile(r"\bzákon(?:a|u|ě|em)?\s+o\s+pojišťovnictví", _I),
        "277/2009",
        "Zákon č. 277/2009 Sb., o pojišťovnictví",
        "high",
        "zákon o pojišťovnictví = 277/2009 (predecessor 363/1999 repealed, not in corpus).",
    ),
    NamedAct(
        re.compile(r"\bzákon(?:a|u|ě|em)?\s+o\s+platebním\s+styku", _I),
        "370/2017",
        "Zákon č. 370/2017 Sb., o platebním styku",
        "high",
        "zákon o platebním styku = CURRENT 370/2017; the predecessor 284/2009 shares the name and IS in "
        "corpus, but a bare-name citation in current consolidated texts means the in-force act → 370/2017. "
        "MEDIUM-confidence edge: see confidence field. Numbered '284/2009' cites are caught separately.",
    ),
    NamedAct(
        re.compile(r"\bzákon(?:a|u|ě|em)?\s+o\s+účetnictví", _I),
        "563/1991",
        "Zákon č. 563/1991 Sb., o účetnictví",
        "high",
        "zákon o účetnictví = 563/1991 — unambiguous.",
    ),
    NamedAct(
        re.compile(r"\bzákon(?:a|u|ě|em)?\s+o\s+dani\s+z\s+přidané\s+hodnoty", _I),
        "235/2004",
        "Zákon č. 235/2004 Sb., o dani z přidané hodnoty",
        "high",
        "zákon o DPH / o dani z přidané hodnoty = current 235/2004 (predecessor 588/1992 repealed, not in corpus).",
    ),
    NamedAct(
        re.compile(r"\bzákon(?:a|u|ě|em)?\s+o\s+daních\s+z\s+příjmů", _I),
        "586/1992",
        "Zákon č. 586/1992 Sb., o daních z příjmů",
        "high",
        "zákon o daních z příjmů = 586/1992 — unambiguous (note plural 'daních', distinct from 'dani z příjmů').",
    ),
    NamedAct(
        re.compile(r"\bzákon(?:a|u|ě|em)?\s+o\s+spotřebních\s+daních", _I),
        "353/2003",
        "Zákon č. 353/2003 Sb., o spotřebních daních",
        "high",
        "zákon o spotřebních daních = current 353/2003 (predecessor 587/1992 repealed, not in corpus).",
    ),
    NamedAct(
        re.compile(r"\bzákon(?:a|u|ě|em)?\s+o\s+zadávání\s+veřejných\s+zakázek", _I),
        "134/2016",
        "Zákon č. 134/2016 Sb., o zadávání veřejných zakázek (ZZVZ)",
        "high",
        "ZZVZ — zákon o zadávání veřejných zakázek = current 134/2016 (predecessor 137/2006 'o veřejných "
        "zakázkách' had a DIFFERENT name and is not in corpus) — unambiguous.",
    ),
    NamedAct(
        re.compile(r"\bzákon(?:a|u|ě|em)?\s+o\s+zaměstnanosti", _I),
        "435/2004",
        "Zákon č. 435/2004 Sb., o zaměstnanosti",
        "high",
        "zákon o zaměstnanosti = current 435/2004 (predecessor 1/1991 repealed, not in corpus).",
    ),
    NamedAct(
        re.compile(r"\bzákon(?:a|u|ě|em)?\s+o\s+přeměnách(?:\s+obchodních\s+společností\s+a\s+družstev)?", _I),
        "125/2008",
        "Zákon č. 125/2008 Sb., o přeměnách obchodních společností a družstev",
        "high",
        "zákon o přeměnách = 125/2008 — unambiguous.",
    ),
    NamedAct(
        re.compile(r"\bzákon(?:a|u|ě|em)?\s+o\s+zvláštních\s+řízeních\s+soudních", _I),
        "292/2013",
        "Zákon č. 292/2013 Sb., o zvláštních řízeních soudních (ZŘS)",
        "high",
        "ZŘS — zákon o zvláštních řízeních soudních = 292/2013 — unambiguous.",
    ),
    NamedAct(
        re.compile(r"\bzákon(?:a|u|ě|em)?\s+o\s+mezinárodním\s+právu\s+soukromém", _I),
        "91/2012",
        "Zákon č. 91/2012 Sb., o mezinárodním právu soukromém (ZMPS)",
        "high",
        "ZMPS — zákon o mezinárodním právu soukromém = current 91/2012 (predecessor 97/1963 repealed, not in corpus).",
    ),
)

# External ids of every mapped act. The backfill checks which of them are present in the corpus
# before writing any row.
NAMED_ACT_EXTERNAL_IDS: frozenset[str] = frozenset(a.external_id for a in _NAMED_ACTS)


def canonical_ref(external_id: str) -> str:
    """Return the numbered-citation string a named hit is recorded as.

    This is the shape the numbered miner writes and `resolve_xrefs.parse_target()` parses, so no new
    resolver branch is needed: "zákona č. 89/2012 Sb." resolves to 89/2012, then `resolve_xrefs`
    links it to the § / Čl. named nearby (see ``_nearest_section``) or else to the act anchor.
    """
    return f"zákona č. {external_id} Sb."


# Finds the closest § / Čl. just before a matched name (same surface forms as
# resolve_xrefs.find_article), so a citation like "§ 2079 občanského zákoníku" resolves to that
# section instead of falling back to the act anchor.
_SEC_BEFORE = re.compile(r"(?:§|Čl\.|Článek)\s*(\d+[a-z]?)", _I)


def _nearest_section(text: str, name_start: int) -> str | None:
    window = text[max(0, name_start - 70) : name_start]
    found = list(_SEC_BEFORE.finditer(window))
    return found[-1].group(1) if found else None


@dataclass
class NamedHit:
    external_id: str
    to_external_ref: str
    section: str | None  # the nearest § / Čl. number, for article-precise resolution


def mine_named_acts(text: str) -> list[NamedHit]:
    """Scan one provision's text for mapped act names.

    Returns at most one hit per mapped act, each with the nearest preceding § / Čl. if any. This
    is a pure function: it does not touch the database and does not drop self-citations, which
    the caller handles because only it knows the citing provision's own act.
    """
    hits: dict[str, NamedHit] = {}
    for na in _NAMED_ACTS:
        m = na.pattern.search(text or "")
        if not m:
            continue
        if na.external_id in hits:  # dedup: one row per act per provision
            continue
        hits[na.external_id] = NamedHit(
            external_id=na.external_id,
            to_external_ref=canonical_ref(na.external_id),
            section=_nearest_section(text, m.start()),
        )
    return list(hits.values())


# ---------------------------------------------------------------------------
# Backfill over existing corpus provisions.
# ---------------------------------------------------------------------------
async def backfill_named_acts(*, dry_run: bool = False) -> dict:
    """Scan every stored provision for named-act citations and record the missing rows.

    Idempotent: a (from_provision, mapped act) pair this miner already recorded is skipped.
    Reversible: `unmine()` deletes exactly the rows written here (detected_by='named-act').

    A hit is dropped when the mapped act is the citing provision's own act (an intra-act
    reference), when the mapped act is not in the corpus (no synthetic reference to an act we do
    not hold), or when the provision already cites that act in numbered form.
    """
    pool = await get_pool()
    stats = {
        "provisions_scanned": 0,
        "hits": 0,
        "written": 0,
        "skipped_self_act": 0,
        "skipped_absent_target": 0,
        "skipped_already_present": 0,
        "skipped_existing_numbered": 0,
    }
    async with pool.acquire() as conn:
        present_ids = {
            r["external_id"]
            for r in await conn.fetch(
                "SELECT external_id FROM source_document WHERE external_id = ANY($1::text[])",
                list(NAMED_ACT_EXTERNAL_IDS),
            )
        }
        # Provision text plus the act it belongs to (its source_document.external_id).
        rows = await conn.fetch(
            """SELECT p.id AS pid, p.text, sd.external_id AS own_act
               FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
               WHERE p.text IS NOT NULL
                 -- EXCLUDE case law: a ruling's statute cites are the case-law linker's job
                 -- (interprets/applies). Mining them here as generic 'references' double-counts
                 -- (the N7 invariant forbids a ruling cite being both an applies-edge AND a references-edge).
                 AND sd.legal_hierarchy IS DISTINCT FROM 'CASE_LAW'"""
        )
        stats["provisions_scanned"] = len(rows)

        # Existing named-act rows (idempotency) keyed by (from_provision, to_external_ref).
        existing = {
            (r["from_provision"], r["to_external_ref"])
            for r in await conn.fetch(
                "SELECT from_provision, to_external_ref FROM cross_reference WHERE detected_by = $1", DETECTED_BY
            )
        }
        # Existing numbered external rows: if the numbered miner already recorded a citation of
        # the same act from the same provision, do not duplicate it by name.
        numbered = {
            (r["from_provision"], r["to_external_ref"])
            for r in await conn.fetch(
                """SELECT from_provision, to_external_ref FROM cross_reference
                   WHERE ref_type = 'external' AND detected_by <> $1 AND to_external_ref IS NOT NULL""",
                DETECTED_BY,
            )
        }
        # Per provision, the acts it already cites in numbered form (N/YYYY parsed from the ref).
        num_by_prov: dict = {}
        _num = re.compile(r"(\d+/\d+)\s*Sb\.", _I)
        for fp, ref in numbered:
            mm = _num.search(ref or "")
            if mm:
                num_by_prov.setdefault(fp, set()).add(mm.group(1))

        to_write: list[tuple] = []
        for r in rows:
            for hit in mine_named_acts(r["text"]):
                stats["hits"] += 1
                if hit.external_id == r["own_act"]:
                    stats["skipped_self_act"] += 1
                    continue
                if hit.external_id not in present_ids:
                    stats["skipped_absent_target"] += 1
                    continue
                if (r["pid"], hit.to_external_ref) in existing:
                    stats["skipped_already_present"] += 1
                    continue
                if hit.external_id in num_by_prov.get(r["pid"], ()):  # numbered cite already exists
                    stats["skipped_existing_numbered"] += 1
                    continue
                import json as _json

                meta = _json.dumps({"named_art": hit.section}) if hit.section else "{}"
                to_write.append((r["pid"], hit.to_external_ref, meta))

        stats["written"] = len(to_write)
        if not dry_run and to_write:
            await conn.executemany(
                """INSERT INTO cross_reference
                       (from_provision, to_provision, to_external_ref, ref_type, detected_by,
                        confidence, detected_at, metadata)
                   VALUES ($1, NULL, $2, 'external', $3, $4, now(), $5::jsonb)""",
                [(pid, ref, DETECTED_BY, CONF_NAMED, meta) for (pid, ref, meta) in to_write],
            )
    return stats


async def unmine() -> int:
    """Delete every row this miner wrote (detected_by='named-act') and return the count."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            """WITH d AS (DELETE FROM cross_reference WHERE detected_by = $1 RETURNING 1)
               SELECT count(*) FROM d""",
            DETECTED_BY,
        )
    return n


def _main() -> None:
    ap = argparse.ArgumentParser(description="Mine Czech by-name act citations into cross_reference rows.")
    ap.add_argument("--dry-run", action="store_true", help="report what would be written, write nothing")
    ap.add_argument("--unmine", action="store_true", help="delete all rows written by this miner")
    a = ap.parse_args()

    async def run():
        if a.unmine:
            print(f"unmined {await unmine()} named-act rows")
        else:
            s = await backfill_named_acts(dry_run=a.dry_run)
            tag = "DRY-RUN (nothing written)" if a.dry_run else "APPLIED"
            print(f"=== named-act citation mining — {tag} ===")
            print(f"  provisions scanned            : {s['provisions_scanned']:,}")
            print(f"  raw name hits                 : {s['hits']:,}")
            print(f"  rows written                  : {s['written']:,}")
            print(f"  skipped (self / own act)      : {s['skipped_self_act']:,}")
            print(f"  skipped (target not in corpus): {s['skipped_absent_target']:,}")
            print(f"  skipped (already mined)       : {s['skipped_already_present']:,}")
            print(f"  skipped (numbered cite exists): {s['skipped_existing_numbered']:,}")
            if not a.dry_run:
                print("  NEXT: python -m pythia.ingest.resolve_xrefs   (links these to provisions + re-materializes)")
        await close_pool()

    asyncio.run(run())


if __name__ == "__main__":
    _main()
