"""Deterministic section-kind classifier, the foundation of trust tagging.

Trust is tagged at the section level: the structural block (preamble, definitions, operative,
penalties, transitional_final) decides what kind of rule a provision is. Section anchors (the
synthetic Část/Hlava/Díl/Oddíl/§-group rows) carry only a label, so each anchor's kind is voted from
its child provisions and stored as `section_kind`, with a `kind_source` audit string and an optional
human `kind_override` per provision. `pythia.graph.trust_resolve` combines it with the source
document's bindingness at retrieval time.

Rolling every descendant up to the largest ancestor and letting any single outlier win mislabels
whole chapters (a Část tagged `penalties` because of one § with a sanction; '§ Závěrečná ustanovení'
tagged penalties instead of transitional). So the classifier works at the finest grain (only anchors
that directly contain real leaves are classified, from those leaves alone; coarser anchors stay NULL
and each leaf inherits the tag of its nearest classified ancestor), each child votes from its title
line only (`_child_kind`), and a non-operative kind needs dominance (`_resolve_anchor`). `operative`
is the neutral default. The DDL is additive and idempotent, and `--unmake` reverts it.

  ensure  :  .venv/bin/python -m pythia.graph.section_kind --ensure-schema   (run first)
  dry-run :  .venv/bin/python -m pythia.graph.section_kind
  apply   :  .venv/bin/python -m pythia.graph.section_kind --backfill
  undo    :  .venv/bin/python -m pythia.graph.section_kind --unmake   (alias: --drop, --down)
"""

from __future__ import annotations

import asyncio
import re
import sys

from pythia.db import close_pool, get_pool

# ─────────────────────────────────────────────────────────────────── the five section kinds
# operative is the no-evidence default (a binding, general rule). The other four need positive
# evidence. penalties and transitional_final keep the document's tier (sanctions, účinnost and
# repeals are binding), as do definitions. preamble_recital is the only kind that can downgrade a
# provision to interpretive, and only conservatively: a recital provision with a binding cue is
# never downgraded. See trust_resolve.TIER_MAP.
KIND_PREAMBLE = "preamble_recital"
KIND_DEFINITIONS = "definitions"
KIND_OPERATIVE = "operative"
KIND_PENALTIES = "penalties"
KIND_TRANSITIONAL = "transitional_final"

SECTION_KINDS: tuple[str, ...] = (
    KIND_PREAMBLE,
    KIND_DEFINITIONS,
    KIND_OPERATIVE,
    KIND_PENALTIES,
    KIND_TRANSITIONAL,
)

# The non-operative kinds in tie-break order. Used only when two kinds tie on the plurality, never
# as a "first vote wins" priority; dominance is decided by the per-kind thresholds in
# `_resolve_anchor`. penalties and transitional_final (sanctions, účinnost, repeals) are binding
# kinds that must never be hidden, so on an exact tie they outrank definitions, which in turn
# outranks preamble_recital (the only kind that can downgrade).
_NONOP_TIEBREAK: tuple[str, ...] = (
    KIND_PENALTIES,
    KIND_TRANSITIONAL,
    KIND_DEFINITIONS,
    KIND_PREAMBLE,
)

# analysis_roles codes: 1 = definition cue, 7 = sanction cue (see role_classifier.ROLES).
_ROLE_DEFINITION = 1
_ROLE_SANCTION = 7

# Dominance thresholds; a single outlier child can never reach any of them.
_DEFINITION_FRACTION = 0.60  # definitions: at least 60% of children vote definitions
_PENALTY_FRACTION = 0.40  # penalties: at least max(2, 40% of children) and the non-operative plurality
_PENALTY_MIN = 2  # absolute floor: a single penalty § never makes the section penalties
_TRANSITIONAL_FRACTION = 0.40  # transitional: plurality, and a trailing block or at least 40%


# ───────────────────────────────────────────────────────────── Czech and English title-line markers
# Only a child's title line (label plus text up to the first numbered clause or duty, see
# `_title_line`) is matched against these. The order is the per-child tie-break (penalties,
# transitional_final, definitions, preamble_recital); `operative` has no marker because it is the
# default. All patterns are anchored on word boundaries, so only a heading word fires, not a
# fragment of 'pokuta' or 'pro účely' that survives into the title slice. Case-insensitive. A child
# with no match votes operative.
_KIND_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # penalties: přestupk*, správní delikt*, sankc*, pokut*;
    # English penalt*, sanction*, fine(s), offence(s).
    (
        KIND_PENALTIES,
        re.compile(
            r"\b(přestupk\w*|spr[aá]vn[ií]\w*\s+delikt\w*|sankc\w*|pokut\w*|"
            r"penalt\w*|sanction\w*|fines?|offen[cs]es?)\b",
            re.IGNORECASE,
        ),
    ),
    # transitional_final: přechodn*, závěrečn*, zrušovac*, zrušuj*, účinnost; English transitional,
    # final provisions, entry into force, repeal, commencement, and "amendment(s) to/of ...". The
    # last covers EU final-block amendment articles such as "Amendments to Directive 2002/92/EC":
    # legislative housekeeping, not a duty for a business, so it must stay out of the operative
    # obligation register. It is English-only and has no effect on Czech titles.
    (
        KIND_TRANSITIONAL,
        re.compile(
            r"\b(přechodn\w*|z[aá]v[eě]re[cč]n\w*|zru[sš]ovac\w*|zru[sš]uj\w*|ú[cč]innost\w*|"
            r"transitional|final\s+provisions?|entry\s+into\s+force|enters?\s+into\s+force|"
            r"repeal\w*|commencement|amendments?\s+(?:to|of))\b",
            re.IGNORECASE,
        ),
    ),
    # definitions: vymezení (základních) pojmů, definice;
    # English definition(s), "for the purposes of".
    (
        KIND_DEFINITIONS,
        re.compile(
            r"(\b(vymezen[ií]\s+(?:z[aá]kladn[ií]ch\s+)?pojm\w*|definic\w*|definitions?)\b"
            r"|\bfor\s+the\s+purposes\s+of\s+th)",
            re.IGNORECASE,
        ),
    ),
    # preamble_recital: úvodní ustanovení, preambule, předmět úpravy; English recital, whereas,
    # preamble, subject matter, "scope of this". předmět úpravy is only a heading marker here; a
    # role-8 (scope) provision stays operative and binding through trust_resolve, which is the safe
    # direction.
    (
        KIND_PREAMBLE,
        re.compile(
            r"\b([uú]vodn[ií]\s+ustanoven\w*|preambul\w*|preamble|"
            r"p[rř]edm[eě]t\s+[uú]pravy|recitals?|whereas|"
            r"subject\s+matter|scope\s+of\s+this)\b",
            re.IGNORECASE,
        ),
    ),
)

# The start of a numbered clause or duty, which ends the title line: a bold or plain odst. number
# ("**(1)**" or "(1)"), a lettered point "a)", a "1." opener, or the obligation verb stems that
# begin the operative body. Everything before the first such token is the heading; the body after it
# is never a marker source, so a 'pokuta' in the body cannot vote.
_CLAUSE_START = re.compile(
    r"(\*{0,2}\(\s*\d+\s*\)|\b[a-zěščřžýáíéúůďťň]\)|(?<![\d§])\b\d{1,3}\.\s|"
    r"\b(?:je\s+povinn|jsou\s+povinn|nesm[ií]|zakazuje\s+se|stanov[ií]\s+se|shall\b|must\b))",
    re.IGNORECASE,
)

# One leading ordinal echo to peel off a heading line: a repeated "§ N", "Čl. N", "článek N" or
# "odst. N" label, with an optional letter suffix such as "§ 87b". A provision's `head` is
# "{label} {text}" and repeats the ordinal up to three times ("§ 1 ##### § 1\n\nPředmět úpravy");
# each echo is peeled so the real heading line is reached.
_ORDINAL_ECHO = re.compile(
    r"^(?:§\s*\d+\s*[a-z]?|čl\.?\s*\d+\s*[a-z]?|čl[áa]nek\s*\d+\s*[a-z]?|odst\.?\s*\d+)\b\.?", re.IGNORECASE
)


def _strip_ordinals(line: str) -> str:
    """Peel all leading ordinal echoes ("§ N", "Čl. N", ...) and markdown #/* markers off a line."""
    prev = None
    cur = line.strip(" #*\t")
    while cur and cur != prev:
        prev = cur
        cur = _ORDINAL_ECHO.sub("", cur).strip(" #*\t")
    return cur


def _title_line(head: str | None) -> str:
    """Return the heading slice of a child provision, capped at 120 characters.

    `head` is "{label} {text}" where text is markdown ("##### § N\\n\\n{Heading}\\n{body}" or "Čl.
    N\\n{Heading}"). Markdown bold and heading markers and the ordinal echoes are dropped, then
    everything up to (not including) the first numbered clause, lettered point or obligation verb is
    kept. Markers read only this slice, so a 'pokuta' or 'pro účely' buried in the operative body
    never votes. A § without a heading (e.g. "Zákon nabývá účinnosti...") yields its first sentence
    instead.
    """
    if not head:
        return ""
    text = head.replace("**", " ").replace("#", " ")  # drop markdown bold and heading markers
    m = _CLAUSE_START.search(text)
    region = text[: m.start()] if m else text  # the heading region (before the first numbered clause)
    for raw in region.splitlines():  # first non-echo physical line is the heading
        line = _strip_ordinals(raw)
        if line:
            return line[:120]
    return _strip_ordinals(" ".join(region.split()))[:120]  # § without a heading: first body sentence


def _match_marker(head: str | None) -> str | None:
    """Return the first title-line marker kind that fires, in tie-break order, or None (operative).

    Only the `_title_line` heading slice is read, with word-boundary matches, so a mention in the
    body can never vote.
    """
    title = _title_line(head)
    if not title:
        return None
    for kind, pat in _KIND_MARKERS:
        if pat.search(title):
            return kind
    return None


def _child_kind(head: str | None, is_definition: bool | None, roles: list[int] | None) -> str:
    """Return one child's vote (one of the five kinds) from its title line and columns only.

    A title-line marker wins first (tie-break order: penalties, transitional_final, definitions,
    preamble). Without a marker, column evidence votes: a role-7 (sanction) cue means penalties, an
    `is_definition` flag or role-1 cue means definitions. With no evidence at all the child votes
    `operative`, so a non-operative kind is never asserted without positive evidence.
    """
    marker = _match_marker(head)
    if marker is not None:
        return marker
    role_set = set(roles or ())
    if _ROLE_SANCTION in role_set:
        return KIND_PENALTIES
    if is_definition or _ROLE_DEFINITION in role_set:
        return KIND_DEFINITIONS
    return KIND_OPERATIVE


def _resolve_anchor(votes: list[str], roles_present: list[list[int]] | None = None, trailing: bool = False) -> str:
    """Collapse the direct-child votes of one finest section into a single section_kind.

    `operative` is the neutral default. A non-operative kind wins only on dominance, so one outlier
    child can never flip a section. With `m` the number of non-operative votes and `n` the number of
    children:

      - penalties: penalty votes are the plurality of the m non-operative votes and at least
        max(2, 0.4 * n). One penalty § among 20 operative ones (1 < 2) stays operative.
      - definitions: definition votes are at least 0.6 * n. A real definitions block is dense by
        construction; a lone defined term inside an operative § cannot reach 60%.
      - transitional_final: transitional votes are the plurality and the block is trailing or has at
        least 0.4 * n votes.
      - preamble_recital: preamble votes are the plurality, at least 0.4 * n, and there are no
        penalty, operative or obligation children. Deliberately conservative: only a genuine short
        intro block, never a "general provisions" chapter that also carries operative rules.
      - otherwise: operative.

    Args:
        votes: One vote per child, from `_child_kind`.
        roles_present: Optional, parallel to `votes`: each child's analysis_roles. An obligation
            (role 2) or prohibition (role 3) child counts as substantive operative and vetoes the
            preamble downgrade even when it voted plain `operative`.
        trailing: Whether this is the last block in its parent (the DB fetch derives it from sibling
            ordinals). It only loosens the transitional rule.
    """
    if not votes:
        return KIND_OPERATIVE
    n = len(votes)
    counts = {k: votes.count(k) for k in SECTION_KINDS}
    nonop_total = n - counts[KIND_OPERATIVE]
    # The dominant non-operative kind: the plurality among the four, with the fixed tie-break.
    dominant = max(_NONOP_TIEBREAK, key=lambda k: (counts[k], -_NONOP_TIEBREAK.index(k)))
    dom_n = counts[dominant]

    # Role evidence: sanction (role 7) density for penalties; obligation/prohibition (2/3) vetoes
    # the preamble downgrade.
    role_sets = [set(r or ()) for r in (roles_present or [])]
    sanction_children = sum(1 for rs in role_sets if _ROLE_SANCTION in rs)
    has_substantive_operative = counts[KIND_OPERATIVE] > 0 or any(rs & {2, 3} for rs in role_sets)

    # Penalties: the non-operative plurality and dense (at least max(2, 40%)). Children with a
    # sanction role (7) count toward the penalty mass.
    penalty_mass = max(counts[KIND_PENALTIES], sanction_children)
    if (dominant == KIND_PENALTIES or penalty_mass >= counts[dominant]) and penalty_mass > 0:
        if penalty_mass >= max(_PENALTY_MIN, _PENALTY_FRACTION * n) and penalty_mass >= nonop_total - penalty_mass:
            return KIND_PENALTIES

    # Definitions: a dense block (at least 60% of all children).
    if counts[KIND_DEFINITIONS] >= _DEFINITION_FRACTION * n:
        return KIND_DEFINITIONS

    # Transitional/final: the non-operative plurality, and a trailing block or at least 40%.
    if dominant == KIND_TRANSITIONAL and dom_n > 0 and dom_n == max(counts[k] for k in _NONOP_TIEBREAK):
        if trailing or dom_n >= _TRANSITIONAL_FRACTION * n:
            return KIND_TRANSITIONAL

    # Preamble/recital, the most conservative rule: dominant preamble votes, at least 40%, and no
    # substantive operative or penalty children (a genuine short intro, never a "general provisions"
    # chapter).
    if (
        dominant == KIND_PREAMBLE
        and dom_n > 0
        and dom_n >= _TRANSITIONAL_FRACTION * n
        and not has_substantive_operative
        and penalty_mass == 0
        and dom_n == max(counts[k] for k in _NONOP_TIEBREAK)
    ):
        return KIND_PREAMBLE

    return KIND_OPERATIVE


def plan_kinds(anchors: list[dict]) -> list[dict]:
    """Classify finest-grain anchors from their children's votes (pure, no database).

    Args:
        anchors: One dict per finest anchor (a direct parent of at least one real leaf)::

                {"id": <anchor_id>, "trailing": bool (optional), "children": [
                    {"head": str | None, "is_definition": bool | None, "roles": list[int] | None},
                    ...
                ]}

            `children` are the anchor's direct real leaf provisions only, with no deep roll-up.
            Coarse anchors (parents of anchors only) are never passed in and stay NULL; the resolver
            inherits the nearest classified ancestor. `trailing` (default False) marks the last
            block in its parent and only loosens the transitional rule.

    Returns:
        One {"id", "section_kind", "kind_source"} dict per anchor. `kind_source` records the vote
        tally so every tag is reproducible. An anchor with no children resolves to `operative`,
        never to a downgrade based on zero evidence.
    """
    out: list[dict] = []
    for a in anchors:
        children = a.get("children") or []
        votes = [_child_kind(c.get("head"), c.get("is_definition"), c.get("roles")) for c in children]
        roles_present = [c.get("roles") for c in children]
        kind = _resolve_anchor(votes, roles_present, trailing=bool(a.get("trailing")))
        tally = {k: votes.count(k) for k in SECTION_KINDS if votes.count(k)}
        out.append(
            {
                "id": a["id"],
                "section_kind": kind,
                "kind_source": f"b1a08_vote n={len(votes)} {tally}",
            }
        )
    return out


# ────────────────────────────────────────────────────────────────────────────────────────────── DDL
# Additive, idempotent and CHECK-constrained to the five-value vocabulary (CI pins this vocabulary
# to the matching CHECK constraint in the app schema). kind_override is the per-provision human
# escape hatch (NULL means none); the resolver prefers it over the inherited section_kind. The
# partial index serves the only_empty classification pass and the --unmake cleanup.
_DDL = f"""
ALTER TABLE provision ADD COLUMN IF NOT EXISTS section_kind  text;
ALTER TABLE provision ADD COLUMN IF NOT EXISTS kind_source   text;
ALTER TABLE provision ADD COLUMN IF NOT EXISTS kind_override text;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'provision_section_kind_chk') THEN
    ALTER TABLE provision ADD CONSTRAINT provision_section_kind_chk
      CHECK (section_kind IS NULL OR section_kind IN
             ('{KIND_PREAMBLE}','{KIND_DEFINITIONS}','{KIND_OPERATIVE}','{KIND_PENALTIES}','{KIND_TRANSITIONAL}'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'provision_kind_override_chk') THEN
    ALTER TABLE provision ADD CONSTRAINT provision_kind_override_chk
      CHECK (kind_override IS NULL OR kind_override IN
             ('{KIND_PREAMBLE}','{KIND_DEFINITIONS}','{KIND_OPERATIVE}','{KIND_PENALTIES}','{KIND_TRANSITIONAL}'));
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_provision_section_kind ON provision (section_kind) WHERE section_kind IS NOT NULL;
"""

_DROP = """
DROP INDEX IF EXISTS ix_provision_section_kind;
ALTER TABLE provision DROP CONSTRAINT IF EXISTS provision_section_kind_chk;
ALTER TABLE provision DROP CONSTRAINT IF EXISTS provision_kind_override_chk;
ALTER TABLE provision DROP COLUMN IF EXISTS section_kind;
ALTER TABLE provision DROP COLUMN IF EXISTS kind_source;
ALTER TABLE provision DROP COLUMN IF EXISTS kind_override;
"""


async def ensure_schema() -> None:
    """Add the section_kind, kind_source and kind_override columns, CHECK constraints and index.

    Idempotent (re-running is a no-op), mirroring ingest.section_rows.ensure_schema. Run before
    classifying.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_DDL)


# ─────────────────────────────────────────────── per-act fetch (finest grain, direct children only)
# For every finest anchor in an act (one whose immediate children include at least one real,
# non-anchor leaf), gather only those direct leaf children's heading text, is_definition and roles.
# There is no recursive roll-up: a coarse anchor whose children are all sub-anchors is not selected
# and stays NULL, and trust_resolve inherits the nearest classified ancestor, so the finest section
# wins. `head` is the child's label plus a short leading slice of its body (the title line sits at
# the top; `_title_line` trims it to the heading). `is_definition` combines the column with a role-1
# cue, so the definitions vote still works while the boolean column is empty in this corpus.
# `is_trailing` flags the last anchor among its parent's section siblings (by ordinal) and only
# loosens the transitional_final rule.
_FETCH_ACT_ANCHORS = """
    WITH anchors AS (
        SELECT a.id, a.parent_id, a.ordinal
          FROM provision a
         WHERE a.source_document_id = $1 AND a.is_anchor_only = true
           AND ($2::boolean = false OR a.section_kind IS NULL)        -- only_empty: skip already-classified
           AND EXISTS (SELECT 1 FROM provision c                       -- FINEST: has ≥1 DIRECT real leaf child
                        WHERE c.parent_id = a.id AND c.is_anchor_only = false)
    ),
    trail_flags AS (   -- the last anchor under each parent (by ordinal) is a trailing/final block candidate
        SELECT id, (ordinal = max(ordinal) OVER (PARTITION BY parent_id)) AS is_trailing
          FROM provision
         WHERE source_document_id = $1 AND is_anchor_only = true
    )
    SELECT a.id::text AS anchor_id,
           coalesce(t.is_trailing, false) AS is_trailing,
           c.id::text AS child_id,
           left(coalesce(c.label, '') || ' ' || coalesce(c.text, ''), 240) AS head,
           coalesce(c.is_definition, false) OR (1 = ANY(coalesce(c.analysis_roles, '{}'))) AS is_definition,
           coalesce(c.analysis_roles, '{}')::int[] AS roles
      FROM anchors a
      LEFT JOIN trail_flags t ON t.id = a.id
      JOIN provision c ON c.parent_id = a.id AND c.is_anchor_only = false   -- DIRECT leaf children ONLY
"""


def _group_children(rows: list[dict]) -> list[dict]:
    """Group flat (anchor_id, child fields) fetch rows into the anchor shape `plan_kinds` expects.

    The per-anchor `trailing` flag is carried through for the transitional rule.
    """
    by_anchor: dict[str, dict] = {}
    for r in rows:
        a = by_anchor.setdefault(
            r["anchor_id"], {"id": r["anchor_id"], "trailing": bool(r.get("is_trailing")), "children": []}
        )
        a["children"].append(
            {
                "head": r["head"],
                "is_definition": r["is_definition"],
                "roles": list(r["roles"]) if r["roles"] is not None else [],
            }
        )
    return list(by_anchor.values())


async def classify_sections(only_empty: bool = True) -> dict:
    """Classify and write section_kind on every finest-grain anchor, act by act.

    For each act, fetches each finest anchor's direct leaf children (no roll-up), votes with the
    pure planner and writes section_kind and kind_source on those anchors. Coarse anchors (parents
    of sub-anchors only) are not touched and stay NULL; trust_resolve inherits the nearest
    classified ancestor. With `only_empty=True` (the default) only anchors whose section_kind is
    NULL are classified, so a human kind_override is never overwritten and the run is idempotent.
    """
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        docs = [r["id"] for r in await conn.fetch("SELECT id::text id FROM source_document ORDER BY external_id")]
        totals = {"acts": 0, "anchors_classified": 0, "by_kind": {k: 0 for k in SECTION_KINDS}}
        for doc_id in docs:
            rows = [dict(r) for r in await conn.fetch(_FETCH_ACT_ANCHORS, doc_id, only_empty)]
            anchors = _group_children(rows)
            if not anchors:
                continue
            plans = plan_kinds(anchors)
            async with conn.transaction():
                await conn.executemany(
                    "UPDATE provision SET section_kind = $2, kind_source = $3 "
                    "WHERE id = $1::uuid AND is_anchor_only = true"
                    + ("" if not only_empty else " AND section_kind IS NULL"),
                    [(p["id"], p["section_kind"], p["kind_source"]) for p in plans],
                )
            totals["acts"] += 1
            totals["anchors_classified"] += len(plans)
            for p in plans:
                totals["by_kind"][p["section_kind"]] += 1
    return totals


async def dry_run() -> dict:
    """Classify in memory without writing and return the per-kind tallies.

    Meant for a lawyer to spot-check before --backfill. Coarse anchors are intentionally absent
    because they would stay NULL.
    """
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        docs = [r["id"] for r in await conn.fetch("SELECT id::text id FROM source_document ORDER BY external_id")]
        totals = {"acts": 0, "anchors_classified": 0, "by_kind": {k: 0 for k in SECTION_KINDS}}
        for doc_id in docs:
            rows = [dict(r) for r in await conn.fetch(_FETCH_ACT_ANCHORS, doc_id, True)]
            anchors = _group_children(rows)
            if not anchors:
                continue
            plans = plan_kinds(anchors)
            totals["acts"] += 1
            totals["anchors_classified"] += len(plans)
            for p in plans:
                totals["by_kind"][p["section_kind"]] += 1
    return totals


async def unmake() -> dict:
    """Drop the section_kind, kind_source and kind_override columns, constraints and index."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_DROP)
    return {"schema_dropped": True}


async def _main() -> None:
    if "--ensure-schema" in sys.argv:
        await ensure_schema()
        print("✅ ensured: provision.section_kind + kind_source + kind_override (CHECK + index).")
        await close_pool()
        return
    if any(f in sys.argv for f in ("--unmake", "--drop", "--down")):
        await unmake()
        print("⏪ reverted: dropped section_kind/kind_source/kind_override columns + constraints + index.")
        await close_pool()
        return
    apply = "--backfill" in sys.argv
    res = await (classify_sections(only_empty=True) if apply else dry_run())
    print(f"=== B1a-08 section-kind classifier — {'APPLIED' if apply else 'DRY-RUN (no write)'} ===")
    print(f"  acts classified        : {res['acts']:,}")
    print(f"  anchors classified     : {res['anchors_classified']:,}")
    for k in SECTION_KINDS:
        print(f"     {k:<20} {res['by_kind'][k]:,}")
    if not apply:
        print("\n  (preview only — re-run with --backfill to write; --unmake to revert)")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
