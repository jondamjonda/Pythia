"""Group retrieved provisions by legal role, in the order a lawyer reads them.

Given retrieved provision ids, bucket them by ``analysis_roles``: what the regime governs, key
definitions, obligations, prohibitions and permissions, applicability conditions, exceptions,
sanctions and procedures. This lets an answer be structured like a compliance memo instead of a
flat list of citations.

Run:  .venv/bin/python -m pythia.reasoning.assemble "your legal question" [cte|ppr|gnn]
"""

from __future__ import annotations

import asyncio
import sys

from pythia.db import close_pool
from pythia.ports import ROLES, NexusPort

# Lawyer's reading order and display labels.
_ORDER = [
    "scope_rule",
    "definition_rule",
    "obligation_rule",
    "prohibition_rule",
    "permission_rule",
    "condition_rule",
    "exception_rule",
    "sanction_rule",
    "procedure_rule",
]
_LABEL = {
    "scope_rule": "Scope / applicability",
    "definition_rule": "Definitions",
    "obligation_rule": "Obligations",
    "prohibition_rule": "Prohibitions",
    "permission_rule": "Permissions",
    "condition_rule": "Conditions",
    "exception_rule": "Exceptions",
    "sanction_rule": "Sanctions",
    "procedure_rule": "Procedures",
}


async def partition_by_role(provision_ids: list[str]) -> dict[str, list[dict]]:
    """Return ``{role label: [{id, ref}]}`` in reading order, omitting empty buckets.

    Provision metadata is read through ``NexusPort.fetch_provisions``, which preserves order and
    accepts an empty list.
    """
    rows = await NexusPort().fetch_provisions(provision_ids)
    by_id = {r.id: r for r in rows}
    buckets: dict[str, list[dict]] = {k: [] for k in _ORDER}
    for pid in provision_ids:  # preserve retrieval order within each bucket
        r = by_id.get(pid)
        if not r:
            continue
        for code in r.analysis_roles or []:
            role = ROLES.get(code)
            if role in buckets:
                buckets[role].append({"id": pid, "ref": f"{r.external_id} {r.label}"})
    return {_LABEL[k]: v for k, v in buckets.items() if v}


async def _demo(query: str, method: str) -> None:
    from pythia.contract import PythiaQuery
    from pythia.engine import PythiaEngine

    ans = await PythiaEngine().retrieve(PythiaQuery(query=query, jurisdiction=["CZ", "EU"], k=25), method=method)
    packet = await partition_by_role([c.provision_id for c in ans.cited_provisions])
    print(f"QUESTION: {query}\nretrieved {len(ans.cited_provisions)} provisions · method {method}\n")
    print("=== COMPLIANCE PACKET (by role) ===")
    for role, items in packet.items():
        print(f"\n{role}:")
        for it in items[:8]:
            print(f"  • {it['ref']}")
    if not packet:
        print("  (no roles on the retrieved provisions yet — run enrich_roles / role_classifier)")
    await close_pool()


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "licence k činnosti banky a podmínky jejího udělení"
    m = sys.argv[2] if len(sys.argv) > 2 else "cte"
    asyncio.run(_demo(q, m))
