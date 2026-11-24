"""Conduit MCP server: the tool, resource and prompt registry over Streamable HTTP.

Registers six tools, two resources and two prompts on the official `mcp` Python SDK, mounted
as an ASGI sub-app inside `conduit/app.py`. The STDIO transport is not offered: this is a
remote, multi-tenant product, and STDIO would bypass authentication and metering.

* Tools (one per `scopes.ConduitTool`): search_czech_law, get_provision, whats_in_force,
  list_obligations, check_compliance, answer_legal_question.
* Resources (the cheapest unit, `corpus.read`): conduit://corpus/overview and
  conduit://act/{external_id}.
* Prompts (which carry the safety contract to the host): cited_legal_lookup(topic, as_of?) and
  obligation_briefing(sector, entity_type, as_of?).

There is deliberately no case-law or "find precedents" tool, and none should be added.

The `mcp` SDK is an optional dependency, imported only inside `build_mcp_server`, so this
module imports without it. The registry is not implemented yet: every function raises
`NotImplementedError`.
"""

from __future__ import annotations

from typing import Any

_WP = "WP-062 (Stage-2 Conduit build)"


def build_mcp_server() -> Any:
    """Build the MCP server with all six tools, two resources and two prompts registered.

    The `mcp` SDK is to be imported inside this function so that a missing SDK produces a clear
    error here rather than at module import. Not implemented yet.
    """
    raise NotImplementedError(_WP)


def register_tools(server: Any) -> None:
    """Register the six tools on `server`, one per `scopes.ConduitTool`. Not implemented yet."""
    raise NotImplementedError(_WP)


def register_resources(server: Any) -> None:
    """Register the two read-only resources (corpus/overview, act/{id}). Not implemented yet."""
    raise NotImplementedError(_WP)


def register_prompts(server: Any) -> None:
    """Register the two safety-contract prompts. Not implemented yet.

    The prompts are cited_legal_lookup and obligation_briefing.
    """
    raise NotImplementedError(_WP)


__all__ = [
    "build_mcp_server",
    "register_prompts",
    "register_resources",
    "register_tools",
]
