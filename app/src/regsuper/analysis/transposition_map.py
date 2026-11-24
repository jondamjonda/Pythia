"""Curated map of EU directives to the Czech acts that transpose them.

Three patterns coexist in the corpus, and the analysis cascade has to tell them apart:

1. EU regulations (directly applicable): only the EU text exists.
2. EU directives: transposed into national acts. The Czech statute binds Czech actors; the
   directive is interpretive context.
3. Purely national law: only the Czech text exists.

This module hardcodes pattern 2 for the directive and act pairs in the corpus, as a single
source of truth instead of pair tuples scattered across files. L2 uses
``transposition_note_for_provision`` to tell the LLM whether a provision binds directly or
through national law, and the admin dashboard shows ``coverage_report``. Article-level mappings
(stored as ``cross_reference.ref_type='transposes'``) and merging obligations cited from both
sides of a pair are not implemented here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text as sql_text

from regsuper.storage.database import session_scope


@dataclass(frozen=True)
class TranspositionPair:
    """One pairing of an EU act with a Czech act.

    ``topic`` is a short human label for dashboards and LLM context; it is not used for matching.
    """

    eu_celex: str  # for example "32011L0061"
    cz_external_id: str  # for example "240/2013" (Collection of Laws reference)
    topic: str  # for example "Alternative Investment Fund Managers"
    notes: str = ""  # context for the reviewing lawyer


# ---------------------------------------------------------------------------
# Curated map: the transposition pairs in the corpus, plus GDPR and its
# supplementing act (see the note below). Extend it when new pairs join the corpus.
# ---------------------------------------------------------------------------
TRANSPOSITION_MAP: tuple[TranspositionPair, ...] = (
    TranspositionPair(
        eu_celex="32011L0061",
        cz_external_id="240/2013",
        topic="Alternative Investment Fund Managers (AIFMD)",
        notes=(
            "Czech Act 240/2013 Sb. (Zákon o investičních společnostech a "
            "investičních fondech) transposes AIFMD. CZ marketing notification "
            "obligations live in §§ 297-320."
        ),
    ),
    TranspositionPair(
        eu_celex="32014L0065",
        cz_external_id="256/2004",
        topic="Markets in Financial Instruments (MiFID II)",
        notes=(
            "Czech Act 256/2004 Sb. (Zákon o podnikání na kapitálovém trhu) "
            "transposes MiFID II. MiFIR (32014R0600) is a directly-applicable "
            "regulation that pairs with MiFID II but has no CZ counterpart."
        ),
    ),
    TranspositionPair(
        eu_celex="32015L0849",
        cz_external_id="253/2008",
        topic="Anti-Money Laundering (5AMLD)",
        notes=(
            "Czech Act 253/2008 Sb. (Zákon o některých opatřeních proti "
            "legalizaci výnosů z trestné činnosti) transposes 4AMLD/5AMLD."
        ),
    ),
    # GDPR is a regulation, not a directive, so it is not transposed. Act
    # 110/2019 Sb. supplements and adapts it in Czech law where the regulation
    # leaves national room (the ÚOOÚ supervisory authority, public-body
    # specifics). The pairing is still recorded so the dashboard can surface the
    # related Czech act, but the note renderer detects the regulation case (see
    # `is_directly_applicable`) and uses regulation wording rather than the
    # "directive transposed by the Czech text" phrasing.
    # 110/2019 Sb. is not yet ingested, so the coverage report shows
    # `loaded: false` for the Czech side. Ingest it before relying on that side.
    TranspositionPair(
        eu_celex="32016R0679",
        cz_external_id="110/2019",
        topic="General Data Protection Regulation (GDPR)",
        notes=(
            "GDPR (32016R0679) is a directly-applicable EU regulation — it is "
            "NOT transposed. Czech Act 110/2019 Sb. (Zákon o zpracování "
            "osobních údajů) is the national act that *supplements* GDPR where "
            "the regulation leaves room for national rules (e.g. the ÚOOÚ "
            "supervisory authority, public-sector specifics). 110/2019 Sb. is "
            "NOT yet ingested — the CZ side shows loaded:false until it is."
        ),
    ),
)

# ---------------------------------------------------------------------------
# Directly applicable EU regulations in the corpus, so the admin dashboard can
# show all three patterns side by side.
#
# GDPR (32016R0679) deliberately appears both here and in TRANSPOSITION_MAP: it is
# directly applicable (so it belongs here), and 110/2019 Sb. supplements it (so the
# pairing belongs there too). `is_directly_applicable` is the single source of truth
# for the regulation-versus-directive distinction, and the note renderer keys off it
# so the GDPR pairing never produces "directive transposed by the Czech text" wording.
# ---------------------------------------------------------------------------
DIRECT_REGULATIONS: tuple[tuple[str, str], ...] = (
    ("32016R0679", "General Data Protection Regulation (GDPR)"),
    ("32014R0600", "Markets in Financial Instruments Regulation (MiFIR)"),
    ("32023R1114", "Markets in Crypto-Assets Regulation (MiCA)"),
)


# ---------------------------------------------------------------------------
# Lookup helpers, deliberately minimal.
# ---------------------------------------------------------------------------
def transposition_for_eu(celex: str) -> TranspositionPair | None:
    """Given an EU CELEX id, return the Czech act that transposes it, if any."""
    for pair in TRANSPOSITION_MAP:
        if pair.eu_celex == celex:
            return pair
    return None


def transposition_for_cz(external_id: str) -> TranspositionPair | None:
    """Given a Czech act's external_id, return the EU directive it transposes."""
    for pair in TRANSPOSITION_MAP:
        if pair.cz_external_id == external_id:
            return pair
    return None


def is_directly_applicable(celex: str) -> bool:
    """Return True if the CELEX id is a directly applicable EU regulation (nothing to transpose)."""
    return any(celex == c for c, _ in DIRECT_REGULATIONS)


# ---------------------------------------------------------------------------
# Coverage report, used by /admin/api/transpositions to show the map with live
# counts so a lawyer or operator can check coverage.
# ---------------------------------------------------------------------------
async def coverage_report() -> dict[str, Any]:
    """Join the curated map to live ``source_document`` and ``provision`` rows.

    Shows how many provisions each side of a pair has. A missing side (for example an EU
    directive not yet ingested) appears as ``n_provisions: null``, the operator's cue to fetch
    the document or remove the row from the map.
    """
    pair_rows: list[dict[str, Any]] = []
    direct_rows: list[dict[str, Any]] = []

    async with session_scope() as session:
        # Collect everything needed in a single round trip.
        ext_ids = (
            [p.eu_celex for p in TRANSPOSITION_MAP]
            + [p.cz_external_id for p in TRANSPOSITION_MAP]
            + [c for c, _ in DIRECT_REGULATIONS]
        )
        rows = (
            await session.execute(
                sql_text(
                    """
                    SELECT sd.external_id,
                           sd.id,
                           sd.title,
                           sd.publisher,
                           sd.legal_hierarchy,
                           COUNT(p.id) AS n_provisions
                      FROM source_document sd
                      LEFT JOIN provision p ON p.source_document_id = sd.id
                     WHERE sd.external_id = ANY(:ext)
                     GROUP BY sd.id
                    """
                ),
                {"ext": ext_ids},
            )
        ).all()

    by_ext: dict[str, dict[str, Any]] = {}
    for r in rows:
        by_ext[r[0]] = {
            "id": str(r[1]),
            "title": r[2],
            "publisher": r[3],
            "legal_hierarchy": r[4],
            "n_provisions": int(r[5] or 0),
        }

    def _side(ext: str | None) -> dict[str, Any]:
        if not ext or ext not in by_ext:
            return {
                "external_id": ext,
                "loaded": False,
                "n_provisions": None,
            }
        info = by_ext[ext]
        return {
            "external_id": ext,
            "loaded": True,
            "source_document_id": info["id"],
            "title": info["title"],
            "publisher": info["publisher"],
            "legal_hierarchy": info["legal_hierarchy"],
            "n_provisions": info["n_provisions"],
        }

    for pair in TRANSPOSITION_MAP:
        pair_rows.append(
            {
                "topic": pair.topic,
                "notes": pair.notes,
                "eu": _side(pair.eu_celex),
                "cz": _side(pair.cz_external_id),
                "both_loaded": (pair.eu_celex in by_ext and pair.cz_external_id in by_ext),
            }
        )

    for celex, label in DIRECT_REGULATIONS:
        direct_rows.append(
            {
                "topic": label,
                "eu": _side(celex),
            }
        )

    return {
        "transposition_pairs": pair_rows,
        "direct_regulations": direct_rows,
        "summary": {
            "pairs_total": len(TRANSPOSITION_MAP),
            "pairs_both_loaded": sum(1 for r in pair_rows if r["both_loaded"]),
            "direct_regulations": len(DIRECT_REGULATIONS),
        },
    }


# ---------------------------------------------------------------------------
# Prompt-context accessor for L2. Returns None for anything not in the curated map,
# by design.
# ---------------------------------------------------------------------------
async def transposition_note_for_provision(
    provision_id: UUID,
) -> str | None:
    """Return a short note on the transposition relationship of the provision's source document.

    Used by L2's applicability prompt to tell the LLM whether the rule binds directly or through
    national law. Returns None if the document is not in the curated map.
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                sql_text(
                    "SELECT sd.external_id "
                    "FROM provision p JOIN source_document sd ON sd.id = p.source_document_id "
                    "WHERE p.id = :pid"
                ),
                {"pid": provision_id},
            )
        ).first()
    if row is None:
        return None
    ext = row[0]
    # Order matters: a directly applicable EU regulation (GDPR 32016R0679) can also
    # appear in TRANSPOSITION_MAP, paired with a supplementing national act
    # (110/2019). Such a regulation must not be called a "directive transposed by
    # the Czech text", since regulations are not transposed. So the EU side checks
    # for a regulation before falling through to the directive wording.
    if is_directly_applicable(ext):
        pair = transposition_for_eu(ext)
        if pair is not None:
            # A directly applicable regulation that also has a supplementing
            # national act (GDPR and 110/2019 Sb.).
            return (
                f"This provision belongs to the EU regulation {ext} "
                f"({pair.topic}), which is directly applicable in the Czech "
                f"Republic — it is NOT transposed. Czech Act "
                f"{pair.cz_external_id} Sb. supplements it where the regulation "
                f"leaves room for national rules; the directly-applicable EU "
                f"regulation remains the primary binding rule."
            )
        # A directly applicable regulation with no supplementing national act.
        return (
            f"This provision belongs to an EU regulation ({ext}) which "
            f"is directly applicable in the Czech Republic — there is no "
            f"separate Czech transposition; the EU text is the binding rule."
        )
    if pair := transposition_for_eu(ext):
        return (
            f"This provision belongs to the EU directive {ext} "
            f"({pair.topic}). It is transposed into Czech law as "
            f"Act {pair.cz_external_id} Sb. — for Czech-jurisdiction "
            f"projects, the Czech text is the binding rule."
        )
    if pair := transposition_for_cz(ext):
        # The Czech side of a regulation and supplementing-act pair (110/2019)
        # supplements rather than transposes; only directives are transposed.
        if is_directly_applicable(pair.eu_celex):
            return (
                f"This provision belongs to Czech Act {ext} Sb., which "
                f"supplements the directly-applicable EU regulation "
                f"{pair.eu_celex} ({pair.topic}) where it leaves room for "
                f"national rules. The EU regulation is directly applicable; "
                f"this Czech act fills the national gaps it permits."
            )
        return (
            f"This provision belongs to Czech Act {ext} Sb., which "
            f"transposes EU directive {pair.eu_celex} ({pair.topic}). "
            f"This is the binding national text for Czech actors."
        )
    return None
