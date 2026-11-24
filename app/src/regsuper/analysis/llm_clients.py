"""A single shared OpenAI client per process.

Every LLM call (analysis cascade, ingest, retrieval, calibration) uses one ``AsyncOpenAI``, so the
underlying HTTP connection pool and keep-alive connections are reused instead of each call site
opening its own.

The lifecycle mirrors ``pythia/client.py``:

* ``init_openai()`` creates the client inside the running event loop (the api/main.py lifespan,
  before serving). Idempotent.
* ``get_openai()`` returns the client, creating it lazily if the lifespan never ran (CI, a CLI
  ingest run, the calibration harness). Raises if the API key is missing rather than returning a
  half-built or None client.
* ``close_openai()`` closes and drops the client on shutdown. Idempotent.

``AsyncOpenAI`` does not bind to an event loop at construction (it builds its httpx client on first
use), but it is still created from these functions, inside a running loop, to keep every client
lifecycle in one place.
"""

from __future__ import annotations

from openai import AsyncOpenAI

from regsuper.config import get_settings

# Process-global client, shared by every call site via `get_openai()`. Created by `init_openai()`
# (lifespan) or lazily by `get_openai()` (CI, CLI, calibration); torn down by `close_openai()`.
_CLIENT: AsyncOpenAI | None = None


def _new_client() -> AsyncOpenAI:
    """Build the AsyncOpenAI from settings, raising if the API key is unset.

    The RuntimeError message is the one callers and tests already expect.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    return AsyncOpenAI(api_key=settings.openai_api_key)


async def init_openai() -> None:
    """Create the shared AsyncOpenAI ahead of the first request (idempotent).

    Call from the running event loop; the api/main.py lifespan does this before serving. Does
    nothing if a client already exists. Also does nothing if no OPENAI_API_KEY is configured, so
    a keyless local or CI deployment still boots; ``get_openai()`` raises if an LLM call is
    actually attempted without a key.
    """
    global _CLIENT
    if _CLIENT is None and get_settings().openai_api_key:
        _CLIENT = _new_client()


def get_openai() -> AsyncOpenAI:
    """Return the shared AsyncOpenAI, creating it if the lifespan never ran.

    CI, a CLI ingest run and the calibration harness call the LLM path without booting the app.
    A missing OPENAI_API_KEY raises rather than returning a None or half-built client.
    """
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = _new_client()
    return _CLIENT


async def close_openai() -> None:
    """Close and drop the shared AsyncOpenAI on app shutdown (idempotent)."""
    global _CLIENT
    if _CLIENT is not None:
        client, _CLIENT = _CLIENT, None
        await client.close()
