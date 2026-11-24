"""Conduit OAuth scopes: constants, the scopes each tool requires, and the entitlement projection.

The authorization server embeds these scopes when it mints a token (from
`entitlements.scopes_for_org(org)`); `auth.require_scope` checks that the token holds every
scope the tool needs (otherwise 403); and `meter.precheck` re-resolves the entitlement on every
call, because a token can outlive a plan downgrade.

Scopes:

* `corpus.read`: search_czech_law, get_provision, whats_in_force and both resources
* `obligations.read`: list_obligations (in addition to `corpus.read`)
* `compliance.check`: check_compliance (in addition to `corpus.read`)
* `answer.generate`: answer_legal_question (top plan only; in addition to `corpus.read`)

The six tool names are fixed. There is deliberately no case-law scope or tool: Conduit serves
legislation and obligations only.
"""

from __future__ import annotations

from enum import StrEnum

_WP = "WP-062 (Stage-2 Conduit build)"

# --- OAuth scope strings -----------------------------------------------------
SCOPE_CORPUS_READ = "corpus.read"
SCOPE_OBLIGATIONS_READ = "obligations.read"
SCOPE_COMPLIANCE_CHECK = "compliance.check"
SCOPE_ANSWER_GENERATE = "answer.generate"

ALL_SCOPES: frozenset[str] = frozenset(
    {
        SCOPE_CORPUS_READ,
        SCOPE_OBLIGATIONS_READ,
        SCOPE_COMPLIANCE_CHECK,
        SCOPE_ANSWER_GENERATE,
    }
)


class ConduitTool(StrEnum):
    """The six exposed tools. There is deliberately no case-law tool."""

    SEARCH_CZECH_LAW = "search_czech_law"
    GET_PROVISION = "get_provision"
    WHATS_IN_FORCE = "whats_in_force"
    LIST_OBLIGATIONS = "list_obligations"
    CHECK_COMPLIANCE = "check_compliance"
    ANSWER_LEGAL_QUESTION = "answer_legal_question"


# Scopes each tool requires. Every tool needs corpus.read; the heavier tools add one more.
TOOL_REQUIRED_SCOPES: dict[ConduitTool, frozenset[str]] = {
    ConduitTool.SEARCH_CZECH_LAW: frozenset({SCOPE_CORPUS_READ}),
    ConduitTool.GET_PROVISION: frozenset({SCOPE_CORPUS_READ}),
    ConduitTool.WHATS_IN_FORCE: frozenset({SCOPE_CORPUS_READ}),
    ConduitTool.LIST_OBLIGATIONS: frozenset({SCOPE_CORPUS_READ, SCOPE_OBLIGATIONS_READ}),
    ConduitTool.CHECK_COMPLIANCE: frozenset({SCOPE_CORPUS_READ, SCOPE_COMPLIANCE_CHECK}),
    ConduitTool.ANSWER_LEGAL_QUESTION: frozenset({SCOPE_CORPUS_READ, SCOPE_ANSWER_GENERATE}),
}


def scopes_for_tool(tool: ConduitTool) -> frozenset[str]:
    """Return the scopes a token must hold to call `tool` (otherwise the call gets a 403).

    `TOOL_REQUIRED_SCOPES` is the source of truth; this is a pure lookup over it, used by
    `auth.require_scope`. An unknown tool fails closed: it requires the full scope set, so an
    unmapped tool can never be reached without every scope. (An empty set would grant it to
    everyone.)
    """
    return TOOL_REQUIRED_SCOPES.get(tool, ALL_SCOPES)


def project_entitlement_scopes(org_scopes: set[str]) -> set[str]:
    """Project an org's entitlement scopes onto the Conduit scope vocabulary.

    Takes the output of `entitlements.scopes_for_org(org)`, the single authority for mapping
    tiers to scopes, and intersects it with `ALL_SCOPES`. Any granted scope that is not a
    Conduit scope is dropped, so Conduit never mints a scope it does not itself define. An
    empty input (an org without API access, or one that was downgraded) projects to an empty
    set, and that org cannot mint a token.

    Pure and fail closed: no database access and no settings read. Resolving the scopes is the
    caller's job.
    """
    return {s for s in (org_scopes or set()) if s in ALL_SCOPES}


__all__ = [
    "ALL_SCOPES",
    "SCOPE_ANSWER_GENERATE",
    "SCOPE_COMPLIANCE_CHECK",
    "SCOPE_CORPUS_READ",
    "SCOPE_OBLIGATIONS_READ",
    "TOOL_REQUIRED_SCOPES",
    "ConduitTool",
    "project_entitlement_scopes",
    "scopes_for_tool",
]
