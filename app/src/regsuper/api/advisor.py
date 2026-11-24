"""Advisor (účetní, accountant) multi-client workspace: a stub, not yet mounted in `api/main.py`.

The workspace will let an accountant act for many client business profiles: list linked clients,
view a client's digest and see a dashboard of who needs attention. Only the router and the typed
shapes exist; every endpoint answers 501 and no table is read or written.

Cross-client access before row-level security is enforced (migration 015) would leak data across
tenants, so the live workspace must stay behind a default-off flag (`advisor_workspace_enabled`)
until then, and also require `require_entitlement(feature='multi_client')`.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/advisor", tags=["advisor"])


class AdvisorResponse(BaseModel):
    id: UUID
    display_name: str
    email: str


class AdvisorRegisterRequest(BaseModel):
    display_name: str
    email: str


class LinkClientRequest(BaseModel):
    business_profile_id: UUID


class AdvisorClientSummary(BaseModel):
    """One linked client, with the at-a-glance counts the dashboard ranks on.

    Intended to be computed by a single CTE or lateral join; a materialized view is not needed
    at this scale.
    """

    business_profile_id: UUID
    name: str
    n_obligations: int = 0
    n_gaps: int = 0
    n_critical_actions: int = 0
    last_analyzed_at: datetime | None = None


class AdvisorClientListResponse(BaseModel):
    clients: list[AdvisorClientSummary] = Field(default_factory=list)
    total: int = 0


class AdvisorDashboardResponse(BaseModel):
    n_clients: int = 0
    n_obligations_total: int = 0
    n_gaps_total: int = 0
    clients_needing_attention: list[AdvisorClientSummary] = Field(default_factory=list)


def _not_implemented() -> HTTPException:
    return HTTPException(status_code=501, detail="advisor endpoint not implemented — WP-050")


@router.post("", response_model=AdvisorResponse)
async def register_advisor(req: AdvisorRegisterRequest) -> AdvisorResponse:
    """Register the current user as an advisor. Not implemented yet (501)."""
    raise _not_implemented()


@router.get("/me", response_model=AdvisorResponse)
async def advisor_me() -> AdvisorResponse:
    """Return the current advisor. Not implemented yet (501)."""
    raise _not_implemented()


@router.get("/clients", response_model=AdvisorClientListResponse)
async def list_clients(
    skip: int = 0,
    limit: int = 50,
    search: str | None = None,
) -> AdvisorClientListResponse:
    """List linked client profiles. Not implemented yet (501)."""
    raise _not_implemented()


@router.post("/clients", status_code=201)
async def link_client(req: LinkClientRequest) -> None:
    """Link a business profile to this advisor. Not implemented yet (501)."""
    raise _not_implemented()


@router.delete("/clients/{profile_id}", status_code=204)
async def unlink_client(profile_id: UUID) -> None:
    """Unlink a client profile. Not implemented yet (501)."""
    raise _not_implemented()


@router.get("/clients/{profile_id}/digest")
async def client_digest(profile_id: UUID) -> dict:
    """Return the full digest for one client (via digest.py). Not implemented yet (501)."""
    raise _not_implemented()


@router.get("/clients/{profile_id}/profile")
async def client_profile(profile_id: UUID) -> dict:
    """Return one client's business profile. Not implemented yet (501)."""
    raise _not_implemented()


@router.get("/dashboard", response_model=AdvisorDashboardResponse)
async def dashboard() -> AdvisorDashboardResponse:
    """Return aggregate counts and the clients needing attention. Not implemented yet (501).

    A client needs attention when it has at least one critical action.
    """
    raise _not_implemented()
