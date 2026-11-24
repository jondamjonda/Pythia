"""VL-G-07 — the claim-ledger CANONICAL-JSON disk export (data/exports/claim_ledgers/).

What this pins (the manifest acceptance):
  * from a fixture ledger, the exporter writes a .json + a .json.sha256 whose sidecar digest MATCHES
    the SHA-256 recomputed from the written bytes — proven over 3 distinct exports (3 artifact pairs).
  * canonical JSON is BYTE-STABLE — re-serialising the same content == the file bytes (reproducible).
  * WP-029 REDACTION — the asker's query text is in the bundle ONLY when conduit_capture_query_text
    is True; otherwise it is absent (query_text_redacted flag) and no query text is present.
  * an export FAILURE never breaks (or delays/denies) the answer — export_ledger is TOTAL (returns
    None on any error) and schedule_ledger_export never raises.
  * flag OFF ⇒ nothing scheduled, nothing written (byte-identical).

All DB-free: the exporter only reads the answer dict + settings and writes local files (into a temp dir).
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from regsuper.api.export import build_canonical_payload
from regsuper.observability import ledger_export


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _ledger_answer(*, rid: str = "rid-abc123") -> dict:
    """An engine answer dict carrying a claim ledger (the claim-verify loop ran) plus provenance keys."""
    return {
        "refused": False,
        "verdict": "ok",
        "drafted_answer": "## Legislativní rámec\nViz § 297 [1].",
        "audit_trace": {
            "request_id": rid,
            "metagate_mode": "enforce",
            "corpus_snapshot_id": "pythia-2026-07-02",
            "prompt_hashes": {"synth": "abc123", "verify": "def456"},
            "llm_usage": [
                {
                    "stage": "generate",
                    "model": "gpt-5.5",
                    "tokens_input": 1200,
                    "tokens_output": 300,
                },
                {
                    "stage": "verify",
                    "model": "gpt-5.4-mini",
                    "tokens_input": 800,
                    "tokens_output": 40,
                },
            ],
            "claim_ledger": {
                "schema": "cl-v1",
                "summary": {
                    "mode": "enforce",
                    "claims_total": 2,
                    "demoted": 1,
                    "per_status": {"verified": 1, "demoted_gap": 1},
                },
                "rows": [
                    {"claim_id": "c0", "final": "verified", "citations": ["p-1"]},
                    {"claim_id": "c1", "final": "demoted_gap", "citations": ["p-1"]},
                ],
            },
        },
    }


def _settings(
    *, claim_ledger_export_enabled: bool = True, conduit_capture_query_text: bool = False
) -> SimpleNamespace:
    return SimpleNamespace(
        claim_ledger_export_enabled=claim_ledger_export_enabled,
        conduit_capture_query_text=conduit_capture_query_text,
    )


# ---------------------------------------------------------------------------
# 1. 3 exported artifacts whose recomputed SHA-256 matches the sidecar .sha256
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_three_exports_sha256_matches_sidecar(tmp_path) -> None:
    """Export 3 distinct answers; each writes a .json + .json.sha256 whose sidecar digest equals the
    SHA-256 recomputed from the written .json bytes."""
    with (
        patch.object(ledger_export, "_EXPORT_DIR", tmp_path),
        patch.object(ledger_export, "get_settings", lambda: _settings()),
    ):
        for i in range(3):
            result = await ledger_export.export_ledger(_ledger_answer(rid=f"rid-{i}"))
            assert result is not None, "a present ledger must produce an export"
            json_path, sha_hex = result
            from pathlib import Path

            jp = Path(json_path)
            sp = Path(str(jp) + ".sha256")
            assert jp.is_file() and sp.is_file()
            # recompute the hash from the on-disk bytes and compare to the sidecar's recorded digest.
            recomputed = hashlib.sha256(jp.read_bytes()).hexdigest()
            sidecar_digest = sp.read_text(encoding="utf-8").split()[0]
            assert recomputed == sidecar_digest == sha_hex

        # exactly 3 json + 3 sha256 artifacts landed.
        jsons = sorted(tmp_path.glob("*.json"))
        shas = sorted(tmp_path.glob("*.json.sha256"))
        assert len(jsons) == 3 and len(shas) == 3


# ---------------------------------------------------------------------------
# 2. canonical JSON is byte-stable (re-serialise the content == the file)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_canonical_json_is_byte_stable(tmp_path) -> None:
    """The written .json equals build_canonical_payload(content) re-serialised from the same content —
    reproducible bytes, so the hash is stable."""
    ans = _ledger_answer()
    content = ledger_export.build_bundle_content(ans, query=None)
    assert content is not None
    canonical_json, sha_hex = build_canonical_payload(content)

    with (
        patch.object(ledger_export, "_EXPORT_DIR", tmp_path),
        patch.object(ledger_export, "get_settings", lambda: _settings()),
    ):
        json_path, written_sha = await ledger_export.export_ledger(ans)

    from pathlib import Path

    on_disk = Path(json_path).read_text(encoding="utf-8")
    assert on_disk == canonical_json, (
        "the file must be byte-identical to the canonical serialisation"
    )
    assert written_sha == sha_hex
    # and it round-trips to the same structural content.
    assert json.loads(on_disk) == content


# ---------------------------------------------------------------------------
# 3. WP-029 redaction — query text present ONLY when conduit_capture_query_text is True
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_wp029_query_text_absent_when_capture_off(tmp_path) -> None:
    """capture OFF (the default) ⇒ the query text is REDACTED: no `query_text`, a `query_text_redacted`
    marker, and the literal query string never appears in the bytes."""
    ans = dict(_ledger_answer(), _query_text="musím vést evidenci skutečných majitelů?")
    with (
        patch.object(ledger_export, "_EXPORT_DIR", tmp_path),
        patch.object(
            ledger_export, "get_settings", lambda: _settings(conduit_capture_query_text=False)
        ),
    ):
        json_path, _ = await ledger_export.export_ledger(ans)

    from pathlib import Path

    body = Path(json_path).read_text(encoding="utf-8")
    doc = json.loads(body)
    assert "query_text" not in doc
    assert doc.get("query_text_redacted") is True
    assert "musím vést evidenci" not in body


@pytest.mark.asyncio
async def test_wp029_query_text_present_when_capture_on(tmp_path) -> None:
    """capture ON ⇒ the query text IS carried in the bundle."""
    q = "musím vést evidenci skutečných majitelů?"
    ans = dict(_ledger_answer(), _query_text=q)
    with (
        patch.object(ledger_export, "_EXPORT_DIR", tmp_path),
        patch.object(
            ledger_export, "get_settings", lambda: _settings(conduit_capture_query_text=True)
        ),
    ):
        json_path, _ = await ledger_export.export_ledger(ans)

    from pathlib import Path

    doc = json.loads(Path(json_path).read_text(encoding="utf-8"))
    assert doc.get("query_text") == q
    assert "query_text_redacted" not in doc


# ---------------------------------------------------------------------------
# 4. no ledger ⇒ nothing exported (byte-identical); export failure never raises
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_ledger_writes_nothing(tmp_path) -> None:
    ans = {"refused": False, "audit_trace": {"request_id": "r"}}  # no claim_ledger key
    with (
        patch.object(ledger_export, "_EXPORT_DIR", tmp_path),
        patch.object(ledger_export, "get_settings", lambda: _settings()),
    ):
        result = await ledger_export.export_ledger(ans)
    assert result is None
    assert list(tmp_path.glob("*")) == []


@pytest.mark.asyncio
async def test_export_failure_is_swallowed(tmp_path) -> None:
    """A disk-write failure is swallowed (returns None) — the export is TOTAL and never propagates."""
    with (
        patch.object(ledger_export, "_EXPORT_DIR", tmp_path),
        patch.object(ledger_export, "get_settings", lambda: _settings()),
        patch.object(ledger_export, "_write_bundle", side_effect=OSError("disk full")),
    ):
        result = await ledger_export.export_ledger(_ledger_answer())
    assert result is None  # error swallowed, no raise


# ---------------------------------------------------------------------------
# 5. schedule_ledger_export — flag gate + never raises (best-effort, never delays/denies)
# ---------------------------------------------------------------------------
def test_schedule_off_does_nothing(tmp_path) -> None:
    """flag OFF ⇒ schedule_ledger_export writes nothing and schedules no task (byte-identical)."""
    scheduled: list = []
    with (
        patch.object(ledger_export, "_EXPORT_DIR", tmp_path),
        patch.object(
            ledger_export, "get_settings", lambda: _settings(claim_ledger_export_enabled=False)
        ),
        patch.object(ledger_export.asyncio, "create_task", lambda *a, **k: scheduled.append(a)),
    ):
        ledger_export.schedule_ledger_export(_ledger_answer(), query_text="q")
    assert scheduled == []
    assert list(tmp_path.glob("*")) == []


@pytest.mark.asyncio
async def test_schedule_fires_a_background_task(tmp_path) -> None:
    """flag ON ⇒ schedule_ledger_export fire-and-forgets a task; awaiting it produces the artifacts."""
    with (
        patch.object(ledger_export, "_EXPORT_DIR", tmp_path),
        patch.object(ledger_export, "get_settings", lambda: _settings()),
    ):
        ledger_export.schedule_ledger_export(_ledger_answer(), query_text=None)
        # drain the fire-and-forget task so the write completes deterministically in the test.
        import asyncio

        pending = [t for t in asyncio.all_tasks() if t.get_name() == "claim_ledger_export"]
        assert pending, "a background export task must have been scheduled"
        await asyncio.gather(*pending)
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_schedule_never_raises_on_missing_loop(tmp_path) -> None:
    """In a sync context with no running loop, schedule_ledger_export still never raises (it degrades to
    a direct best-effort write). The answer path is unaffected either way."""
    with (
        patch.object(ledger_export, "_EXPORT_DIR", tmp_path),
        patch.object(ledger_export, "get_settings", lambda: _settings()),
    ):
        # no running event loop here → the create_task RuntimeError path degrades to asyncio.run.
        ledger_export.schedule_ledger_export(_ledger_answer(), query_text=None)
    # a best-effort direct write landed one artifact pair (or none on any error — never a raise).
    assert len(list(tmp_path.glob("*.json"))) in (0, 1)


# ---------------------------------------------------------------------------
# 6. handler integration — the export is BEST-EFFORT: it never breaks / delays / denies the answer
# ---------------------------------------------------------------------------
def _answer_settings(*, claim_ledger_export_enabled: bool = True) -> SimpleNamespace:
    """The full getattr-safe settings namespace the /api/answer handler reads."""
    return SimpleNamespace(
        answer_endpoint_enabled=True,
        compliance_guard_enabled=False,
        compliance_guard_mode="shadow",
        require_auth=False,
        entitlements_enabled=False,
        answer_shadow_weak_coverage_hedged=False,
        answer_profile_context_enabled=False,
        answer_clarify_enabled=False,
        answer_practical_block_enabled=False,
        answer_intake_panel_enabled=False,
        authority_registry_enabled=False,
        answer_budget_precheck_enabled=False,
        claim_ledger_export_enabled=claim_ledger_export_enabled,
        conduit_capture_query_text=False,
    )


def _sound_ledger_answer() -> dict:
    """A sound, grounded answer that also carries a claim ledger (so the export path is live)."""
    return {
        "refused": False,
        "verdict": "ok",
        "drafted_answer": "## Legislativní rámec\nViz § 5 [1].",
        "cited_provisions": [{"provision_id": "p-1"}],
        "citations_map": [{"start": 0, "end": 5, "provision_id": "p-1", "label": "§ 5"}],
        "sections": [
            {
                "kind": "legislative",
                "heading": "Legislativní rámec",
                "text": "Viz § 5.",
                "citations_map": [{"start": 0, "end": 5, "provision_id": "p-1", "label": "§ 5"}],
            }
        ],
        "render_mode": "full",
        "band": "full",
        "meta": {"route": "sound", "intent": "legal", "coverage": "covered"},
        "audit_trace": {
            "request_id": "rid-h",
            "claim_ledger": {
                "schema": "cl-v1",
                "summary": {"claims_total": 1},
                "rows": [{"claim_id": "c0"}],
            },
        },
    }


def test_handler_answer_served_and_exports_when_flag_on(tmp_path) -> None:
    """Flag ON + a ledger present: POST /api/answer serves 200 AND (best-effort background) writes the
    bundle. The export runs AFTER the response is shaped, so it can never delay/deny the answer."""
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from regsuper.api import answer as answer_api

    app = FastAPI()
    app.include_router(answer_api.router)
    with (
        patch.object(answer_api, "get_settings", lambda: _answer_settings()),
        patch.object(answer_api.pythia, "answer", AsyncMock(return_value=_sound_ledger_answer())),
        patch.object(ledger_export, "_EXPORT_DIR", tmp_path),
        patch.object(ledger_export, "get_settings", lambda: _answer_settings()),
    ):
        resp = TestClient(app, raise_server_exceptions=True).post(
            "/api/answer", json={"query": "x"}
        )
    assert resp.status_code == 200
    assert resp.json()["refused"] is False
    # the background export landed (the TestClient runs the loop to completion, draining the task).
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_handler_answer_unaffected_when_export_would_raise(tmp_path) -> None:
    """Even if the (normally total) exporter's inner write RAISES, the answer is still served 200 — the
    failure is swallowed inside export_ledger; the answer path is never affected."""
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from regsuper.api import answer as answer_api

    app = FastAPI()
    app.include_router(answer_api.router)
    with (
        patch.object(answer_api, "get_settings", lambda: _answer_settings()),
        patch.object(answer_api.pythia, "answer", AsyncMock(return_value=_sound_ledger_answer())),
        patch.object(ledger_export, "_EXPORT_DIR", tmp_path),
        patch.object(ledger_export, "get_settings", lambda: _answer_settings()),
        patch.object(ledger_export, "_write_bundle", side_effect=OSError("disk full")),
    ):
        resp = TestClient(app, raise_server_exceptions=True).post(
            "/api/answer", json={"query": "x"}
        )
    assert resp.status_code == 200  # the write error was swallowed — the answer is unaffected
    assert resp.json()["refused"] is False
    assert list(tmp_path.glob("*.json")) == []  # nothing written, but no raise reached the handler


def test_handler_off_writes_nothing(tmp_path) -> None:
    """Flag OFF ⇒ the handler schedules no export and writes nothing (byte-identical)."""
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from regsuper.api import answer as answer_api

    app = FastAPI()
    app.include_router(answer_api.router)
    with (
        patch.object(
            answer_api, "get_settings", lambda: _answer_settings(claim_ledger_export_enabled=False)
        ),
        patch.object(answer_api.pythia, "answer", AsyncMock(return_value=_sound_ledger_answer())),
        patch.object(ledger_export, "_EXPORT_DIR", tmp_path),
        patch.object(
            ledger_export,
            "get_settings",
            lambda: _answer_settings(claim_ledger_export_enabled=False),
        ),
    ):
        resp = TestClient(app, raise_server_exceptions=True).post(
            "/api/answer", json={"query": "x"}
        )
    assert resp.status_code == 200
    assert list(tmp_path.glob("*")) == []
