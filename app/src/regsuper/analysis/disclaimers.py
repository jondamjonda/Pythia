"""The disclaimer registry: versioned disclaimer texts kept as data.

Every answer mode carries a disclaimer, and a finding stores only the disclaimer's stable id. This
module holds the in-code registry of those texts (Czech and English), a pure lookup, and
``seed_disclaimers``, which copies the registry into the ``disclaimer`` table created by migration
``012_compliance.sql``. Nothing touches the database at import, since the table may not exist yet.

Stable ids (matching the migration's ``-- seed:`` comment and the ``Disclaimer.id`` TEXT primary
key in ``storage/models.py``):

* ``info.v1``: answer mode ``information``, confidence ``normal``.
* ``info.low_confidence.v1``: answer mode ``information``, confidence ``low``.
* ``refusal.reserved_advice.v1``: answer mode ``reserved_advice``, confidence ``normal``.

Every entry has non-empty Czech and English text, so ``get_disclaimer`` never returns an empty
disclaimer. The wording is provisional until the reviewing lawyer finalises it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy import text as sql_text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Internal tracking tag for the consuming component; not used at runtime.
_CONSUMING_WP: Final[str] = "B2b"

# Mirrors the closed answer-mode vocabulary (see ``compliance_guard.AnswerMode`` and
# ``Disclaimer.answer_mode`` / ``Finding.answer_mode`` in ``storage/models.py``).
AnswerMode = Literal["information", "reserved_advice", "needs_review"]
Confidence = Literal["normal", "low"]


@dataclass(frozen=True)
class DisclaimerBlock:
    """One immutable disclaimer version, the in-code mirror of a ``disclaimer`` row.

    A finding stores only ``disclaimer_id``; with the id and this registry one can
    reconstruct exactly which disclaimer text and answer mode applied. Frozen, so a
    block is a stable, hashable reference value.
    """

    id: str  # stable version id, for example 'info.v1'
    answer_mode: AnswerMode  # the answer mode this disclaimer accompanies
    confidence: Confidence  # 'normal' or 'low' (the heightened-caveat variant)
    text_cs: str  # Czech text (primary product language)
    text_en: str  # English text


# Stable id constants: the single source of truth for the ids used by
# compliance_guard, render and the database seed.
DISCLAIMER_INFO: Final[str] = "info.v1"
DISCLAIMER_INFO_LOW_CONFIDENCE: Final[str] = "info.low_confidence.v1"
DISCLAIMER_RESERVED_ADVICE: Final[str] = "refusal.reserved_advice.v1"


# The in-code registry. Public reference data: the ``disclaimer`` table has no
# row-level security. A plain module-level constant, so no database access at import.
# Provisional wording until the reviewing lawyer finalises it.
DISCLAIMERS: Final[dict[str, DisclaimerBlock]] = {
    DISCLAIMER_INFO: DisclaimerBlock(
        id=DISCLAIMER_INFO,
        answer_mode="information",
        confidence="normal",
        text_cs=(
            "Toto je obecná právní informace, nikoli právní rada k vašemu "
            "konkrétnímu případu. Pro závazné posouzení se obraťte na advokáta."
        ),
        text_en=(
            "This is general legal information, not legal advice for your "
            "specific situation. For a binding assessment, consult a lawyer."
        ),
    ),
    DISCLAIMER_INFO_LOW_CONFIDENCE: DisclaimerBlock(
        id=DISCLAIMER_INFO_LOW_CONFIDENCE,
        answer_mode="information",
        confidence="low",
        text_cs=(
            "Toto je obecná právní informace s nižší mírou jistoty — výsledek "
            "nebyl ověřen právníkem. Nejde o právní radu; ověřte u advokáta."
        ),
        text_en=(
            "This is general legal information with lower confidence — it has "
            "not been reviewed by a lawyer. Not legal advice; verify with a lawyer."
        ),
    ),
    # Per the advocate's determination, the only reserved class is the product acting on behalf of
    # the user (representing them, or filing, submitting or suing for them). The refusal copy must
    # say exactly that ("we cannot act for you"), not a broad "this needs legal advice": summaries,
    # procedures, drafting and advice on the user's own matter are information and carry the
    # information disclaimer. Provisional wording until the lawyer finalises it.
    DISCLAIMER_RESERVED_ADVICE: DisclaimerBlock(
        id=DISCLAIMER_RESERVED_ADVICE,
        answer_mode="reserved_advice",
        confidence="normal",
        text_cs=(
            "Tento dotaz žádá, abychom za vás jednali nebo vás zastupovali — "
            "podali podání či vás zastoupili v řízení. To smí pouze advokát; "
            "naše služba to dělat nemůže. Můžeme vás propojit s partnerským advokátem."
        ),
        text_en=(
            "This request asks us to act on your behalf or represent you — to file "
            "a submission or appear for you. Only a lawyer may do that; our service "
            "cannot. We can connect you with a partner lawyer."
        ),
    ),
}


def get_disclaimer(disclaimer_id: str) -> DisclaimerBlock:
    """Look up a :class:`DisclaimerBlock` by its stable id.

    A pure dictionary lookup over the in-code ``DISCLAIMERS`` registry (no database).

    Raises:
        KeyError: For an unknown id, so a typo never silently surfaces an empty disclaimer.
    """
    return DISCLAIMERS[disclaimer_id]


def list_disclaimer_ids() -> list[str]:
    """Return the stable ids registered in :data:`DISCLAIMERS`.

    No database access. The order follows the registry's insertion order, so
    callers (the seed below, or a registry-versus-table parity check) get a
    deterministic list. Every id returned here must seed exactly one
    ``disclaimer`` row, and the table must hold no id this list omits.
    """
    return list(DISCLAIMERS.keys())


# ---------------------------------------------------------------------------
# Database seed: idempotent UPSERT into the ``disclaimer`` table (migration 012).
#
# Not run at import (the table may not exist, and there is no database at import
# time). A caller (a CLI, a startup hook or a test) invokes
# ``seed_disclaimers(session)`` once the migration is applied. The table is public
# reference data without row-level security, so this needs no org setting and is
# safe to run from any session.
# ---------------------------------------------------------------------------

# Raw UPSERT, in the same raw-``sql_text`` style as ``ingest/tagging.py``'s
# ``_apply_tags``. INSERT ... ON CONFLICT DO UPDATE overwrites every column except
# the primary key and ``created_at`` from the in-code registry, so seeded rows can
# never drift from the code constants and a re-run changes nothing. ``is_active``
# is forced TRUE for every registered id (the registry is the live set);
# ``created_at`` keeps its original DEFAULT now() value on conflict, preserving the
# row's first-seen time.
_SEED_UPSERT_SQL: Final[str] = """
    INSERT INTO disclaimer (id, answer_mode, confidence, text_cs, text_en, is_active)
    VALUES (:id, :answer_mode, :confidence, :text_cs, :text_en, TRUE)
    ON CONFLICT (id) DO UPDATE SET
        answer_mode = EXCLUDED.answer_mode,
        confidence  = EXCLUDED.confidence,
        text_cs     = EXCLUDED.text_cs,
        text_en     = EXCLUDED.text_en,
        is_active   = TRUE
"""


async def seed_disclaimers(session: AsyncSession) -> int:
    """Idempotently UPSERT every :data:`DISCLAIMERS` entry into ``disclaimer``.

    One row per registry id, keyed on the TEXT primary key, so a second call
    overwrites in place rather than duplicating; running it twice leaves exactly
    the same rows. Returns the number of registry entries upserted.

    The caller owns the transaction (commit or rollback). This function only
    issues the UPSERTs on the supplied ``session`` and opens no session of its
    own. The table is public reference data, so no row-level security or org
    setting is required.
    """
    stmt = sql_text(_SEED_UPSERT_SQL)
    n = 0
    for block in DISCLAIMERS.values():
        await session.execute(
            stmt,
            {
                "id": block.id,
                "answer_mode": block.answer_mode,
                "confidence": block.confidence,
                "text_cs": block.text_cs,
                "text_en": block.text_en,
            },
        )
        n += 1
    return n
