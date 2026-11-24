"""Print a structured report of the analysis results for the most recent project with L4 gaps.

Covers project metadata and derived tags, L2 applicable findings by source document, L3
obligations by modal and actor, L4 gaps by missing evidence type (each with examples), and one
full provenance chain from a gap back through its obligation and applicable finding to the
provision.

Usage (from app/): python scripts/inspect_day12.py
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy import text as sql_text

from regsuper.storage.database import session_scope
from regsuper.storage.models import (
    Finding,
    Project,
    SourceDocument,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

H = "─" * 72


def hdr(t: str) -> None:
    print(f"\n{H}\n {t}\n{H}")


async def main() -> None:
    async with session_scope() as session:
        # Pick the most recent project that has L4 findings
        latest_id = (
            await session.execute(
                sql_text(
                    """
                    SELECT project_id
                      FROM finding
                     WHERE analysis_layer = 'L4'
                     GROUP BY project_id
                     ORDER BY MAX(created_at) DESC
                     LIMIT 1
                    """
                )
            )
        ).scalar_one_or_none()
    if latest_id is None:
        print("No project with L4 findings found.")
        return
    await _report(latest_id)


async def _report(project_id: UUID) -> None:
    async with session_scope() as session:
        proj = (await session.execute(select(Project).where(Project.id == project_id))).scalar_one()
        findings = (
            (
                await session.execute(
                    select(Finding)
                    .where(Finding.project_id == project_id)
                    .order_by(desc(Finding.created_at))
                )
            )
            .scalars()
            .all()
        )

    # ---------- Project ----------
    hdr(f"PROJECT  {proj.id}")
    meta = proj.metadata_ or {}
    print(f"  name:       {proj.name}")
    print(f"  sector:     {proj.sector}")
    print(f"  juris:      {proj.jurisdiction_scope}")
    print(f"  entity:     {proj.entity_type}")
    print(f"  intent:     {(proj.declared_intent or '')[:160]}")
    print(f"  sector_tags:   {meta.get('sector_tags')}")
    print(f"  activity_tags: {meta.get('activity_tags')}")

    by_type: Counter[str] = Counter(f.finding_type for f in findings)
    print(
        f"\n  findings totals: total={len(findings)}  "
        f"applicable={by_type['applicable']}  "
        f"obligation={by_type['obligation']}  "
        f"gap={by_type['gap']}"
    )

    # ---------- L2 applicable findings ----------
    l2 = [f for f in findings if f.finding_type == "applicable"]
    hdr(f"L2 · applicable findings  ({len(l2)})")
    # Group by source document
    sd_ids = {f.cited_documents[0] for f in l2 if f.cited_documents}
    async with session_scope() as session:
        sd_rows = (
            await session.execute(
                select(SourceDocument.id, SourceDocument.external_id, SourceDocument.title).where(
                    SourceDocument.id.in_(sd_ids)
                )
            )
        ).all()
    sd_lookup = {r[0]: (r[1], r[2]) for r in sd_rows}
    by_source: Counter[str] = Counter()
    for f in l2:
        ext = sd_lookup.get(f.cited_documents[0], ("?", "?"))[0] if f.cited_documents else "?"
        by_source[ext] += 1
    print("  by source document:")
    for ext, n in by_source.most_common():
        title = sd_lookup.get(_first_id_for_ext(sd_rows, ext), ("", ""))[1]
        print(f"    {n:>3}  {ext:<14}  {title[:55]}")

    verdicts: Counter[str] = Counter()
    for f in l2:
        p = f.payload or {}
        verdicts[str(p.get("applicable"))] += 1
    print(f"\n  verdict distribution: {dict(verdicts)}")

    # 3 representative applicable verdicts
    print("\n  3 example verdicts:")
    for f in sorted(l2, key=lambda x: -(x.confidence_overall or 0))[:3]:
        p = f.payload or {}
        provision = await _provision_label(f.cited_provisions[0])
        print(f"    · {provision}  conf={p.get('confidence'):.2f}")
        print(f"      reasoning: {p.get('reasoning', '')[:160]}")

    # ---------- L3 obligations ----------
    l3 = [f for f in findings if f.finding_type == "obligation"]
    hdr(f"L3 · obligations  ({len(l3)})")
    modals: Counter[str] = Counter()
    actors: Counter[str] = Counter()
    with_deadlines = 0
    for f in l3:
        p = f.payload or {}
        modals[str(p.get("modal", "?"))] += 1
        actor = (p.get("actor") or "").strip().lower()
        if actor:
            actors[actor] += 1
        if (p.get("deadline") or "").strip():
            with_deadlines += 1
    print(f"  by modal: {dict(modals)}")
    print(f"  obligations with a deadline: {with_deadlines}/{len(l3)}")
    print("  top actors:")
    for actor, n in actors.most_common(8):
        print(f"    {n:>3}  {actor}")

    # 5 representative obligations spanning modals where possible
    print("\n  5 example obligations:")
    seen_modals: set[str] = set()
    seen_actors: set[str] = set()
    picks: list[Finding] = []
    for f in l3:
        p = f.payload or {}
        m = str(p.get("modal", "?"))
        a = str(p.get("actor", "")).lower()
        if m not in seen_modals or a not in seen_actors:
            picks.append(f)
            seen_modals.add(m)
            seen_actors.add(a)
        if len(picks) >= 5:
            break
    for f in picks:
        p = f.payload or {}
        prov = await _provision_label(f.cited_provisions[0])
        deadline = p.get("deadline") or ""
        cond = p.get("conditions") or []
        print(
            f"    · {p.get('modal')}  {p.get('actor')[:35]:<35}  → {(p.get('action') or '')[:90]}"
        )
        print(f"        cited: {prov}")
        if deadline:
            print(f"        deadline: {deadline}")
        if cond:
            print(f"        conditions: {cond[:2]}")

    # ---------- L4 gaps ----------
    l4 = [f for f in findings if f.finding_type == "gap"]
    hdr(f"L4 · gaps  ({len(l4)})")
    if not l4:
        print("  (no gaps)")
    else:
        missing_types: Counter[str] = Counter()
        for f in l4:
            p = f.payload or {}
            for e in p.get("missing_evidence") or []:
                missing_types[e.get("evidence_type", "?")] += 1
        print("  by evidence_type missing:")
        for t, n in missing_types.most_common():
            print(f"    {n:>3}  {t}")
        print("\n  5 example gaps:")
        for f in l4[:5]:
            p = f.payload or {}
            missing = [e.get("evidence_type") for e in (p.get("missing_evidence") or [])]
            prov = await _provision_label(f.cited_provisions[0])
            print(f"    · missing {missing}")
            print(f"        provision: {prov}")
            print(f"        obligation: {(p.get('obligation_action') or '')[:100]}")

    # ---------- One full provenance chain ----------
    if l4:
        sample_gap = l4[0]
        ob_id = sample_gap.payload.get("obligation_finding_id") if sample_gap.payload else None
        if ob_id:
            async with session_scope() as session:
                ob_row = (
                    await session.execute(select(Finding).where(Finding.id == UUID(ob_id)))
                ).scalar_one_or_none()
            if ob_row is not None:
                pid = ob_row.cited_provisions[0]
                # The L2 applicable finding that justified L3 running on this provision
                async with session_scope() as session:
                    l2_for_p = (
                        await session.execute(
                            select(Finding).where(
                                Finding.project_id == project_id,
                                Finding.analysis_layer == "L2",
                                Finding.cited_provisions == [pid],  # exact array equality
                            )
                        )
                    ).scalar_one_or_none()
                prov_label = await _provision_label(pid)
                hdr("Provenance chain (one gap → obligation → applicable → provision)")
                if l2_for_p is not None:
                    p2 = l2_for_p.payload or {}
                    print(
                        f"  L2 finding:  applicable={p2.get('applicable')}  "
                        f"conf={p2.get('confidence'):.2f}"
                    )
                    print(f"               reasoning: {p2.get('reasoning', '')[:140]}")
                print(f"  Provision:   {prov_label}")
                p3 = ob_row.payload or {}
                print(
                    f"  L3 finding:  {p3.get('modal')} · {p3.get('actor')} → "
                    f"{(p3.get('action') or '')[:100]}"
                )
                if p3.get("deadline"):
                    print(f"               deadline: {p3.get('deadline')}")
                p4 = sample_gap.payload or {}
                print(
                    f"  L4 finding:  gap — missing "
                    f"{[e.get('evidence_type') for e in (p4.get('missing_evidence') or [])]}"
                )
                print("               (because n_client_documents=0 for this project)")


def _first_id_for_ext(rows: list, ext: str) -> UUID | None:
    for sd_id, sd_ext, _title in rows:
        if sd_ext == ext:
            return sd_id
    return None


async def _provision_label(pid: UUID) -> str:
    async with session_scope() as session:
        row = (
            await session.execute(
                sql_text(
                    "SELECT sd.external_id, p.label "
                    "FROM provision p JOIN source_document sd ON sd.id = p.source_document_id "
                    "WHERE p.id = :pid"
                ),
                {"pid": pid},
            )
        ).first()
    if not row:
        return f"<missing {pid}>"
    return f"{row[0]} {row[1]}"


if __name__ == "__main__":
    asyncio.run(main())
