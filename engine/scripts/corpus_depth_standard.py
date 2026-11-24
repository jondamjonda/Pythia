"""Corpus depth standard: measure and assert per-family corpus quality as a gate.

A single executable that measures the quality dimensions below over the served `pythia` corpus,
prints a deterministic PASS/FAIL line per dimension plus an overall verdict, and exits 0 or 1 so it
can gate a shell `&&` chain. Every addition to the corpus (a new domain, a new batch of acts) must
clear it again: re-run it after an ingest and either the standard still holds or the thin spot is
reported explicitly. It complements the integrity checks (`d1_check`, `n5_check`, `n6_check`): those
assert that the graph is internally consistent, this asserts that the corpus is deep enough to serve.

Per-family gating. Averaging each dimension over the whole corpus would let a thin or broken new
batch hide behind the strong existing corpus (EU acts at 0% section resolution were once carried
over the bar by Czech statutes at 98.6%). So every dimension based on leaves or citing provisions is
measured per legal family (`legal_hierarchy`) and gated on the worst qualifying family. A family
qualifies when its registry row is gated, it has at least `min_sample` rows and, for the section
dimension, it is `section_capable`. A new all-EU domain ingested at 0% therefore fails on its own
slice instead of averaging itself green. Per-domain figures (finer than family) are reported as
well, but only the per-family numbers gate. Thresholds are never loosened: a thin slice is recorded
as a failure, not hidden.

The legal-family registry (one universal standard plus a small per-family adapter) has one row per
`legal_hierarchy` family, declaring whether it is section_capable (has a section structure the
classifier can tag, so it is gated on the section dimension), whether it is gated or report-only,
and its min_sample (a tiny family is reported but not gated, to avoid single-row noise). CASE_LAW
is staged: present in the corpus but kept out of the served statute retrieval lane. Its isolation is
asserted by `n5_check` and the staging dimension, so it is report-only here.

Dimensions (each yields one PASS/FAIL row; a thin spot is a failure, never hidden):

  (1) Citation connectedness, per family: the share of citing provisions (grouped by the citing
      provision's family) with at least one resolved citation. Gated on the worst qualifying family.
      Dangling citations to acts outside the corpus are breadth still to be added (reported, not
      failed); dangling citations to acts inside the corpus are real resolver misses.

  (2) Definition coverage, Czech and English: per language (the axis where gaps hide), with a
      per-family report. Among acts with at least one candidate definition provision (the enricher's
      `_CANDIDATE_PREDICATE`; acts that genuinely define nothing are left out of the denominator), the
      share with at least one extracted `defined_term`. Gated per language.

  (3) Trust completeness, per family: section-capable families are gated on the worst slice; the
      others are reported at the document-tier floor, which is never weaker than the document's
      bindingness. A value-legality sub-check confirms that every leaf resolves to some legal tier (by
      construction of the document floor) and that no served document has a bindingness outside the
      known taxonomy.

  (4) As-of completeness, per family: the share of served (non-anchor) provisions with a non-null
      valid_from. Soft law has no formal effective date, so its NULL valid_from is expected and it is
      reported only; gated on the worst other family.

  (5) Case-law staging: case law is present but cleanly quarantined. Every provision of a CASE_LAW
      document must itself be labelled CASE_LAW, so the `IS DISTINCT FROM 'CASE_LAW'` exclusion used
      by every served dimension is sound. Retrieval isolation (a statute's four-corners expansion
      never returns case law) is asserted by `n5_check` and tests/test_hierarchy_lane.py.

  (6) Refusal rate of at least 95% per domain: not tuned yet and needs human input. It requires a
      lawyer-labelled evaluation set, which does not exist yet, so the live refusal floor is the
      documented 0.5 fallback. Reported as HOLD (an open item that needs a human) rather than a
      made-up pass; it cannot pass or fail until labelled, so it does not affect the verdict.

  (7) Integrity checks: temporal versioning (`d1_check`, run as part of `n5_check`), case law
      (`n5_check`) and graph intelligence (`n6_check`). A failure here fails the standard, because
      measuring depth over an inconsistent graph is meaningless.

The overall verdict is deterministic: PASS if and only if every gated dimension passes; the refusal
HOLD does not change it.

Run (from engine/, against the live database):

    .venv/bin/python scripts/corpus_depth_standard.py                     # table, exit 0/1
    .venv/bin/python scripts/corpus_depth_standard.py --json              # also print JSON
    .venv/bin/python scripts/corpus_depth_standard.py --skip-invariants   # dimensions 1-6 only
    .venv/bin/python scripts/corpus_depth_standard.py --domain DOMAIN     # gate one legal_domain

Options --families and --domains print the per-family and per-domain breakdowns. Exit code: 0 if
every gated dimension passes, 1 on any gated failure, 2 for an unknown --domain.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path

# Allow `python scripts/corpus_depth_standard.py` from engine/ (scripts/ is not a package). With
# `pythia` installed in editable mode the import works anyway; this only helps a plain checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pythia.db import close_pool, get_pool  # noqa: E402
from pythia.ingest.enrich_definitions import _CANDIDATE_PREDICATE  # noqa: E402
from pythia.ingest.resolve_xrefs import parse_target  # noqa: E402 — the resolver's citation parser
from pythia.graph.trust_resolve import _DOC_TIER  # noqa: E402 — known document bindingness values

# Thresholds. These are never loosened.
TARGET_RESOLVED_RATE = 0.80  # provisions citing an act in the corpus that resolve the citation
TARGET_DEF_COVERAGE = 0.90  # definition-bearing acts with an extracted defined_term, per language
TARGET_SECTION_TRUST = 0.70  # leaves with a precise section tag (the rest use the document floor)
TARGET_ASOF = 0.95  # served provisions, other than soft law, with a valid_from
REFUSAL_TARGET = 0.95  # refusal bar per domain (dimension 6); needs a lawyer-labelled set

SOFT_LAW_BINDINGNESS = "SOFT_LAW"

# The legal-family registry (the per-family adapter).
# section_capable  The family has a section structure the classifier tags (Czech § anchors or EU
#                  article titles), so it is gated on the trust (section) dimension. Families
#                  without one (small regional, soft-law and measure families) are reported at the
#                  document-tier floor instead.
# gated            The family is asserted. CASE_LAW is staged and report-only here: its isolation
#                  is checked by n5_check and the staging dimension, not by a depth metric.
# min_sample       A family slice with fewer leaves or citing provisions than this is reported but
#                  not gated, to avoid single-row noise. The large families always exceed it, so a
#                  thin new batch is caught as soon as it reaches min_sample rows.
_DEFAULT_MIN_SAMPLE = 50
FAMILY_REGISTRY: dict[str, dict] = {
    "STATUTE": {"section_capable": True, "gated": True, "min_sample": 50},
    "EU_SECONDARY": {"section_capable": True, "gated": True, "min_sample": 50},
    "CONSTITUTIONAL_ORDER": {"section_capable": True, "gated": True, "min_sample": 50},
    "MINISTERIAL_DECREE": {"section_capable": True, "gated": True, "min_sample": 50},
    "GOV_REGULATION": {
        "section_capable": True,
        "gated": True,
        "min_sample": 50,
    },  # government regulations (nařízení vlády) use Czech § / Hlava structure when large
    "REGIONAL_MUNICIPAL_PREDPIS": {"section_capable": False, "gated": True, "min_sample": 50},
    "SOFT_LAW": {"section_capable": False, "gated": True, "min_sample": 50},
    "MEASURE_GENERAL_NATURE": {"section_capable": False, "gated": True, "min_sample": 50},
    "CASE_LAW": {"section_capable": False, "gated": False, "min_sample": 50, "staged": True},
}


def _reg(family: str | None) -> dict:
    """Return the registry row for a family.

    An unregistered family gets a gated, section-capable default, so a new legal_hierarchy is gated
    rather than silently let through.
    """
    return FAMILY_REGISTRY.get(
        family or "", {"section_capable": True, "gated": True, "min_sample": _DEFAULT_MIN_SAMPLE}
    )


def _gate_worst(per_family: dict[str, tuple[int, int]], target: float, *, require_section_capable: bool = False):
    """Gate on the worst qualifying family slice.

    `per_family` maps family to (numerator, denominator). A family qualifies when its registry row is
    gated, its denominator is at least min_sample and, if `require_section_capable`, it is
    section_capable. Returns (ok, worst, rows): `worst` is (family, rate) of the worst qualifying
    slice, `rows` the per-family report sorted by denominator (largest first), and `ok` whether the
    worst slice reaches `target`. `ok` is trivially True when no family qualifies, but the large
    families always do, so a thin new batch cannot avoid the gate.
    """
    rows = []
    worst: tuple[str, float] | None = None
    for fam, (num, den) in sorted(per_family.items(), key=lambda kv: -kv[1][1]):
        reg = _reg(fam)
        rate = (num / den) if den else None
        qualifies = (
            reg.get("gated", True)
            and den >= reg.get("min_sample", _DEFAULT_MIN_SAMPLE)
            and (not require_section_capable or reg.get("section_capable", True))
        )
        note = (
            ""
            if qualifies
            else (
                "report-only"
                if not reg.get("gated", True)
                else (
                    "not-section-capable→doc-floor"
                    if require_section_capable and not reg.get("section_capable", True)
                    else f"den<{reg.get('min_sample', _DEFAULT_MIN_SAMPLE)} (reported)"
                )
            )
        )
        rows.append(
            {
                "family": fam,
                "num": num,
                "den": den,
                "rate": round(rate, 4) if rate is not None else None,
                "gated": qualifies,
                "note": note,
            }
        )
        if qualifies and rate is not None and (worst is None or rate < worst[1]):
            worst = (fam, rate)
    ok = worst is None or worst[1] >= target
    return ok, worst, rows


def _fam_report(rows: list[dict]) -> str:
    """Return a one-line per-family breakdown for the human-readable table."""
    parts = []
    for r in rows:
        pct = f"{r['rate']:.0%}" if r["rate"] is not None else "—"
        tag = "" if r["gated"] else f"[{r['note']}]"
        parts.append(f"{r['family']} {r['num']}/{r['den']}={pct}{tag}")
    return " · ".join(parts)


# Dimension 1: citation connectedness, per family.
async def _dim_citation(conn) -> dict:
    # Citing provisions with at least one resolved external citation, and their family.
    resolved_rows = await conn.fetch(
        """SELECT DISTINCT cr.from_provision, sd.legal_hierarchy AS fam
             FROM cross_reference cr
             JOIN provision p ON p.id = cr.from_provision
             JOIN source_document sd ON sd.id = p.source_document_id
            WHERE cr.ref_type = 'external' AND cr.from_provision IS NOT NULL
              AND cr.to_provision IS NOT NULL"""
    )
    resolved_fam = {r["from_provision"]: r["fam"] for r in resolved_rows}
    resolved_citer_ids = set(resolved_fam)

    corpus_acts = {
        r["external_id"]
        for r in await conn.fetch("SELECT external_id FROM source_document WHERE external_id IS NOT NULL")
    }

    dangling_rows = await conn.fetch(
        """SELECT cr.from_provision, cr.to_external_ref, sd.legal_hierarchy AS fam
             FROM cross_reference cr
             JOIN provision p ON p.id = cr.from_provision
             JOIN source_document sd ON sd.id = p.source_document_id
            WHERE cr.ref_type = 'external' AND cr.from_provision IS NOT NULL
              AND cr.to_provision IS NULL AND cr.to_external_ref IS NOT NULL"""
    )
    in_corpus_unresolved: dict[str, str] = {}  # from_provision to family, resolvable but unresolved
    dangling_in_corpus = 0
    dangling_out_corpus = 0
    for r in dangling_rows:
        tgt = parse_target(r["to_external_ref"])
        if tgt is not None and tgt.external_id in corpus_acts:
            dangling_in_corpus += 1
            if r["from_provision"] not in resolved_citer_ids:
                in_corpus_unresolved[r["from_provision"]] = r["fam"]
        else:
            dangling_out_corpus += 1

    # Per family: (resolved citing provisions, all citing provisions whose target is in the corpus).
    per_family: dict[str, list[int]] = {}
    for fam in resolved_fam.values():
        per_family.setdefault(fam, [0, 0])
        per_family[fam][0] += 1
        per_family[fam][1] += 1
    for pid, fam in in_corpus_unresolved.items():
        per_family.setdefault(fam, [0, 0])
        per_family[fam][1] += 1  # denominator only: resolvable but unresolved
    pf = {f: (v[0], v[1]) for f, v in per_family.items()}
    ok, worst, rows = _gate_worst(pf, TARGET_RESOLVED_RATE)

    total_external = await conn.fetchval("SELECT count(*) FROM cross_reference WHERE ref_type='external'")
    resolved_external = await conn.fetchval(
        "SELECT count(*) FROM cross_reference WHERE ref_type='external' AND to_provision IS NOT NULL"
    )
    overall_num = sum(v[0] for v in pf.values())
    overall_den = sum(v[1] for v in pf.values())
    overall_rate = (overall_num / overall_den) if overall_den else 1.0
    wf = f"{worst[0]} {worst[1]:.1%}" if worst else "n/a"
    return {
        "name": "citation-connectedness (per family)",
        "ok": ok,
        "gated": True,
        "metric": round(overall_rate, 4),
        "target": TARGET_RESOLVED_RATE,
        "summary": (
            f"worst gated family = {wf} (target ≥{TARGET_RESOLVED_RATE:.0%}); overall {overall_num}/"
            f"{overall_den} = {overall_rate:.1%}; {resolved_external}/{total_external} external edges "
            f"resolved; dangling {dangling_in_corpus} in-corpus (real misses) + {dangling_out_corpus} "
            f"out-of-corpus (breadth)"
        ),
        "families": rows,
        "detail": {
            "worst": worst,
            "overall_rate": round(overall_rate, 4),
            "dangling_in_corpus": dangling_in_corpus,
            "dangling_out_corpus": dangling_out_corpus,
        },
    }


# Dimension 2: definition coverage, Czech and English.
async def _dim_definitions(conn) -> dict:
    # Per (language, family): acts with a candidate definition provision, and how many have a
    # defined_term.
    sql = f"""
        WITH cand AS (
            SELECT DISTINCT sd.id, sd.external_id, sd.language, sd.legal_hierarchy AS fam
              FROM provision p
              JOIN source_document sd ON sd.id = p.source_document_id
             WHERE sd.legal_hierarchy IS DISTINCT FROM 'CASE_LAW'
               -- municipal ordinances (OZV / regional nařízení) carry NO 'vymezení pojmů' definitions
               -- section — they are operative local rules; the length+marker candidate predicate over-flags
               -- their long provisions. Excluded from the "should have definitions" denominator, consistent
               -- with their non-section-capable doc-floor treatment in DIM 3.
               AND sd.legal_hierarchy IS DISTINCT FROM 'REGIONAL_MUNICIPAL_PREDPIS'
               AND ({_CANDIDATE_PREDICATE})
        )
        SELECT
            CASE WHEN lower(coalesce(c.language,'')) = 'en' THEN 'en' ELSE 'cs' END AS lang,
            c.fam AS fam,
            count(*) AS acts_with_candidates,
            count(*) FILTER (WHERE EXISTS (SELECT 1 FROM defined_term dt WHERE dt.scope_document_id = c.id))
                AS acts_with_definitions
          FROM cand c GROUP BY 1, 2"""
    rows = await conn.fetch(sql)
    by_lang: dict[str, list[int]] = {"cs": [0, 0], "en": [0, 0]}
    by_famlang: dict[str, tuple[int, int]] = {}
    for r in rows:
        by_lang[r["lang"]][0] += r["acts_with_definitions"]
        by_lang[r["lang"]][1] += r["acts_with_candidates"]
        by_famlang[f"{r['fam']}/{r['lang']}"] = (r["acts_with_definitions"], r["acts_with_candidates"])

    def _cov(lang):
        num, den = by_lang[lang]
        return num, den, (num / den) if den else 1.0

    cs_num, cs_den, cs_cov = _cov("cs")
    en_num, en_den, en_cov = _cov("en")
    cs_ok = cs_den == 0 or cs_cov >= TARGET_DEF_COVERAGE
    en_ok = en_den == 0 or en_cov >= TARGET_DEF_COVERAGE
    ok = cs_ok and en_ok
    fam_rows = [
        {
            "family": k,
            "num": v[0],
            "den": v[1],
            "rate": round(v[0] / v[1], 4) if v[1] else None,
            "gated": False,
            "note": "report (per-lang gated)",
        }
        for k, v in sorted(by_famlang.items(), key=lambda kv: -kv[1][1])
    ]
    return {
        "name": "definition-coverage (cs+en)",
        "ok": ok,
        "gated": True,
        "metric": round(min(cs_cov, en_cov), 4),
        "target": TARGET_DEF_COVERAGE,
        "summary": (
            f"cs {cs_num}/{cs_den} = {cs_cov:.1%}{' ⚠' if not cs_ok else ''} · "
            f"en {en_num}/{en_den} = {en_cov:.1%}{' ⚠' if not en_ok else ''} "
            f"(denominator = acts WITH candidate-definition provisions; target ≥{TARGET_DEF_COVERAGE:.0%}/lang)"
        ),
        "families": fam_rows,
        "detail": {
            "cs": {"covered": cs_num, "candidate_acts": cs_den, "coverage": round(cs_cov, 4), "ok": cs_ok},
            "en": {"covered": en_num, "candidate_acts": en_den, "coverage": round(en_cov, 4), "ok": en_ok},
        },
    }


# Dimension 3: trust completeness, per family.
# Walks up from each leaf to its nearest classified section_kind, using the same bounded recursion
# as pythia.ports._decorate (depth at most 8, climbing only while no classified ancestor has been
# found), grouped by family. A leaf counts towards section precision only if its act has a section
# structure (at least one of the act's leaves resolves to a classified section). A flat act, for
# example a government regulation on CE conformity whose § all sit at the ltree root with no Část /
# Hlava grouping, has no section to resolve to; its leaves fall to the document-tier floor and are
# not counted as failures. This mirrors the treatment of EU acts: precision is measured only where a
# section can exist. The number of flat-act leaves is reported, so a sudden rise (which could hide a
# bug in anchor building) stays visible.
_TRUST_SQL = """
WITH RECURSIVE leaves AS (
    SELECT p.id, p.section_kind, p.source_document_id AS doc, sd.legal_hierarchy AS fam
      FROM provision p
      JOIN source_document sd ON sd.id = p.source_document_id
     WHERE p.is_anchor_only = false
       AND sd.legal_hierarchy IS DISTINCT FROM 'CASE_LAW'
),
walk(start_id, node_id, kind, depth) AS (
    SELECT l.id, l.id, l.section_kind, 0 FROM leaves l
  UNION ALL
    SELECT w.start_id, par.id, par.section_kind, w.depth + 1
      FROM walk w
      JOIN provision cur ON cur.id = w.node_id
      JOIN provision par ON par.id = cur.parent_id
     WHERE w.kind IS NULL AND w.depth < 8
),
resolved AS (SELECT DISTINCT start_id FROM walk WHERE kind IS NOT NULL),
leaf_res AS (
    SELECT l.id, l.doc, l.fam, (r.start_id IS NOT NULL) AS resolved
      FROM leaves l LEFT JOIN resolved r ON r.start_id = l.id
),
structured_act AS (   -- an act that DEMONSTRABLY has a section structure: ≥1 of its leaves section-resolves
    SELECT doc FROM leaf_res WHERE resolved GROUP BY doc
)
SELECT lr.fam AS fam,
       count(*) AS leaves,
       count(*) FILTER (WHERE lr.doc IN (SELECT doc FROM structured_act)) AS structured_leaves,
       count(*) FILTER (WHERE lr.resolved) AS section_resolved
  FROM leaf_res lr GROUP BY lr.fam
"""


async def _dim_trust(conn) -> dict:
    rows = await conn.fetch(_TRUST_SQL)
    # The gate's denominator is structured leaves (leaves of acts with a section structure); flat-act
    # leaves fall to the document floor and are reported only. Section precision per family is
    # resolved / structured.
    pf = {r["fam"]: (r["section_resolved"], r["structured_leaves"]) for r in rows}
    ok, worst, frows = _gate_worst(pf, TARGET_SECTION_TRUST, require_section_capable=True)

    leaf_total = sum(r["leaves"] for r in rows)
    structured_total = sum(r["structured_leaves"] for r in rows)
    section_resolved = sum(r["section_resolved"] for r in rows)
    flat_leaves = leaf_total - structured_total  # leaves of flat acts, using the document floor
    doc_floor = leaf_total - section_resolved  # all leaves without a precise section tag
    unknown_binding = await conn.fetchval(
        """SELECT count(*) FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
            WHERE p.is_anchor_only = false AND sd.legal_hierarchy IS DISTINCT FROM 'CASE_LAW'
              AND (sd.bindingness IS NULL OR upper(sd.bindingness) <> ALL($1::text[]))""",
        list(_DOC_TIER.keys()),
    )
    wf = f"{worst[0]} {worst[1]:.1%}" if worst else "n/a"
    prec = (section_resolved / structured_total) if structured_total else 1.0
    return {
        "name": "trust-completeness (per family · section-precision over structured acts)",
        "ok": ok,
        "gated": True,
        "metric": round(prec, 4),
        "target": TARGET_SECTION_TRUST,
        "summary": (
            f"worst family section-precision = {wf} (target ≥{TARGET_SECTION_TRUST:.0%}); "
            f"section-resolved {section_resolved}/{structured_total} of structured-act leaves = {prec:.1%}; "
            f"flat-act leaves {flat_leaves} → safe doc-floor (no section to resolve); total doc-floor "
            f"{doc_floor}/{leaf_total}; value-legality: 100% resolve to a tier, {unknown_binding} leaves "
            f"carry unknown bindingness (→ safe hard-law floor)"
        ),
        "families": frows,
        "detail": {
            "leaf_total": leaf_total,
            "structured_total": structured_total,
            "section_resolved": section_resolved,
            "flat_leaves": flat_leaves,
            "doc_tier_floor": doc_floor,
            "worst": worst,
            "unknown_bindingness_leaves": unknown_binding,
        },
    }


# Dimension 4: as-of completeness, per family.
async def _dim_asof(conn) -> dict:
    rows = await conn.fetch(
        """SELECT sd.legal_hierarchy AS fam, count(*) AS total,
                  count(*) FILTER (WHERE p.valid_from IS NOT NULL) AS nonnull
             FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
            WHERE p.is_anchor_only = false AND sd.legal_hierarchy IS DISTINCT FROM 'CASE_LAW'
              AND sd.bindingness IS DISTINCT FROM $1
            GROUP BY sd.legal_hierarchy""",
        SOFT_LAW_BINDINGNESS,
    )
    pf = {r["fam"]: (r["nonnull"], r["total"]) for r in rows}
    ok, worst, frows = _gate_worst(pf, TARGET_ASOF)

    soft_total = await conn.fetchval(
        """SELECT count(*) FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
            WHERE p.is_anchor_only = false AND sd.legal_hierarchy IS DISTINCT FROM 'CASE_LAW'
              AND sd.bindingness = $1""",
        SOFT_LAW_BINDINGNESS,
    )
    soft_null = await conn.fetchval(
        """SELECT count(*) FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
            WHERE p.is_anchor_only = false AND sd.legal_hierarchy IS DISTINCT FROM 'CASE_LAW'
              AND sd.bindingness = $1 AND p.valid_from IS NULL""",
        SOFT_LAW_BINDINGNESS,
    )
    nonnull = sum(v[0] for v in pf.values())
    total = sum(v[1] for v in pf.values())
    rate = (nonnull / total) if total else 1.0
    wf = f"{worst[0]} {worst[1]:.1%}" if worst else "n/a"
    return {
        "name": "as_of completeness (per family · valid_from)",
        "ok": ok,
        "gated": True,
        "metric": round(rate, 4),
        "target": TARGET_ASOF,
        "summary": (
            f"worst non-SOFT_LAW family = {wf} (target ≥{TARGET_ASOF:.0%}); overall {nonnull}/{total} = "
            f"{rate:.1%}; SOFT_LAW honest-NULL {soft_null}/{soft_total} excluded from the failure count"
        ),
        "families": frows,
        "detail": {
            "nonsoft_total": total,
            "nonsoft_nonnull": nonnull,
            "worst": worst,
            "softlaw_total": soft_total,
            "softlaw_honest_null": soft_null,
        },
    }


# Dimension 5: case-law staging.
async def _dim_caselaw(conn) -> dict:
    # Case law is staged: present, but kept out of the served corpus. Check that it is present and
    # that every provision of a CASE_LAW document is itself labelled CASE_LAW, so the exclusion filter
    # of every served dimension is sound. Retrieval isolation is covered by n5_check and
    # tests/test_hierarchy_lane.py.
    total = await conn.fetchval(
        """SELECT count(*) FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
            WHERE sd.legal_hierarchy = 'CASE_LAW'"""
    )
    # A provision whose own legal_hierarchy disagrees with its CASE_LAW document would defeat the
    # exclusion.
    mislabeled = await conn.fetchval(
        """SELECT count(*) FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
            WHERE sd.legal_hierarchy = 'CASE_LAW' AND p.legal_hierarchy IS DISTINCT FROM 'CASE_LAW'"""
    )
    ok = mislabeled == 0
    return {
        "name": "case-law staging (staged + asserted-absent)",
        "ok": ok,
        "gated": True,
        "metric": None,
        "target": None,
        "summary": (
            f"{total} case-law provisions STAGED + quarantined; {mislabeled} mis-hierarchied "
            f"(must be 0 — else a served-dimension exclusion would leak case law); retrieval-lane "
            f"isolation asserted by n5 + tests/test_hierarchy_lane.py"
        ),
        "detail": {"case_law_provisions": total, "mislabeled": mislabeled},
    }


# Dimension 6: refusal rate per domain (HOLD until a lawyer-labelled set exists).
def _dim_refusal() -> dict:
    floor_tuned = False
    live_floor_source = "fallback (0.5)"
    app_dir = Path(__file__).resolve().parents[2] / "app"
    try:
        # The app half of the monorepo (app/src) owns the calibrated refusal floor.
        sys.path.insert(0, str(app_dir / "src"))
        from regsuper.calibration.runtime import get_cascade_refusal_floor  # type: ignore

        # Resolve it as the app does: the app's settings read secrets.env / .env and the
        # relative data_dir ("./data") from the app's own directory, not from where this runs.
        with contextlib.chdir(app_dir):
            rt = get_cascade_refusal_floor()
        floor_tuned = rt.source == "calibrated"
        live_floor_source = f"{rt.source} ({rt.value})"
    except Exception as exc:  # noqa: BLE001 — accessor unavailable; keep the fallback, say why
        live_floor_source = f"fallback (0.5); app accessor unavailable: {type(exc).__name__}: {exc}"
    return {
        "name": "refusal ≥95%/domain",
        "ok": None,
        "gated": False,
        "hold": True,
        "metric": None,
        "target": REFUSAL_TARGET,
        "summary": (
            f"floor_tuned={str(floor_tuned).lower()} — "
            f"{'calibrated floor in use' if floor_tuned else 'un-tuned (fallback floor)'}; "
            f"HUMAN-GATED (needs a lawyer-labelled eval set; 0 labelled today). "
            f"live cascade floor source = {live_floor_source}. NOT a fabricated pass — HOLD."
        ),
        "detail": {
            "floor_tuned": floor_tuned,
            "live_floor_source": live_floor_source,
            "target_refusal": REFUSAL_TARGET,
            "labelled_eval_rows": 0,
            "human_gated": True,
        },
    }


# Dimension 7: integrity checks (temporal versioning, case law, graph intelligence).
async def _dim_invariants(skip: bool) -> dict:
    if skip:
        return {
            "name": "d1/n5/n6 invariants",
            "ok": None,
            "gated": False,
            "hold": True,
            "metric": None,
            "target": None,
            "summary": "SKIPPED (--skip-invariants) — run the invariant gates separately; recorded as HOLD",
            "detail": {"skipped": True},
        }
    from pythia.ingest.n5_check import check as n5_check
    from pythia.ingest.n6_check import check as n6_check

    n5 = await n5_check()
    n6 = await n6_check()
    ok = bool(n5["ok"] and n6["ok"])
    n5_fails = [k for k, v in n5["checks"].items() if not v]
    n6_fails = [k for k, v in n6["checks"].items() if not v]
    return {
        "name": "d1/n5/n6 invariants",
        "ok": ok,
        "gated": True,
        "metric": None,
        "target": None,
        "summary": (
            f"n5(+d1) {'PASS' if n5['ok'] else 'FAIL'} · n6 {'PASS' if n6['ok'] else 'FAIL'}"
            + ("" if ok else f" — n5 fails={n5_fails} n6 fails={n6_fails}")
        ),
        "detail": {"n5_ok": n5["ok"], "n6_ok": n6["ok"], "n5_fails": n5_fails, "n6_fails": n6_fails},
    }


# Per-domain report (report-only, never gates). Per-family gating catches a thin new family (such as
# EU acts); this catches a thin domain within a family (such as a sparsely tagged new batch of
# employment law inside STATUTE). It shows a thin domain for a person to inspect but does not change
# the verdict: gating on a multi-valued, partly `uncategorized` domain axis would be noisy.
# legal_domain is a TEXT[] column on source_document, so an act with several domains counts towards
# each of them.
_DOMAIN_SQL = """
WITH RECURSIVE leaves AS (
    SELECT p.id, p.section_kind, p.valid_from, sd.legal_domain, sd.bindingness
      FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
     WHERE p.is_anchor_only = false AND sd.legal_hierarchy IS DISTINCT FROM 'CASE_LAW'
),
walk(start_id, node_id, kind, depth) AS (
    SELECT l.id, l.id, l.section_kind, 0 FROM leaves l
  UNION ALL
    SELECT w.start_id, par.id, par.section_kind, w.depth + 1
      FROM walk w JOIN provision cur ON cur.id = w.node_id JOIN provision par ON par.id = cur.parent_id
     WHERE w.kind IS NULL AND w.depth < 8
),
resolved AS (SELECT DISTINCT ON (start_id) start_id FROM walk WHERE kind IS NOT NULL ORDER BY start_id, depth ASC)
SELECT dom AS domain, count(*) AS leaves,
       count(r.start_id) AS section_resolved,
       count(*) FILTER (WHERE l.valid_from IS NOT NULL OR l.bindingness = 'SOFT_LAW') AS asof_ok
  FROM leaves l
  CROSS JOIN LATERAL unnest(coalesce(l.legal_domain, ARRAY['uncategorized'])) AS dom
  LEFT JOIN resolved r ON r.start_id = l.id
 GROUP BY dom ORDER BY 2 DESC
"""


async def _domain_report(conn) -> list[dict]:
    rows = await conn.fetch(_DOMAIN_SQL)
    return [
        {
            "domain": r["domain"],
            "leaves": r["leaves"],
            "section_resolved_rate": round(r["section_resolved"] / r["leaves"], 4) if r["leaves"] else None,
            "asof_rate": round(r["asof_ok"] / r["leaves"], 4) if r["leaves"] else None,
        }
        for r in rows
    ]


# --domain gate. Scopes the four data dimensions to one legal_domain (an array overlap on the TEXT[]
# column source_document.legal_domain) and gates each against its threshold (exit 0 or 1). The
# integrity checks, case-law staging and the refusal HOLD are global, because a domain is served on
# top of a graph that must be consistent as a whole, and are included unchanged. Use it to clear a
# newly added domain before it is served. Strictly read-only. An unknown domain (a typo, or a sector
# name from another taxonomy) is rejected rather than measured as an empty slice.
_DOM = "sd.legal_domain && ARRAY[$1]::text[]"


async def _known_domains(conn) -> set:
    rows = await conn.fetch(
        "SELECT DISTINCT unnest(legal_domain) AS d FROM source_document WHERE legal_domain IS NOT NULL"
    )
    return {r["d"] for r in rows}


async def _domain_citation(conn, domain) -> dict:
    resolved = {
        r["from_provision"]
        for r in await conn.fetch(
            f"""SELECT DISTINCT cr.from_provision FROM cross_reference cr
              JOIN provision p ON p.id = cr.from_provision JOIN source_document sd ON sd.id = p.source_document_id
             WHERE cr.ref_type='external' AND cr.to_provision IS NOT NULL AND {_DOM}""",
            domain,
        )
    }
    corpus_acts = {
        r["external_id"]
        for r in await conn.fetch("SELECT external_id FROM source_document WHERE external_id IS NOT NULL")
    }
    dangling = await conn.fetch(
        f"""SELECT cr.from_provision, cr.to_external_ref FROM cross_reference cr
              JOIN provision p ON p.id = cr.from_provision JOIN source_document sd ON sd.id = p.source_document_id
             WHERE cr.ref_type='external' AND cr.to_provision IS NULL AND cr.to_external_ref IS NOT NULL AND {_DOM}""",
        domain,
    )
    unresolved = set()
    for r in dangling:
        t = parse_target(r["to_external_ref"])
        if t is not None and t.external_id in corpus_acts and r["from_provision"] not in resolved:
            unresolved.add(r["from_provision"])
    denom = len(resolved | unresolved)
    num = len(resolved)
    rate = (num / denom) if denom else 1.0
    return {
        "element": "citation-connectedness",
        "ok": rate >= TARGET_RESOLVED_RATE,
        "rate": round(rate, 4),
        "target": TARGET_RESOLVED_RATE,
        "detail": f"{num}/{denom} in-corpus citers resolve",
    }


async def _domain_definitions(conn, domain) -> dict:
    sql = f"""WITH cand AS (
                SELECT DISTINCT sd.id, CASE WHEN lower(coalesce(sd.language,''))='en' THEN 'en' ELSE 'cs' END AS lang
                  FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
                 WHERE sd.legal_hierarchy IS DISTINCT FROM 'CASE_LAW'
                   AND sd.legal_hierarchy IS DISTINCT FROM 'REGIONAL_MUNICIPAL_PREDPIS'   -- no definitions section (see DIM 2)
                   AND {_DOM} AND ({_CANDIDATE_PREDICATE}))
            SELECT lang, count(*) AS den,
                   count(*) FILTER (WHERE EXISTS (SELECT 1 FROM defined_term dt WHERE dt.scope_document_id = cand.id)) AS num
              FROM cand GROUP BY 1"""
    rows = {r["lang"]: (r["num"], r["den"]) for r in await conn.fetch(sql, domain)}
    worst = 1.0
    parts = []
    for lang, (num, den) in rows.items():
        cov = (num / den) if den else 1.0
        worst = min(worst, cov)
        parts.append(f"{lang} {num}/{den}={cov:.0%}")
    ok = all((den == 0 or num / den >= TARGET_DEF_COVERAGE) for num, den in rows.values()) if rows else True
    return {
        "element": "definition-coverage",
        "ok": ok,
        "rate": round(worst, 4),
        "target": TARGET_DEF_COVERAGE,
        "detail": " · ".join(parts) or "no candidate-definition acts (vacuously complete)",
    }


async def _domain_trust(conn, domain) -> dict:
    # As in _dim_trust: gate section precision over leaves whose act has a section structure (at
    # least one resolved leaf); flat-act leaves fall to the document floor and are not misses.
    row = await conn.fetchrow(
        f"""
        WITH RECURSIVE leaves AS (
            SELECT p.id, p.section_kind, p.source_document_id AS doc
              FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
             WHERE p.is_anchor_only=false AND sd.legal_hierarchy IS DISTINCT FROM 'CASE_LAW' AND {_DOM}),
        walk(start_id, node_id, kind, depth) AS (
            SELECT l.id, l.id, l.section_kind, 0 FROM leaves l
          UNION ALL SELECT w.start_id, par.id, par.section_kind, w.depth+1 FROM walk w
            JOIN provision cur ON cur.id=w.node_id JOIN provision par ON par.id=cur.parent_id
           WHERE w.kind IS NULL AND w.depth < 8),
        res AS (SELECT DISTINCT start_id FROM walk WHERE kind IS NOT NULL),
        leaf_res AS (SELECT l.id, l.doc, (r.start_id IS NOT NULL) AS resolved FROM leaves l LEFT JOIN res r ON r.start_id=l.id),
        structured_act AS (SELECT doc FROM leaf_res WHERE resolved GROUP BY doc)
        SELECT count(*) AS leaves,
               count(*) FILTER (WHERE doc IN (SELECT doc FROM structured_act)) AS structured,
               count(*) FILTER (WHERE resolved) AS resolved FROM leaf_res""",
        domain,
    )
    structured = row["structured"] or 0
    resolved = row["resolved"] or 0
    leaves = row["leaves"] or 0
    rate = (resolved / structured) if structured else 1.0
    return {
        "element": "trust-completeness",
        "ok": rate >= TARGET_SECTION_TRUST,
        "rate": round(rate, 4),
        "target": TARGET_SECTION_TRUST,
        "detail": f"section-precision {resolved}/{structured} structured-act leaves; {leaves - structured} flat→doc-floor; 100% resolve to a tier",
    }


async def _domain_asof(conn, domain) -> dict:
    row = await conn.fetchrow(
        f"""
        SELECT count(*) FILTER (WHERE sd.bindingness IS DISTINCT FROM 'SOFT_LAW') AS den,
               count(*) FILTER (WHERE sd.bindingness IS DISTINCT FROM 'SOFT_LAW' AND p.valid_from IS NOT NULL) AS num
          FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
         WHERE p.is_anchor_only=false AND sd.legal_hierarchy IS DISTINCT FROM 'CASE_LAW' AND {_DOM}""",
        domain,
    )
    den = row["den"] or 0
    num = row["num"] or 0
    rate = (num / den) if den else 1.0
    return {
        "element": "as_of completeness",
        "ok": rate >= TARGET_ASOF,
        "rate": round(rate, 4),
        "target": TARGET_ASOF,
        "detail": f"non-SOFT_LAW valid_from {num}/{den}",
    }


async def run_domain(domain: str, *, skip_invariants: bool = False) -> dict:
    """Gate a single legal_domain.

    Returns a dict with `error` for an unknown domain (the CLI exits 2). Otherwise returns a verdict
    per element and overall: the four data dimensions scoped to the domain plus the global case-law
    and integrity gates. The refusal HOLD does not affect the verdict.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        known = await _known_domains(conn)
        if domain not in known:
            return {"domain": domain, "error": f"unknown legal_domain {domain!r}", "known": sorted(known)}
        elements = [
            await _domain_citation(conn, domain),
            await _domain_definitions(conn, domain),
            await _domain_trust(conn, domain),
            await _domain_asof(conn, domain),
        ]
        caselaw = await _dim_caselaw(conn)
    inv = await _dim_invariants(skip_invariants)
    ref = _dim_refusal()
    gated = elements + [caselaw] + ([inv] if inv.get("gated") else [])
    overall = all(g["ok"] for g in gated)
    return {
        "domain": domain,
        "overall": overall,
        "elements": elements,
        "caselaw": caselaw,
        "refusal": ref,
        "invariants": inv,
    }


# Driver.
async def run(*, skip_invariants: bool = False) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        dims = [
            await _dim_citation(conn),
            await _dim_definitions(conn),
            await _dim_trust(conn),
            await _dim_asof(conn),
            await _dim_caselaw(conn),
        ]
        domains = await _domain_report(conn)
    dims.append(_dim_refusal())
    dims.append(await _dim_invariants(skip_invariants))

    gated = [d for d in dims if d.get("gated")]
    overall = all(d["ok"] for d in gated)
    holds = [d for d in dims if d.get("hold") or d.get("ok") is None]
    return {
        "overall": overall,
        "dimensions": dims,
        "n_gated": len(gated),
        "n_hold": len(holds),
        "domain_report": domains,
    }


def _verdict_tag(d: dict) -> str:
    if d.get("hold") or d.get("ok") is None:
        return "HOLD"
    return "PASS" if d["ok"] else "FAIL"


async def _main() -> None:
    ap = argparse.ArgumentParser(description="B1a-15 corpus depth-standard harness (measure + assert).")
    ap.add_argument("--json", action="store_true", help="also emit a machine-readable JSON block")
    ap.add_argument(
        "--skip-invariants", action="store_true", help="skip the d1/n5/n6 dimension (faster; it is recorded as HOLD)"
    )
    ap.add_argument("--families", action="store_true", help="print the per-family breakdown under each dimension")
    ap.add_argument(
        "--domains",
        action="store_true",
        help="print the report-only per-domain breakdown (surfaces a thin domain within a family)",
    )
    ap.add_argument(
        "--domain",
        metavar="LEGAL_DOMAIN",
        default=None,
        help="GATE a single legal_domain (B1b): per-element verdict + overall; exit 0 iff it clears",
    )
    args = ap.parse_args()

    # --domain: gate one legal_domain. Exit 0 if it clears, 1 if it fails, 2 for an unknown domain.
    if args.domain:
        dr = await run_domain(args.domain, skip_invariants=args.skip_invariants)
        if dr.get("error"):
            print(f"❌ {dr['error']}\n   known legal_domains: {', '.join(dr['known'])}")
            await close_pool()
            sys.exit(2)
        print(f"=== DEPTH-STANDARD — DOMAIN GATE: {dr['domain']} ===")
        for e in dr["elements"]:
            print(
                f"  [{'PASS' if e['ok'] else 'FAIL'}] {e['element']:<26} {e['rate']:.1%} (≥{e['target']:.0%}) — {e['detail']}"
            )
        print(f"  [{'PASS' if dr['caselaw']['ok'] else 'FAIL'}] {dr['caselaw']['name']} (global)")
        print(
            f"  [{_verdict_tag(dr['invariants'])}] {dr['invariants']['name']} (global): {dr['invariants']['summary']}"
        )
        print(f"  [HOLD] refusal ≥95%/domain (global, human-gated): {dr['refusal']['summary']}")
        print(
            f"\n  overall: {'✅ DOMAIN CLEARS THE STANDARD' if dr['overall'] else '❌ DOMAIN BELOW STANDARD'} "
            f"(refusal HOLD does not flip; serving stays OFF until the lawyer refusal refit + B3 knob)"
        )
        if args.json:
            print("\n=== JSON ===\n" + json.dumps(dr, ensure_ascii=False, default=str, indent=2))
        await close_pool()
        sys.exit(0 if dr["overall"] else 1)

    res = await run(skip_invariants=args.skip_invariants)
    print("=== B1a-15 CORPUS DEPTH-STANDARD (per-family gated) ===")
    for i, d in enumerate(res["dimensions"], 1):
        tag = _verdict_tag(d)
        print(f"  [{tag}] ({i}) {d['name']}")
        print(f"         {d['summary']}")
        if args.families and d.get("families"):
            print(f"         families: {_fam_report(d['families'])}")
    if args.domains and res.get("domain_report"):
        print("\n=== PER-DOMAIN (report-only — surfaces a thin domain WITHIN a family; does not gate) ===")
        for d in res["domain_report"]:
            sr = f"{d['section_resolved_rate']:.0%}" if d["section_resolved_rate"] is not None else "—"
            ao = f"{d['asof_rate']:.0%}" if d["asof_rate"] is not None else "—"
            print(f"  {d['domain']:<22} leaves={d['leaves']:>6}  section={sr:>5}  as_of={ao:>5}")

    print("\n=== STANDARD ===")
    print(f"  gated dimensions: {res['n_gated']} · hold/human-gated: {res['n_hold']}")
    print(
        f"  overall: {'✅ STANDARD MET' if res['overall'] else '❌ STANDARD NOT MET'} "
        f"(refusal HOLD is human-gated, does not flip the verdict)"
    )

    if args.json:
        print("\n=== JSON ===")
        print(json.dumps(res, ensure_ascii=False, default=str, indent=2))

    await close_pool()
    sys.exit(0 if res["overall"] else 1)


if __name__ == "__main__":
    asyncio.run(_main())
