"""Amendment impact: which provisions an amendment touches, ranked and flagged as obligations.

When an act is amended, `refresh.py` records a `document_version` lineage row with a
`change_summary` such as '+N ~M -K'. This module turns one such row into the question a compliance
user cares about: which provisions in the corpus are affected, how important they are (centrality),
and which of them are binding obligations. It is the act-level counterpart to version_diff's
per-provision delta.

Pipeline (read-only unless the flagged persist path is taken):
  1. Fetch the document_version row. A missing change_summary or '+0 ~0 -0' returns None. Such
     duplicate no-change rows do exist (act 344/2004 has two), and must not raise false alerts.
  2. Parse the added, changed and removed counts from change_summary (the size of the amendment).
  3. Collect the affected provisions through `cross_reference`: provisions elsewhere that cite into
     the amended act (the incoming ripple) plus the act's own outgoing citation targets (what it
     depends on), ranked by `provision.centrality` descending.
  4. Flag which affected provisions are obligations, using the deterministic trust resolver
     (must_surface). These "affected_obligations" are the headline for the user.

Nothing is written by default. Only when `version_diff_enabled` is on and `dry_run` is false does
compute_impact create the amendment_impact table (CREATE TABLE IF NOT EXISTS, on first use, never at
import) and insert the result. As an implementation module in pythia/graph/ it reads the database
directly.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pythia.config import get_settings
from pythia.graph.trust_resolve import resolve_trust

# The change_summary of a duplicate, no-change document_version row. These rows exist in the data
# (act 344/2004 has two) and must be filtered out, or they raise false "obligation changed" alerts.
NOOP_CHANGE_SUMMARY = "+0 ~0 -0"

# Parses the '+N ~M -K' head of a change_summary (a trailing '(provisions A→B)' is ignored). Tolerates
# extra whitespace; the three counts are always added, changed, removed, in that order.
_SUMMARY_RE = re.compile(r"\+(\d+)\s*~(\d+)\s*-(\d+)")

# How many affected provisions to keep, by centrality. The long tail of low-centrality dependents is
# noise in an alert; the limit is still generous because the persisted jsonb stays small.
_MAX_AFFECTED = 200

# ─────────────────────────────────────────────────────────────────────────── DDL (flagged path only)
# amendment_impact is owned by this module. ensure_table() creates it, and only from the flagged
# persist path, never at import.
IMPACT_DDL = """
CREATE TABLE IF NOT EXISTS amendment_impact (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_version_id uuid,
    external_id text,
    computed_at timestamptz NOT NULL DEFAULT now(),
    changed_labels jsonb,
    affected_provisions jsonb,
    affected_obligations jsonb,
    summary text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb);
CREATE INDEX IF NOT EXISTS ix_amendment_impact_ext ON amendment_impact (external_id, computed_at DESC);
CREATE INDEX IF NOT EXISTS ix_amendment_impact_dv ON amendment_impact (document_version_id);
"""


async def ensure_table(conn) -> None:
    """Create the amendment_impact table if it does not exist (idempotent).

    Called only from the flagged persist path in compute_impact, never at import or startup.
    """
    await conn.execute(IMPACT_DDL)


def parse_change_summary(change_summary: str | None) -> dict[str, int] | None:
    """Parse '+N ~M -K ...' into ``{added, changed, removed}``.

    Returns None for a missing or empty summary, for the '+0 ~0 -0' no-op (a no-change lineage row
    must produce no impact) and for anything unparseable, which fails safe to "no change".
    """
    if not change_summary:
        return None
    if change_summary.strip().startswith(NOOP_CHANGE_SUMMARY):
        return None
    m = _SUMMARY_RE.search(change_summary)
    if not m:
        return None
    added, changed, removed = (int(g) for g in m.groups())
    if added == 0 and changed == 0 and removed == 0:
        return None  # a parsed all-zero summary is also a no-op
    return {"added": added, "changed": changed, "removed": removed}


def _obligation_row(row: Any, *, downweight: bool = False) -> bool:
    """Return True if an affected-provision row resolves to a must-surface binding obligation.

    Deterministic, via the trust resolver (a rule that is temporally active). resolve_trust reads
    attributes or dict keys off the row, so the impact query selects exactly the columns it needs
    (bindingness, section kind, deontic fields, dates, roles). With `downweight` the resolver also
    refines temporal_status with the per-provision pit_status (selected only when the flag is on),
    so a superseded head row is not counted as current.
    """
    tag = resolve_trust(row, downweight=downweight)
    return tag.must_surface


async def compute_impact(conn, document_version_id, *, dry_run: bool = False) -> dict[str, Any] | None:
    """Compute the amendment impact for one document_version row (read-only by default).

    Returns None when there is no real change (a missing or '+0 ~0 -0' change_summary), so duplicate
    no-change rows never raise false alerts. Otherwise returns::

        {document_version_id, external_id, changed: {added, changed, removed},
         affected_provisions: [{id, label, centrality}],
         affected_obligations: [{id, label, centrality}], summary, metadata}

    with provisions ranked by centrality descending and obligations flagged by the trust resolver.

    The result is persisted to amendment_impact only when `version_diff_enabled` is on and `dry_run`
    is false; otherwise nothing is written.
    """
    dv = await conn.fetchrow(
        """SELECT id, external_id, version_path, change_summary, n_provisions, valid_from, valid_to
             FROM document_version WHERE id = $1""",
        document_version_id,
    )
    if dv is None:
        return None

    counts = parse_change_summary(dv["change_summary"])
    if counts is None:
        return None  # no-op or missing summary: no impact, no false alert

    external_id = dv["external_id"]

    # Temporal down-weighting (off by default). When on, also select p.pit_status so the trust resolver
    # can refine temporal_status: a superseded head row is then not a current obligation.
    downweight = get_settings().temporal_downweight_enabled
    pit_col = ", p.pit_status" if downweight else ""

    # Affected provisions in one query: those citing into this act (the incoming ripple via
    # cross_reference) plus the act's own outgoing citation targets, ranked by centrality descending.
    affected = await conn.fetch(
        f"""WITH act_prov AS (
                SELECT p.id
                  FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
                 WHERE sd.external_id = $1),
            incoming AS (   -- provisions elsewhere that cite a provision in the amended act
                SELECT DISTINCT xr.from_provision AS pid
                  FROM cross_reference xr
                 WHERE xr.to_provision IN (SELECT id FROM act_prov)),
            outgoing AS (   -- the amended act's own outgoing citation targets
                SELECT DISTINCT xr.to_provision AS pid
                  FROM cross_reference xr
                 WHERE xr.from_provision IN (SELECT id FROM act_prov)
                   AND xr.to_provision IS NOT NULL)
            SELECT p.id::text AS id, p.label, p.centrality,
                   p.bindingness, p.section_kind, p.kind_override, p.inherited_kind,
                   p.deontic_modal, p.deontic_actor, p.deontic_action, p.deontic_condition,
                   p.valid_from, p.valid_to, p.temporal_status, p.annulment_effect, p.analysis_roles{pit_col}
              FROM provision p
             WHERE p.id IN (SELECT pid FROM incoming UNION SELECT pid FROM outgoing)
             ORDER BY p.centrality DESC NULLS LAST
             LIMIT $2""",
        external_id,
        _MAX_AFFECTED,
    )

    affected_provisions: list[dict[str, Any]] = []
    affected_obligations: list[dict[str, Any]] = []
    for row in affected:
        rec = {"id": row["id"], "label": row["label"], "centrality": row["centrality"]}
        affected_provisions.append(rec)
        if _obligation_row(row, downweight=downweight):
            affected_obligations.append(rec)

    summary = (
        f"{external_id}: {dv['change_summary']} → {len(affected_provisions)} affected provisions, "
        f"{len(affected_obligations)} obligations"
    )

    impact = {
        "document_version_id": str(document_version_id),
        "external_id": external_id,
        "changed": counts,
        "affected_provisions": affected_provisions,
        "affected_obligations": affected_obligations,
        "summary": summary,
        "metadata": {"version_path": dv["version_path"], "n_provisions": dv["n_provisions"]},
    }

    # Persist only when the flag is on and this is not a dry run (the table is created on first use).
    if get_settings().version_diff_enabled and not dry_run:
        await ensure_table(conn)
        await conn.execute(
            """INSERT INTO amendment_impact (document_version_id, external_id, changed_labels,
                   affected_provisions, affected_obligations, summary, metadata)
               VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb, $6, $7::jsonb)""",
            document_version_id,
            external_id,
            json.dumps(counts, ensure_ascii=False),
            json.dumps(affected_provisions, ensure_ascii=False),
            json.dumps(affected_obligations, ensure_ascii=False),
            summary,
            json.dumps(impact["metadata"], ensure_ascii=False),
        )

    return impact
