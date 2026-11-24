"""Conduit: the metered external API over the legislation corpus, served as MCP and REST.

A separate ASGI service (entry point `conduit/app.py`) that exposes a read-only,
information-only slice of the Czech and EU legislation and obligations corpus as a remote MCP
server (OAuth 2.1) and an equivalent REST API (API keys). Both channels are metered per org and
pass every response through the compliance guard and the citation envelope.
"""

from __future__ import annotations

# Design invariants
#
# 1. Trust-boundary isolation. Conduit lives in this repository so it can import
#    regsuper.retrieval and regsuper.pythia.client directly, but it runs as its own process
#    with its own uvicorn, database pool and route table. This internet-facing surface, which
#    handles third-party tokens, never shares a process with the app's admin surface or its
#    /internal/* routes: conduit/app.py mounts neither, and the main app never imports this
#    package.
# 2. Off switch. The whole service sits behind `conduit_enabled` (default off). While it is
#    off, the app factory refuses to start the public listeners.
# 3. Citation chokepoint. Every tool and resource response is finalised by
#    envelope.finalize(): a response that carries data must carry citations, otherwise it is
#    refused. This mirrors the database CHECK on finding.cited_provisions. Conduit only reads;
#    it never writes findings.
# 4. Tenancy. Under enforced row-level security the caller's org (from the token or API key)
#    is stamped into app.current_org as the first statement of every request transaction
#    (tenancy.py).
# 5. Information only. The answer mode is always "information", never advice. Questions that
#    ask for reserved legal advice are refused without retrieval, but still metered so abuse
#    is visible.
# 6. Corpus scope. Legislation and regulatory obligations only. There is deliberately no
#    case-law or "find precedents" tool, and none should be added.
# 7. Standard denial codes, shared with the app via regsuper.entitlements: 402 payment or
#    upgrade required (feature not in tier), 403 not entitled (wrong org or missing scope),
#    429 quota or rate limit exhausted (quota.exceeded, raised before any engine call).
#
# Conduit reuses regsuper.retrieval, regsuper.entitlements, regsuper.billing
# (emit_billing_event with event_type='conduit.call') and regsuper.pythia.client rather than
# reimplementing them. The mcp SDK, pyjwt and Redis are optional dependencies (the `conduit`
# extra), never imported at module level. The REST handlers, the MCP registry and
# answer_legal_question are not implemented yet and raise NotImplementedError.

# Deliberately import-light: app.py, mcp_server.py and the tools are not imported eagerly, so
# importing this package never pulls in the optional `mcp` SDK or Redis and never starts a
# listener. The factory is resolved lazily (see __getattr__ below).
__all__ = ["create_conduit_app"]


def __getattr__(name: str):  # PEP 562 lazy attribute access
    """Expose `create_conduit_app` lazily, without importing app.py at package import time.

    This keeps `import regsuper.conduit` free of side effects and of the optional dependencies.
    Any other attribute raises the usual AttributeError.
    """
    if name == "create_conduit_app":
        from regsuper.conduit.app import create_conduit_app

        return create_conduit_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
