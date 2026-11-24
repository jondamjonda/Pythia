"""Rank the implementing decrees most worth ingesting next (read-only; never ingests).

Statute provisions that delegate rule-making to a decree (zmocňovací ustanovení) are marked with
`provision.delegates_decree` by structural_edges.py, and thousands of citations point at decrees
that were never ingested (`vyhláška č. N/RRRR`, `nařízení vlády č. N/RRRR`). This module ranks those
decrees (see ``rank_candidates``) so a person can approve a bounded ingest batch. It is off by
default (see ``build_queue``) and only reads and prints a plan.

Run (from engine/): .venv/bin/python -m pythia.ingest.decree_queue [--top N]  (prints the plan)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from dataclasses import dataclass, field

# A decree external_id: "<num>/<year>" as it appears inside a citation ("vyhláška č. 296/2017 Sb.").
_DECREE_REF = re.compile(r"\bč\.\s*(\d+/\d{4})")
# The decree-kind word that distinguishes a decree citation from a plain act reference. A citation is a
# decree candidate only if such a word appears just before the number.
_DECREE_KIND = re.compile(
    r"(vyhlášk\w*|nařízení\w*\s+vlády|prováděc\w*\s*(?:právní\s*)?předpis\w*)\s*(?:č\.\s*)?(\d+/\d{4})", re.IGNORECASE
)

# SME boost, keyed by the delegating act's external_id: the acts whose implementing decrees small
# businesses actually deal with. Hand-curated; still a draft pending legal review.
SME_CORE_ACTS: dict[str, float] = {
    "262/2006": 1.0,  # zákoník práce (labour code)
    "586/1992": 0.9,  # daně z příjmů (income tax)
    "235/2004": 0.9,  # DPH (VAT)
    "563/1991": 0.8,  # účetnictví (accounting)
    "455/1991": 0.8,  # živnostenský zákon (trade licensing)
    "309/2006": 0.8,  # BOZP (occupational safety)
    "110/2019": 0.7,  # zpracování osobních údajů (data protection adaptation)
    "112/2016": 0.6,  # EET (sales records)
    "90/2012": 0.6,  # zákon o obchodních korporacích (business corporations)
}

# Ranking weights (a draft pending legal review). Demand dominates; delegation and centrality
# break ties.
_W_DEMAND = 1.0
_W_DELEGATION = 1.5
_W_CENTRALITY = 0.4
_W_SME = 3.0


def queue_enabled() -> bool:
    """Return True if PYTHIA_DECREE_QUEUE_ENABLED is set; the queue is off by default."""
    return os.getenv("PYTHIA_DECREE_QUEUE_ENABLED", "0").strip() in {"1", "true", "True", "yes"}


@dataclass
class DecreeCandidate:
    external_id: str  # e.g. "296/2017"
    kind: str  # 'vyhlaska' (ministerial decree), 'narizeni_vlady' (government regulation) or 'predpis'
    demand: int = 0  # dangling citations pointing at it
    delegation: int = 0  # delegating provisions naming it
    centrality: float = 0.0  # summed out-degree of the delegating statutes
    sme_boost: float = 0.0  # highest SME_CORE_ACTS boost among the delegating statutes
    delegating_acts: set[str] = field(default_factory=set)

    @property
    def score(self) -> float:
        return (
            _W_DEMAND * self.demand
            + _W_DELEGATION * self.delegation
            + _W_CENTRALITY * self.centrality
            + _W_SME * self.sme_boost
        )


def classify_decree_kind(text_before_number: str) -> str:
    """Map the decree-kind word to a coarse kind tag; anything unrecognized is 'predpis'."""
    t = (text_before_number or "").lower()
    if "nařízení" in t and "vlády" in t:
        return "narizeni_vlady"
    if "vyhlášk" in t:
        return "vyhlaska"
    return "predpis"


def extract_decree_refs(text: str) -> list[tuple[str, str]]:
    """Return (decree_external_id, kind) for each implementing-decree citation in one provision.

    Only citations with a decree-kind word (vyhláška, nařízení vlády, prováděcí předpis) count; a
    plain "zákon č. 262/2006" is an act, not a decree. Order is preserved and repeats within one
    provision collapse to one, so a provision citing the same decree twice is one unit of demand.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _DECREE_KIND.finditer(text or ""):
        ext = m.group(2)
        if ext in seen:
            continue
        seen.add(ext)
        out.append((ext, classify_decree_kind(m.group(1))))
    return out


def rank_candidates(
    *,
    demand_rows: list[tuple[str, str]],
    delegation_rows: list[tuple[str, str, str]],
    centrality: dict[str, float] | None = None,
) -> list[DecreeCandidate]:
    """Rank decree candidates from pre-fetched rows; a pure function with no database access.

    The score is a weighted sum, deterministic so the same corpus always gives the same order, of:
      * demand: dangling citations pointing at the decree (unmet retrieval demand);
      * delegation: delegating statute provisions that name it (statute-to-decree links the graph
        will resolve once the decree is present);
      * centrality: the out-degree of the delegating statute in the graph, so a decree under a
        central statute (labour code, VAT act, data-protection act) ranks above one under a
        peripheral act;
      * an SME boost: a small hand-curated bonus for the domains small businesses meet most
        (labour, tax, occupational safety, data protection, accounting, trade licensing).

    Args:
        demand_rows: (decree_external_id, decree_kind), one row per dangling citation.
        delegation_rows: (decree_external_id, decree_kind, delegating_act_external_id).
        centrality: delegating act external_id to its out-degree; missing acts count as 0.0.

    Returns:
        Candidates sorted by score descending, then by external_id, so the order is deterministic.
    """
    centrality = centrality or {}
    cands: dict[str, DecreeCandidate] = {}

    def get(ext: str, kind: str) -> DecreeCandidate:
        c = cands.get(ext)
        if c is None:
            c = cands[ext] = DecreeCandidate(external_id=ext, kind=kind)
        return c

    for ext, kind in demand_rows:
        get(ext, kind).demand += 1

    for ext, kind, act in delegation_rows:
        c = get(ext, kind)
        c.delegation += 1
        if act:
            c.delegating_acts.add(act)

    for c in cands.values():
        for act in c.delegating_acts:
            c.centrality += centrality.get(act, 0.0)
            c.sme_boost = max(c.sme_boost, SME_CORE_ACTS.get(act, 0.0))

    return sorted(cands.values(), key=lambda c: (-c.score, c.external_id))


# ── read-only database driver ────────────────────────────────────────────────────────────────────────
async def _fetch_signals(conn) -> tuple[list, list, dict]:
    """Read the ranking signals with SELECT queries only.

    Returns demand (dangling decree citations), delegation (delegates_decree provisions that name a
    decree) and centrality (out-degree of each act in the graph).
    """
    demand_rows: list[tuple[str, str]] = []
    for r in await conn.fetch(
        """SELECT cr.to_external_ref AS ref, p.text AS citing_text
             FROM cross_reference cr JOIN provision p ON p.id = cr.from_provision
            WHERE cr.ref_type = 'external' AND cr.to_provision IS NULL
              AND cr.to_external_ref IS NOT NULL"""
    ):
        for ext, kind in extract_decree_refs(r["citing_text"] or ""):
            demand_rows.append((ext, kind))

    delegation_rows: list[tuple[str, str, str]] = []
    for r in await conn.fetch(
        """SELECT p.text AS txt, sd.external_id AS act
             FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
            WHERE p.delegates_decree IS TRUE"""
    ):
        for ext, kind in extract_decree_refs(r["txt"] or ""):
            delegation_rows.append((ext, kind, r["act"]))

    centrality: dict[str, float] = {}
    for r in await conn.fetch(
        """SELECT sd.external_id AS act, count(*)::float AS deg
             FROM pythia_edge e JOIN provision p ON p.id = e.src
             JOIN source_document sd ON sd.id = p.source_document_id
            GROUP BY sd.external_id"""
    ):
        centrality[r["act"]] = float(r["deg"])
    return demand_rows, delegation_rows, centrality


async def build_queue(*, top: int = 60) -> dict:
    """Build the ranked plan of decrees to ingest.

    Unless PYTHIA_DECREE_QUEUE_ENABLED=1, returns an empty plan marked DISABLED. Even when enabled
    there is no write path: ingesting a decree is a separate, human-approved step taken with a
    database snapshot.
    """
    if not queue_enabled():
        return {"mode": "DISABLED", "flag": "PYTHIA_DECREE_QUEUE_ENABLED", "candidates": []}
    from pythia.db import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        demand_rows, delegation_rows, centrality = await _fetch_signals(conn)
    ranked = rank_candidates(demand_rows=demand_rows, delegation_rows=delegation_rows, centrality=centrality)[:top]
    return {
        "mode": "DRY-RUN (read-only; NO ingest)",
        "n_ranked": len(ranked),
        "candidates": [
            {
                "external_id": c.external_id,
                "kind": c.kind,
                "score": round(c.score, 2),
                "demand": c.demand,
                "delegation": c.delegation,
                "centrality": round(c.centrality, 1),
                "sme_boost": c.sme_boost,
                "delegating_acts": sorted(c.delegating_acts),
            }
            for c in ranked
        ],
    }


async def _main() -> None:
    ap = argparse.ArgumentParser(description="B3-P2 decree ingest QUEUE (read-only dry-run print).")
    ap.add_argument("--top", type=int, default=60, help="how many top decrees to print (default 60)")
    args = ap.parse_args()
    res = await build_queue(top=args.top)
    if res["mode"] == "DISABLED":
        print("decree_queue is DISABLED — set PYTHIA_DECREE_QUEUE_ENABLED=1 to preview the ranked plan.")
        return
    print(f"=== B3-P2 decree ingest QUEUE — {res['mode']} · top {res['n_ranked']} ===")
    for i, c in enumerate(res["candidates"], 1):
        print(
            f"  {i:>2}. č.{c['external_id']:<10s} {c['kind']:<14s} score={c['score']:>7.2f} "
            f"demand={c['demand']:>3} deleg={c['delegation']:>2} "
            f"cent={c['centrality']:>6.1f} sme={c['sme_boost']:.1f} · {','.join(c['delegating_acts'][:3])}"
        )
    print("\n  (DRY-RUN — ingest is a separate human-approved step; this module NEVER writes.)")


if __name__ == "__main__":
    asyncio.run(_main())
