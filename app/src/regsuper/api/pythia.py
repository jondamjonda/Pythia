"""Feature-flagged proxy endpoints to the Pythia reasoning engine.

Off by default: with `PYTHIA_ENABLED` unset every route returns 503, and the analysis cascade is
unaffected either way. When enabled, the routes forward questions and corpus reads to the engine.
The engine answers from its own corpus (the `pythia` database), so the returned provision ids are
the engine's and are shown for display only; they are never joined into the app's tables.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from regsuper.config import get_settings
from regsuper.pythia import client as pythia

router = APIRouter(prefix="/api/pythia", tags=["pythia"])


class AskRequest(BaseModel):
    query: str
    method: str = "cte"  # cte, ppr or gnn (gnn needs the engine's [gnn] extra)
    k: int = 12
    # Optional point-in-time and verification settings for /answer. A field left as None is not
    # sent, so the engine applies its own defaults (verify="gated", as_of = current law).
    as_of: str | None = None  # ISO date (YYYY-MM-DD) for a point-in-time answer
    verify: str | None = None  # off, gated or always: the edge-assertion faithfulness check


class IdsRequest(BaseModel):
    """Body for the id-keyed read proxies (/provisions, /importance)."""

    ids: list[str] = []


class CandidatesRequest(BaseModel):
    """Body for /candidates: metadata-filtered candidate generation (statutes only by default)."""

    jurisdiction: list[str] | None = None
    legal_domain: list[str] | None = None
    legal_hierarchy: list[str] | None = None
    limit: int = 200


def _require_enabled() -> None:
    s = get_settings()
    if not (s.pythia_enabled and s.pythia_url):
        raise HTTPException(
            503, "Pythia is disabled — set PYTHIA_ENABLED=true and PYTHIA_URL to the sidecar URL"
        )


async def _proxy(make_coro):
    """Run an engine call and map its errors to HTTP responses.

    The engine's own status (404 unknown act, 422 bad id) is passed through unchanged, so it is
    not masked as a 502; a transport failure becomes a 502. `make_coro` is a zero-argument
    callable, so the coroutine is only created after the enabled check passes.
    """
    _require_enabled()
    try:
        return await make_coro()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            exc.response.status_code, f"Pythia sidecar: {exc.response.text[:200]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Pythia sidecar unreachable: {exc!r}") from exc


@router.get("/health")
async def pythia_health() -> dict[str, Any]:
    """Return the engine's own health report (database and Ollama liveness).

    503 when the integration is disabled, 502 when the engine is unreachable.
    """
    _require_enabled()
    try:
        return {"enabled": True, "sidecar": await pythia.health()}
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Pythia sidecar unreachable: {exc!r}") from exc


@router.post("/retrieve")
async def pythia_retrieve(req: AskRequest) -> dict[str, Any]:
    """Run fast, LLM-free graph retrieval on the engine and return the cited provisions."""
    _require_enabled()
    try:
        return await pythia.retrieve(req.query, method=req.method, k=req.k)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Pythia sidecar error: {exc!r}") from exc


@router.post("/answer")
async def pythia_answer(req: AskRequest) -> dict[str, Any]:
    """Return a full grounded, cited answer from the engine.

    Slow when the engine drafts with a local LLM; the client waits up to about 400 seconds.
    `as_of` and `verify` are forwarded only when set; otherwise the engine's defaults apply.
    """
    _require_enabled()
    try:
        return await pythia.answer(
            req.query, method=req.method, k=req.k, as_of=req.as_of, verify=req.verify
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Pythia sidecar error: {exc!r}") from exc


# ── Corpus read endpoints (feed for the regulation explorer) ──────────────────────────────────────
# One-to-one proxies of the engine's read API, so the frontend can browse the engine's corpus
# (browse index, act trees, rulings, importance, candidates) without importing Pythia. The returned
# ids belong to the engine's id space, which is disjoint from the app's: a view renders one corpus
# only, and these ids must never be joined into the app's tables.
@router.get("/browse")
async def pythia_browse() -> dict[str, Any]:
    """Return the faceted browse index (facets, regimes, spine and catalogue)."""
    return await _proxy(lambda: pythia.browse())


@router.get("/act/{external_id:path}")
async def pythia_act(external_id: str, as_of: str | None = None) -> dict[str, Any]:
    """Return one act's nested tree of sections.

    Czech act ids contain a slash (for example `240/2013`), hence the path parameter. `as_of` is an
    optional point-in-time date. Returns 404 for an unknown act.
    """
    return await _proxy(lambda: pythia.act(external_id, as_of=as_of))


@router.post("/provisions")
async def pythia_provisions(req: IdsRequest) -> list[dict[str, Any]]:
    """Return metadata and trust facets for the given provision ids, in input order."""
    return await _proxy(lambda: pythia.provisions(req.ids))


@router.post("/importance")
async def pythia_importance(req: IdsRequest) -> dict[str, Any]:
    """Return graph centrality and community per provision id: `{id: {centrality, community}}`."""
    return await _proxy(lambda: pythia.importance(req.ids))


@router.get("/rulings/{provision_id}")
async def pythia_rulings(provision_id: str, as_of: str | None = None) -> list[dict[str, Any]]:
    """Return the court rulings linked to one statute provision (court, case number, title, date).

    `as_of` is an optional point-in-time date.
    """
    return await _proxy(lambda: pythia.rulings(provision_id, as_of=as_of))


@router.post("/candidates")
async def pythia_candidates(req: CandidatesRequest) -> list[dict[str, Any]]:
    """Return metadata-filtered candidate provisions ranked by centrality.

    Only statutes are returned; case law is never included.
    """
    return await _proxy(
        lambda: pythia.candidates(
            jurisdiction=req.jurisdiction,
            legal_domain=req.legal_domain,
            legal_hierarchy=req.legal_hierarchy,
            limit=req.limit,
        )
    )
