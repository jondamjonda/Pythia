"""Export claim ledgers to disk as canonical-JSON audit bundles.

When `claim_ledger_export_enabled` is on and an answer carries a claim ledger (the engine's
claim-verification loop ran, so `audit_trace["claim_ledger"]` is present), this module writes a
durable, reproducible audit bundle to `data/exports/claim_ledgers/`: a canonical-JSON `.json`
file plus a SHA-256 sidecar (`.json.sha256`). The bundle holds the ledger, per-stage LLM usage,
the engine request id (the key that joins the costed `api_call` row, the engine trace and the
`X-Pythia-Request-Id` header), the corpus snapshot id, the app's non-default flags, the model
ids and the prompt hashes. Together that is the full provenance of one claim-verified answer,
so an auditor can tell what was verified against which law snapshot under which flags and
models.

Two properties matter:

* Best effort, in the background. The export must never delay or deny the answer.
  `schedule_ledger_export` runs the write off the response path (`asyncio.create_task`), and
  every failure inside is caught and logged. With the flag off it returns immediately.
* Query-text redaction. The bundle includes the asker's question only when
  `conduit_capture_query_text` is on; otherwise the text is omitted. Apart from the ledger
  rows themselves (the audit artifact being exported), the bundle holds only counts, ids and
  hashes.

Determinism: the hashed content is serialised with `api.export.build_canonical_payload`, the
same pure canonical-JSON and SHA-256 builder the audit export uses, so the same content always
yields the same bytes and hash. Only the file name (date, a short uuid and the request id)
varies per export, and it is not part of the hashed content.
"""

from __future__ import annotations

import asyncio
import uuid as _uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from regsuper.api.export import build_canonical_payload
from regsuper.config import get_settings
from regsuper.observability.flip_preflight import non_default_flags
from regsuper.observability.logging_config import get_logger

_log = get_logger(__name__)

# Where bundles are written: a directory under data/ (next to the price book and limits), created on
# demand. Like api/export.py, this writes local files and never uploads to object storage.
_EXPORT_DIR = Path("data/exports/claim_ledgers")

# The bundle schema version. It is part of the hashed content, so a schema change changes the hash.
_BUNDLE_SCHEMA = "claim-ledger-export-v1"

# Strong references to in-flight export tasks, so the event loop cannot garbage-collect a task
# mid-write (ruff RUF006). Each task removes itself when done, so the set stays bounded.
_INFLIGHT: set[asyncio.Task[Any]] = set()


def _sanitize_rid(rid: Any) -> str:
    """Return a filesystem-safe fragment of the engine request id, for the file name.

    Keeps alphanumerics, '-' and '_', replaces everything else with '-', and caps the length. An
    empty or missing id becomes 'norid'.
    """
    s = str(rid) if rid is not None else ""
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in s).strip("-")
    return safe[:40] or "norid"


def build_bundle_content(ans: dict[str, Any], *, query: str | None = None) -> dict[str, Any] | None:
    """Build the bundle content for an answer, or None when there is no ledger to export.

    Pure (no I/O, clock or randomness), so the canonical JSON and its hash are reproducible.
    Returns None when ``audit_trace["claim_ledger"]`` is absent (the loop did not run).

    ``query`` (the asker's question) is included as ``query_text`` only when the caller passes it,
    which it does only when ``conduit_capture_query_text`` is on; otherwise the bundle has
    ``query_text_redacted: True`` and no query text. Everything else is the ledger, per-stage usage
    counts, and provenance ids and hashes; there is no free text beyond the ledger's own rows.
    """
    at = ans.get("audit_trace") if isinstance(ans, dict) else None
    if not isinstance(at, dict):
        return None
    ledger = at.get("claim_ledger")
    if not isinstance(ledger, dict):
        return None  # no ledger, nothing to export

    content: dict[str, Any] = {
        "schema": _BUNDLE_SCHEMA,
        "ledger": ledger,
        # Per-stage LLM usage: token counts and model names only, never prompt or response text.
        "llm_usage": at.get("llm_usage") if isinstance(at.get("llm_usage"), list) else [],
        "request_id": at.get("request_id"),
        # Provenance the engine stamps on the trace, with safe defaults when a field is missing.
        "corpus_snapshot_id": at.get("corpus_snapshot_id") or at.get("snapshot_id"),
        "model_ids": _model_ids(at),
        "prompt_hashes": at.get("prompt_hashes")
        if isinstance(at.get("prompt_hashes"), dict)
        else {},
        # The non-default app flags this answer ran under (names and values only).
        "flag_manifest": non_default_flags(),
    }
    # Query text only when the caller supplied it (conduit_capture_query_text is on).
    if query is not None:
        content["query_text"] = query
    else:
        content["query_text_redacted"] = True
    return content


def _model_ids(at: dict[str, Any]) -> list[str]:
    """Return the distinct model ids used, sorted.

    Taken from an explicit ``model_ids`` list, or derived from the model names in ``llm_usage``.
    Empty when neither is reported.
    """
    explicit = at.get("model_ids")
    if isinstance(explicit, list):
        return sorted({str(m) for m in explicit if m})
    usage = at.get("llm_usage")
    if isinstance(usage, list):
        return sorted(
            {str(e.get("model")) for e in usage if isinstance(e, dict) and e.get("model")}
        )
    return []


def _write_bundle(content: dict[str, Any], *, rid: Any) -> tuple[str, str] | None:
    """Write the canonical-JSON bundle and its .sha256 sidecar; return (json_path, sha256).

    Synchronous file I/O, run off the response path in a worker thread, so it never delays the
    answer. Errors propagate to the caller, which catches and logs them.
    """
    canonical_json, sha256_hex = build_canonical_payload(content)
    _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    short = _uuid.uuid4().hex[:8]
    stem = f"{day}_{short}_{_sanitize_rid(rid)}"
    json_path = _EXPORT_DIR / f"{stem}.json"
    sha_path = _EXPORT_DIR / f"{stem}.json.sha256"
    json_path.write_text(canonical_json, encoding="utf-8")
    # The sidecar holds the hex digest and the file name it covers (the sha256sum format).
    sha_path.write_text(f"{sha256_hex}  {json_path.name}\n", encoding="utf-8")
    return str(json_path), sha256_hex


async def export_ledger(ans: dict[str, Any]) -> tuple[str, str] | None:
    """Build and write the bundle for one answer.

    Returns (json_path, sha256) after a successful write, or None when there was nothing to export
    or the export failed. Never raises: every failure (settings, canonicalisation, disk I/O) is
    caught and logged, so the answer path is never affected. Whether to include the query text is
    decided here from ``conduit_capture_query_text``.

    The answer handler does not await this; :func:`schedule_ledger_export` runs it in the
    background. Awaiting it directly is fine in tests.
    """
    try:
        settings = get_settings()
        capture = bool(getattr(settings, "conduit_capture_query_text", False))
        # Pass the query text only when capture is enabled; None means it is redacted.
        query = None
        if capture:
            q = ans.get("_query_text") if isinstance(ans, dict) else None
            query = str(q) if isinstance(q, str) and q else None
        content = build_bundle_content(ans, query=query)
        if content is None:
            return None
        rid = (ans.get("audit_trace") or {}).get("request_id") if isinstance(ans, dict) else None
        # The blocking file I/O runs in a worker thread so it never blocks the event loop.
        result = await asyncio.to_thread(_write_bundle, content, rid=rid)
        if result is not None:
            _log.info("claim_ledger_exported", path=result[0], sha256=result[1][:12])
        return result
    except Exception:  # an export failure must never break or delay the answer
        _log.warning("claim_ledger_export_failed")
        return None


def schedule_ledger_export(ans: dict[str, Any], *, query_text: str | None = None) -> None:
    """Schedule a best-effort background export of an answer's claim ledger.

    This is the entry point the answer handler calls. The export runs fire-and-forget, so the
    answer is never delayed or denied. Does nothing unless ``claim_ledger_export_enabled`` is on.

    ``query_text`` is the asker's original question; it is written into the bundle only when
    ``conduit_capture_query_text`` is on. It is stored under a private ``_query_text`` key on a
    shallow copy of the answer, so the background task reads a stable value. Never raises.
    """
    if not getattr(get_settings(), "claim_ledger_export_enabled", False):
        return
    if not isinstance(ans, dict) or not isinstance(ans.get("audit_trace"), dict):
        return
    payload = dict(ans)
    if query_text is not None:
        payload["_query_text"] = query_text
    coro = export_ledger(payload)
    try:
        # Fire-and-forget: the write happens off the response path and failures are caught inside.
        # Keep a strong reference so the loop cannot garbage-collect the task mid-write.
        task = asyncio.create_task(coro, name="claim_ledger_export")
        _INFLIGHT.add(task)
        task.add_done_callback(_INFLIGHT.discard)
    except RuntimeError:
        # No running loop (e.g. a synchronous context): write directly instead, still catching every
        # error. asyncio.run awaits the same coroutine, so it is never left un-awaited.
        try:
            asyncio.run(coro)
        except Exception:  # a background-export failure must never surface to the answer
            coro.close()
            _log.warning("claim_ledger_export_schedule_failed")
