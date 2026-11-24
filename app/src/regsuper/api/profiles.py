"""Business-profile CRUD and profile-driven analysis for SMEs.

A `business_profile` describes a company once (legal form, IČO, NACE code, headcount, activity
flags); each `POST /api/profiles/{id}/analyze` creates a `project` from it and runs the analysis
cascade synchronously. Every route returns 404 unless `profiles_enabled` is set (off by default).
`projects.py` imports neither this module nor `digest`, so importing from it here is cycle-free.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from regsuper.analysis.profile_tags import derive_tags_for_profile
from regsuper.api.auth import get_principal
from regsuper.api.projects import AnalyzeRequest, AnalyzeResponse, project_analyze
from regsuper.config import get_settings
from regsuper.observability.events import emit
from regsuper.storage.database import session_scope
from regsuper.storage.models import BusinessProfile, Project

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


# ---------------------------------------------------------------------------
# Request and response models. legal_form, headcount_band and preferred_language are constrained
# by Literal types; the expected formats of ico (8 digits) and nace_code (4 digits) are not
# validated yet.
# ---------------------------------------------------------------------------
LegalForm = Literal["sro", "as", "osvc", "ks", "vos", "other"]
HeadcountBand = Literal["0", "1-9", "10-49", "50-249", "250+"]
Language = Literal["cs", "en"]


class BusinessProfileIntake(BaseModel):
    """A company profile as submitted by the SME."""

    name: str
    legal_form: LegalForm | None = None
    ico: str | None = None
    nace_code: str | None = None
    headcount_band: HeadcountBand | None = None
    primary_jurisdiction: str | None = None
    preferred_language: Language = "cs"
    # Municipality code from the RÚIAN/ČSÚ gazetteer, validated against it on write.
    municipality_code: str | None = None
    characteristics: dict[str, bool] = Field(default_factory=dict)


class BusinessProfileResponse(BaseModel):
    id: UUID
    name: str
    legal_form: LegalForm | None
    ico: str | None
    nace_code: str | None
    headcount_band: HeadcountBand | None
    primary_jurisdiction: str | None
    preferred_language: Language
    municipality_code: str | None
    sector_tags: list[str]
    activity_tags: list[str]


class ProfilePatch(BaseModel):
    """Partial profile update.

    `force_retag` re-derives the tags even when no sector or characteristic field changed.

    `suggested_only` is set when the user applies a suggestion from the answer page with one
    click ("uložit do profilu", save to profile). A sent scalar field is then written only if the
    profile's current value is empty; a populated field is left untouched, so a suggestion never
    overwrites the company's own data. This is enforced server-side, whatever the UI sends.
    A normal edit leaves it False and overwrites fields.
    """

    name: str | None = None
    legal_form: LegalForm | None = None
    nace_code: str | None = None
    headcount_band: HeadcountBand | None = None
    primary_jurisdiction: str | None = None
    preferred_language: Language | None = None
    municipality_code: str | None = None
    characteristics: dict[str, bool] | None = None
    force_retag: bool = False
    suggested_only: bool = False


class ProfileAnalyzeRequest(BaseModel):
    """Mirrors `projects.py::AnalyzeRequest` so the profile path runs the same cascade.

    Keep the two in sync.
    """

    top_k: int = Field(default=200, ge=1, le=1000)
    limit: int | None = Field(default=None, ge=1, le=1000)
    max_concurrency: int = Field(default=8, ge=1, le=32)
    force: bool = False
    layers: list[Literal["L2", "L3", "L4"]] = Field(default_factory=lambda: ["L2", "L3", "L4"])


class RunAcceptedResponse(BaseModel):
    """The 202 response planned for asynchronous analysis.

    Not returned yet: analysis currently runs synchronously (see the module docstring).
    """

    run_id: UUID
    project_id: UUID
    poll_url: str
    stream_url: str


# ---------------------------------------------------------------------------
# Feature flag (same pattern as api/export.py::_require_enabled)
# ---------------------------------------------------------------------------
def _require_enabled() -> None:
    """Raise 404 unless ``profiles_enabled`` is set (it is off by default).

    A 404 rather than a 403, so a disabled route is indistinguishable from a non-existent one.
    """
    if not get_settings().profiles_enabled:
        raise HTTPException(status_code=404, detail="not found")


# Values the response Literals accept. Used to coerce stored values, so a legacy row with an
# unknown value never fails response validation with a 500.
_RESPONSE_LEGAL_FORMS = {"sro", "as", "osvc", "ks", "vos", "other"}
_RESPONSE_HEADCOUNT_BANDS = {"0", "1-9", "10-49", "50-249", "250+"}

# The five boolean compliance flags on BusinessProfile, set from the `characteristics` dict of
# the intake or patch.
_GATE_NAMES = (
    "processes_personal_data",
    "handles_client_money",
    "has_real_estate",
    "cross_border",
    "is_employer",
)


# Scalar attributes a PATCH may set, mapped from the ProfilePatch field name to the
# BusinessProfile attribute name. The fill-only-if-empty guard reads the current value through
# this map before letting a suggestion fill it. `characteristics` (the boolean flags) is handled
# separately: a bool is never "empty", so a suggestion never touches a flag.
_SCALAR_ATTR_BY_FIELD: dict[str, str] = {
    "name": "display_name",
    "legal_form": "legal_form",
    "nace_code": "nace_code",
    "headcount_band": "headcount_band",
    "primary_jurisdiction": "primary_jurisdiction",
    "preferred_language": "preferred_language",
    "municipality_code": "municipality_code",
}


# ---------------------------------------------------------------------------
# Pure helpers (no database access)
# ---------------------------------------------------------------------------
def _field_is_empty(value: Any) -> bool:
    """Return True if a profile field is empty, so that a suggestion may fill it.

    Empty means None or a blank string. Any other value (a non-blank string, a bool, a number)
    counts as populated.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def _suggestion_may_fill(profile: Any, field: str) -> bool:
    """Return True if a suggestion may set `field` on `profile`.

    Only when `field` is a known scalar attribute whose current value is empty (see
    `_field_is_empty`). A suggestion never overwrites a populated value and never touches the
    boolean flags. Reads the profile; mutates nothing.
    """
    attr = _SCALAR_ATTR_BY_FIELD.get(field)
    if attr is None:
        return False
    return _field_is_empty(getattr(profile, attr, None))


async def _validate_municipality_code(session: Any, code: str | None) -> None:
    """Reject a municipality code that is not in the gazetteer with 422.

    None (unset or cleared) is always allowed. Any other code must exist in the `municipality`
    table, so a typo or stale code is never stored. Runs on the caller's session, inside the
    same transaction as the write.
    """
    if code is None:
        return
    from regsuper.storage.models import Municipality

    hit = await session.get(Municipality, code)
    if hit is None:
        raise HTTPException(
            status_code=422,
            detail=f"unknown municipality_code: {code!r}",
        )


def _jurisdiction_scope(primary: str | None) -> list[str]:
    """Map a profile `primary_jurisdiction` to a project `jurisdiction_scope` array."""
    m = {"CZ": ["CZ"], "EU": ["EU"], "CZ+EU": ["CZ", "EU"]}
    return m.get(primary or "CZ", ["CZ"])


def build_project_from_profile(profile: Any, *, fallback_org_id: UUID) -> Project:
    """Build (without persisting) a Project linked to the profile. Deterministic."""
    from regsuper.storage.models import Project

    sector_tags, activity_tags = derive_tags_for_profile(profile)
    return Project(
        org_id=(getattr(profile, "org_id", None) or fallback_org_id),
        business_profile_id=profile.id,
        name=profile.display_name,
        sector=(profile.sector_override or (sector_tags[0] if sector_tags else "general")),
        jurisdiction_scope=_jurisdiction_scope(getattr(profile, "primary_jurisdiction", None)),
        entity_type=getattr(profile, "legal_form", None),
        analysis_depth=getattr(profile, "analysis_depth", None) or "standard",
        metadata_={
            "sector_tags": sector_tags,
            "activity_tags": activity_tags,
            "source": "business_profile",
            "business_profile_id": str(profile.id),
            "intake_version": 1,
        },
    )


def _coerce_legal_form(value: str | None) -> str | None:
    """Return `value` only if it is in the response LegalForm vocab, else None."""
    return value if value in _RESPONSE_LEGAL_FORMS else None


def _coerce_headcount_band(value: str | None) -> str | None:
    """Return `value` only if it is in the response HeadcountBand vocab, else None."""
    return value if value in _RESPONSE_HEADCOUNT_BANDS else None


def _to_response(profile: BusinessProfile) -> BusinessProfileResponse:
    """Map a BusinessProfile row to the response model.

    `legal_form` and `headcount_band` become None when the stored value is outside the response
    vocabulary, so a legacy row (for example `osvč` or `2-9`) never fails validation with a 500.
    """
    return BusinessProfileResponse(
        id=profile.id,
        name=profile.display_name,
        legal_form=_coerce_legal_form(getattr(profile, "legal_form", None)),
        ico=getattr(profile, "ico", None),
        nace_code=getattr(profile, "nace_code", None),
        headcount_band=_coerce_headcount_band(getattr(profile, "headcount_band", None)),
        primary_jurisdiction=getattr(profile, "primary_jurisdiction", None),
        preferred_language=getattr(profile, "preferred_language", None) or "cs",
        municipality_code=getattr(profile, "municipality_code", None),
        sector_tags=list(getattr(profile, "derived_sector_tags", None) or []),
        activity_tags=list(getattr(profile, "derived_activity_tags", None) or []),
    )


def _is_deleted(profile: Any) -> bool:
    """True when the profile carries a soft-delete marker (`metadata.deleted_at`)."""
    return (getattr(profile, "metadata_", None) or {}).get("deleted_at") is not None


def _apply_tags(profile: BusinessProfile) -> None:
    """Re-derive and overwrite the tag columns on `profile` (only written at intake)."""
    sector_tags, activity_tags = derive_tags_for_profile(profile)
    profile.derived_sector_tags = sector_tags
    profile.derived_activity_tags = activity_tags


# ---------------------------------------------------------------------------
# Routes (all return 404 unless profiles_enabled is set)
# ---------------------------------------------------------------------------
@router.post("", response_model=BusinessProfileResponse, status_code=201)
async def create_profile(
    intake: BusinessProfileIntake, request: Request
) -> BusinessProfileResponse:
    """Create a company profile and derive its sector and activity tags.

    The profile belongs to the caller's organization. Returns 422 for an unknown municipality code.
    """
    _require_enabled()
    p = await get_principal(request)
    profile = BusinessProfile(
        org_id=p.org_id,
        display_name=intake.name,
        legal_form=intake.legal_form or "other",
        ico=intake.ico,
        nace_code=intake.nace_code,
        headcount_band=intake.headcount_band or "1-9",
        primary_jurisdiction=intake.primary_jurisdiction or "CZ",
        preferred_language=intake.preferred_language,
        municipality_code=intake.municipality_code,
        processes_personal_data=bool(intake.characteristics.get("processes_personal_data", False)),
        handles_client_money=bool(intake.characteristics.get("handles_client_money", False)),
        has_real_estate=bool(intake.characteristics.get("has_real_estate", False)),
        cross_border=bool(intake.characteristics.get("cross_border", False)),
        is_employer=bool(intake.characteristics.get("is_employer", False)),
    )
    _apply_tags(profile)

    async with session_scope() as session:
        await _validate_municipality_code(session, intake.municipality_code)
        session.add(profile)
        await session.flush()  # populate profile.id for the event and the response
        pid = profile.id
        resp = _to_response(profile)

    await emit(
        "profile.created",
        module="polarius",
        profile_id=str(pid),
        name=intake.name,
    )
    return resp


@router.get("/{profile_id}", response_model=BusinessProfileResponse)
async def get_profile(profile_id: UUID, request: Request) -> BusinessProfileResponse:
    """Return one profile. Returns 404 if it does not exist or was deleted."""
    _require_enabled()
    await get_principal(request)
    async with session_scope() as session:
        profile = await session.get(BusinessProfile, profile_id)
        if profile is None or _is_deleted(profile):
            raise HTTPException(status_code=404, detail="profile not found")
        return _to_response(profile)


@router.patch("/{profile_id}", response_model=BusinessProfileResponse)
async def patch_profile(
    profile_id: UUID, patch: ProfilePatch, request: Request
) -> BusinessProfileResponse:
    """Update the fields present in the request body.

    Tags are re-derived when a sector or characteristic field changed, or when `force_retag` is
    set. With `suggested_only`, fields that already have a value are left unchanged. Returns 404
    for a missing or deleted profile and 422 for an unknown municipality code.
    """
    _require_enabled()
    await get_principal(request)
    async with session_scope() as session:
        profile = await session.get(BusinessProfile, profile_id)
        if profile is None or _is_deleted(profile):
            raise HTTPException(status_code=404, detail="profile not found")

        # Only overwrite the attributes the caller actually sent (exclude_unset).
        fields = patch.model_dump(exclude_unset=True)

        # Suggestion guard: under `suggested_only` a field is filled only when it is currently
        # empty, never overwriting the company's own value. Enforced here, not only in the UI,
        # so a crafted request cannot overwrite a populated field. Skipped fields are silently
        # ignored; `applied` tracks what actually changed, so a suggestion that changes nothing
        # does not trigger a re-tag.
        suggested = bool(patch.suggested_only)

        def _may_set(field: str) -> bool:
            # A normal (self/operator) edit always applies; a suggestion applies only
            # when the target field is currently empty (and never a boolean gate).
            return (not suggested) or _suggestion_may_fill(profile, field)

        applied: set[str] = set()
        if "name" in fields and _may_set("name"):
            profile.display_name = patch.name
            applied.add("name")
        if "legal_form" in fields and _may_set("legal_form"):
            profile.legal_form = patch.legal_form or "other"
            applied.add("legal_form")
        if "nace_code" in fields and _may_set("nace_code"):
            profile.nace_code = patch.nace_code
            applied.add("nace_code")
        if "headcount_band" in fields and _may_set("headcount_band"):
            profile.headcount_band = patch.headcount_band or "1-9"
            applied.add("headcount_band")
        if "primary_jurisdiction" in fields and _may_set("primary_jurisdiction"):
            profile.primary_jurisdiction = patch.primary_jurisdiction or "CZ"
            applied.add("primary_jurisdiction")
        if "preferred_language" in fields and _may_set("preferred_language"):
            profile.preferred_language = patch.preferred_language or "cs"
            applied.add("preferred_language")
        if "municipality_code" in fields and _may_set("municipality_code"):
            await _validate_municipality_code(session, patch.municipality_code)
            profile.municipality_code = patch.municipality_code
            applied.add("municipality_code")
        # A suggestion never touches the boolean flags (a bool is never "empty", so there is
        # nothing to fill); only a normal edit changes them.
        if not suggested and "characteristics" in fields and patch.characteristics is not None:
            for gate in _GATE_NAMES:
                setattr(profile, gate, bool(patch.characteristics.get(gate, False)))
            applied.add("characteristics")

        tag_inputs_changed = bool({"nace_code", "legal_form", "characteristics"} & applied)
        if tag_inputs_changed or patch.force_retag:
            _apply_tags(profile)

        profile.updated_at = datetime.now(UTC)
        resp = _to_response(profile)

    await emit("profile.updated", module="polarius", profile_id=str(profile_id))
    return resp


@router.delete("/{profile_id}", status_code=204)
async def delete_profile(profile_id: UUID, request: Request) -> None:
    """Soft-delete a profile by stamping `metadata.deleted_at`.

    Deleting an already-deleted profile returns 404, because it is no longer visible.
    """
    _require_enabled()
    await get_principal(request)
    async with session_scope() as session:
        profile = await session.get(BusinessProfile, profile_id)
        if profile is None or _is_deleted(profile):
            raise HTTPException(status_code=404, detail="profile not found")
        # Reassign the dict so SQLAlchemy detects the JSONB change.
        profile.metadata_ = {
            **(profile.metadata_ or {}),
            "deleted_at": datetime.now(UTC).isoformat(),
        }

    await emit("profile.deleted", module="polarius", profile_id=str(profile_id))
    return None


@router.post("/{profile_id}/analyze", response_model=AnalyzeResponse)
async def analyze_profile(
    profile_id: UUID,
    request: Request,
    req: ProfileAnalyzeRequest | None = None,
) -> AnalyzeResponse:
    """Create a project from the profile and run the analysis cascade on it synchronously.

    Returns the same `AnalyzeResponse` as `POST /api/projects/{id}/analyze`, which this endpoint
    calls directly. The new project has no declared intent, so the input compliance gate does
    not apply to profile runs. Returns 404 for a missing or deleted profile.
    """
    _require_enabled()
    p = await get_principal(request)
    async with session_scope() as session:
        profile = await session.get(BusinessProfile, profile_id)
        if profile is None or _is_deleted(profile):
            raise HTTPException(status_code=404, detail="profile not found")
        project = build_project_from_profile(profile, fallback_org_id=p.org_id)
        session.add(project)
        await session.flush()
        project_id = project.id

    # Run the cascade only after the session block has committed the project.
    areq = AnalyzeRequest(**(req.model_dump() if req is not None else {}))
    return await project_analyze(project_id, areq)
