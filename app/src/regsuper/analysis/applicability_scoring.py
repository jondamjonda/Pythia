"""Applicability scoring: the pass between L3 and L4 that marks which obligations bind the client.

A thin, additive, idempotent pass that runs after L3 (so every obligation finding exists) and before
L4. For each ``finding_type='obligation'`` row it reads the extracted ``payload['actor']``,
``payload['modal']``, ``statement`` and ``confidence_overall``, runs the pure ``applicability`` core
(no LLM and no prompt, so the L2/L3 prompt-hash chain is untouched), and merges a
``payload['applicability']`` verdict into the row's JSONB.

It never deletes a finding and never changes ``finding_type``, ``severity``, ``statement``,
``cited_*`` or ``prompt_hash``; the only change to a row is the added payload key. The orchestrator
calls it only when ``entity_scoping_enabled`` is on. An ``out_of_scope`` verdict is a rendering
concern (the digest moves it to a visible "does not bind you directly" section); suppressing L4
gaps for such findings is a separate, lawyer-gated switch and is deliberately not wired here.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from regsuper.analysis.applicability import (
    addressee_class,
    decide_binding,
    obligation_type,
    project_entity_role,
)
from regsuper.analysis.applicability_confidence import applicability_confidence_basis
from regsuper.analysis.sme_roles import sme_role_dict_from_profile
from regsuper.config import get_settings
from regsuper.observability.events import emit
from regsuper.storage.database import session_scope
from regsuper.storage.models import BusinessProfile, Finding, Project


@dataclass(frozen=True)
class ApplicabilityScoringReport:
    project_id: UUID
    n_obligations: int
    n_scored: int
    n_skipped_existing: int
    n_yes: int
    n_conditional: int
    n_out_of_scope: int
    project_role: str


def _resolve_role(project: Project | None, sme_profile=None, *, sme_enabled: bool = False) -> dict:
    """Return the project's structured entity role.

    Prefers the role written at intake; otherwise derives one now from the project's columns. A
    missing or blank entity_type yields an ``assumed`` role, so the gate keeps every finding.

    SME lane (``sme_enabled``, from the ``sme_roles_enabled`` flag): the finance vocabulary is
    always resolved first. The SME lane is consulted only as a fallback, when the finance role came
    back ``assumed`` (``primary='unknown'``, which is where a bakery or a small s.r.o. lands) and a
    linked business profile was loaded. A genuine finance entity therefore always keeps its finance
    role; only businesses the finance vocabulary cannot place pick up the SME role. If the SME lane
    resolves nothing either (an empty profile gives an ``assumed`` SME dict), it is discarded and
    the finance ``assumed`` role stands, so the gate still keeps everything.
    """
    stored = (getattr(project, "metadata_", None) or {}).get("entity_role") if project else None
    if isinstance(stored, dict) and stored.get("primary"):
        return stored
    finance_role = project_entity_role(
        getattr(project, "entity_type", None) if project else None,
        getattr(project, "declared_intent", None) if project else None,
    )
    if sme_enabled and finance_role.get("assumed") and sme_profile is not None:
        sme_role = sme_role_dict_from_profile(sme_profile)
        if not sme_role.get("assumed"):  # only override when the SME lane actually resolved a role
            return sme_role
    return finance_role


async def _load_sme_profile(session, project: Project | None, *, sme_enabled: bool):
    """Load the linked BusinessProfile for the SME lane.

    Only loads when the flag is on and the project carries a ``business_profile_id``. Returns
    ``None`` otherwise, so with the flag off the pass does no extra database work.
    """
    if not sme_enabled or project is None:
        return None
    bp_id = getattr(project, "business_profile_id", None)
    if bp_id is None:
        return None
    return await session.get(BusinessProfile, bp_id)


async def run_applicability_scoring(
    project_id: UUID, *, force: bool = False
) -> ApplicabilityScoringReport:
    """Stamp ``payload['applicability']`` on every obligation finding for the project.

    Idempotent: a finding that already carries the key is skipped unless ``force``. Returns a small
    report. A bad actor string never raises: unparseable actors classify as ``unknown`` and the
    guard keeps them.
    """
    settings = get_settings()
    floor = float(settings.entity_scoping_floor)
    confidence_enabled = bool(getattr(settings, "applicability_confidence_enabled", False))
    sme_enabled = bool(getattr(settings, "sme_roles_enabled", False))

    counts = {"yes": 0, "conditional": 0, "out_of_scope": 0}
    n_obl = n_scored = n_skipped = 0
    role: dict = {}

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        sme_profile = await _load_sme_profile(session, project, sme_enabled=sme_enabled)
        role = _resolve_role(project, sme_profile, sme_enabled=sme_enabled)

        rows = (
            (
                await session.execute(
                    select(Finding).where(
                        Finding.project_id == project_id,
                        Finding.finding_type == "obligation",
                    )
                )
            )
            .scalars()
            .all()
        )
        n_obl = len(rows)

        for finding in rows:
            payload = dict(finding.payload or {})
            if not force and "applicability" in payload:
                n_skipped += 1
                continue
            addressee = addressee_class(payload.get("actor"))
            otype = obligation_type(addressee, payload.get("modal"), finding.statement)
            verdict = decide_binding(
                role,
                addressee,
                otype,
                binds_confidence=float(finding.confidence_overall or 1.0),
                floor=floor,
            )
            # Optionally (applicability_confidence_enabled, off by default) annotate the verdict
            # with the applicability-confidence basis, label and reason. This rates how legally
            # solid the verdict is, independently of extraction confidence, and never changes
            # `binds`. It is merged into the same dict, so export and render surface it with no
            # extra plumbing.
            if confidence_enabled:
                cv = applicability_confidence_basis(
                    addressee,
                    otype,
                    verdict["binds"],
                    extraction_confidence=finding.confidence_overall,
                    project_role=role,
                )
                verdict["confidence_basis"] = cv["basis"]
                verdict["confidence_label"] = cv["label"]
                verdict["confidence_reason"] = cv["reason"]
            payload["applicability"] = verdict
            finding.payload = payload  # reassign so SQLAlchemy tracks the JSONB mutation
            counts[verdict["binds"]] += 1
            n_scored += 1
        # session_scope commits on clean exit

    await emit(
        "analysis.applicability.scored",
        module="polarius",
        level="info",
        pid=str(project_id),
        n_scored=n_scored,
        n_skipped=n_skipped,
        yes=counts["yes"],
        conditional=counts["conditional"],
        out_of_scope=counts["out_of_scope"],
        project_role=str(role.get("primary")),
    )
    return ApplicabilityScoringReport(
        project_id=project_id,
        n_obligations=n_obl,
        n_scored=n_scored,
        n_skipped_existing=n_skipped,
        n_yes=counts["yes"],
        n_conditional=counts["conditional"],
        n_out_of_scope=counts["out_of_scope"],
        project_role=str(role.get("primary")),
    )
