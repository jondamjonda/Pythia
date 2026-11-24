"""Conduit REST router: one endpoint per tool, mirroring the MCP catalogue.

This is the API-key channel. It shares `schemas.py`, the envelope and the meter with the MCP
channel, is mounted under `/v1/*` by `conduit/app.py` only, and never mounts `/admin/*` or
`/internal/*` routes (trust-boundary isolation). The OAuth protected-resource metadata
endpoint (`GET /.well-known/oauth-protected-resource`) is public and lives in `app.py`.

Paths, all under the Conduit host's `/v1`:

    POST /v1/search            search_czech_law
    POST /v1/provision         get_provision
    POST /v1/whats-in-force    whats_in_force
    POST /v1/obligations       list_obligations
    POST /v1/check-compliance  check_compliance
    POST /v1/answer            answer_legal_question

The router exists at import time but has no routes yet: the handlers below are signatures
whose bodies raise `NotImplementedError`, and none is registered with `@router.post`. The
main app process never imports this module.
"""

from __future__ import annotations

from fastapi import APIRouter

from regsuper.conduit.envelope import Envelope
from regsuper.conduit.schemas import (
    AnswerLegalQuestionInput,
    CheckComplianceInput,
    GetProvisionInput,
    ListObligationsInput,
    SearchCzechLawInput,
    WhatsInForceInput,
)

_WP = "WP-062 (Stage-2 Conduit build)"

# Empty for now; the handlers below are to be registered here with their auth, tenancy, rate
# limit and metering dependencies. Included by conduit/app.py only, never by api/main.py. The
# tag groups these endpoints in Conduit's OpenAPI schema.
router = APIRouter(prefix="/v1", tags=["conduit"])


async def search(req: SearchCzechLawInput) -> Envelope:
    """Handle POST /v1/search (search_czech_law). Not implemented yet."""
    raise NotImplementedError(_WP)


async def provision(req: GetProvisionInput) -> Envelope:
    """Handle POST /v1/provision (get_provision); the provision is its own citation.

    Not implemented yet.
    """
    raise NotImplementedError(_WP)


async def whats_in_force(req: WhatsInForceInput) -> Envelope:
    """Handle POST /v1/whats-in-force (whats_in_force). Not implemented yet."""
    raise NotImplementedError(_WP)


async def obligations(req: ListObligationsInput) -> Envelope:
    """Handle POST /v1/obligations (list_obligations). Not implemented yet.

    Returns corpus-level rules only and does not run the blocking analysis cascade.
    """
    raise NotImplementedError(_WP)


async def check_compliance(req: CheckComplianceInput) -> Envelope:
    """Handle POST /v1/check-compliance (check_compliance). Not implemented yet.

    The compliance pre-gate is mandatory: a situation that asks for reserved legal advice is
    refused without retrieval (and still metered).
    """
    raise NotImplementedError(_WP)


async def answer(req: AnswerLegalQuestionInput) -> Envelope:
    """Handle POST /v1/answer (answer_legal_question). Not implemented yet.

    Both compliance gates (before and after generation) are mandatory, and the caller needs
    the `answer.generate` scope (top plan only).
    """
    raise NotImplementedError(_WP)


__all__ = [
    "answer",
    "check_compliance",
    "obligations",
    "provision",
    "router",
    "search",
    "whats_in_force",
]
