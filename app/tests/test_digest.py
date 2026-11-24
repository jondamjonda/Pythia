"""WP-049 — read-only plain-language compliance digest (api/digest.py).

DB-FREE coverage, two layers:

1. **Gated-off surface** — mounting ONLY this router on a bare FastAPI app
   (the ``test_audit_export._client`` pattern), with ``get_settings`` patched
   ``digest_enabled=False``, every one of the five GET routes is 404 (inert by
   default, indistinguishable from a missing endpoint until WP-049 flips it).

2. **PURE mappers** — ``_to_obligation_item`` / ``_to_checklist_item`` /
   ``_to_gap_item`` / ``_to_action_item`` shape the ``render`` dataclasses into
   the digest wire models (incl. the string → UUID coercion of
   ``cited_provision_id`` and the ``render.action`` → ``digest.statement``
   rename), and ``_dominant_answer_mode`` resolves the answer-mode precedence —
   all with no DB.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from regsuper.analysis import render
from regsuper.api import digest as digest_api


# ── 1. Gated-off surface (DB-free, router-only mount) ────────────────────────
def _client() -> TestClient:
    """A TestClient over JUST this router — no FastAPI lifespan, so no DB at import."""
    app = FastAPI()
    app.include_router(digest_api.router)
    return TestClient(app, raise_server_exceptions=True)


def test_all_routes_404_when_disabled() -> None:
    pid = uuid4()
    paths = [
        f"/api/digest/{pid}/obligations",
        f"/api/digest/{pid}/checklist",
        f"/api/digest/{pid}/gap-report",
        f"/api/digest/{pid}/actions",
        f"/api/digest/{pid}/summary",
    ]
    with patch.object(digest_api, "get_settings", lambda: SimpleNamespace(digest_enabled=False)):
        client = _client()
        for path in paths:
            resp = client.get(path)
            assert resp.status_code == 404, (path, resp.status_code)
            assert resp.json()["detail"] == "not found"


# ── 2. PURE mappers (DB-free) ────────────────────────────────────────────────
_PROV_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _render_obligation() -> render.ObligationItem:
    return render.ObligationItem(
        modal_label="musíte",
        actor_label="provozovatel",
        deadline="do 15 dnů",
        legal_source="§ 5 (CZ/zakon)",
        legal_source_url=f"/sources/{_PROV_ID}",
        cited_provision_id=_PROV_ID,
        confidence=0.82,
        review_state="unreviewed",
        disclaimer_id="info.v1",
    )


def test_to_obligation_item_maps_and_coerces_uuid() -> None:
    out = digest_api._to_obligation_item(_render_obligation())
    assert isinstance(out, digest_api.ObligationItem)
    assert out.modal_label == "musíte"
    assert out.actor_label == "provozovatel"
    assert out.deadline == "do 15 dnů"
    assert out.legal_source == "§ 5 (CZ/zakon)"
    assert out.legal_source_url == f"/sources/{_PROV_ID}"
    assert out.cited_provision_id == UUID(_PROV_ID)
    assert out.confidence == 0.82
    assert out.review_state == "unreviewed"


def test_to_checklist_item_pairs_obligation_with_status() -> None:
    out = digest_api._to_checklist_item(_render_obligation(), "neověřeno")
    assert isinstance(out, digest_api.ChecklistItem)
    assert out.status == "neověřeno"
    assert isinstance(out.obligation, digest_api.ObligationItem)
    assert out.obligation.cited_provision_id == UUID(_PROV_ID)
    assert out.obligation.modal_label == "musíte"


def test_to_gap_item_maps_and_coerces_uuid() -> None:
    r = render.GapItem(
        statement="Chybí evidence.",
        severity_label="kritické",
        legal_source="§ 5 (CZ/zakon)",
        legal_source_url=f"/sources/{_PROV_ID}",
        cited_provision_id=_PROV_ID,
        disclaimer_id="info.v1",
    )
    out = digest_api._to_gap_item(r)
    assert isinstance(out, digest_api.GapItem)
    assert out.statement == "Chybí evidence."
    assert out.severity_label == "kritické"
    assert out.legal_source == "§ 5 (CZ/zakon)"
    assert out.cited_provision_id == UUID(_PROV_ID)


def test_to_action_item_renames_action_to_statement() -> None:
    r = render.ActionItem(
        action="Zaveďte evidenci.",
        priority="okamžitě",
        legal_source="§ 5 (CZ/zakon)",
        legal_source_url=f"/sources/{_PROV_ID}",
        cited_provision_id=_PROV_ID,
        disclaimer_id="info.v1",
    )
    out = digest_api._to_action_item(r)
    assert isinstance(out, digest_api.ActionItem)
    assert out.statement == "Zaveďte evidenci."
    assert out.priority == "okamžitě"
    assert out.legal_source == "§ 5 (CZ/zakon)"


# ── 3. PURE _dominant_answer_mode precedence ─────────────────────────────────
def _findings(*modes) -> list:
    return [SimpleNamespace(answer_mode=m) for m in modes]


def test_dominant_answer_mode_precedence() -> None:
    # reserved_advice wins over everything
    assert (
        digest_api._dominant_answer_mode(
            _findings("information", "needs_review", "reserved_advice")
        )
        == "reserved_advice"
    )
    # needs_review wins over information
    assert (
        digest_api._dominant_answer_mode(_findings("information", "needs_review")) == "needs_review"
    )
    # all-information stays information
    assert (
        digest_api._dominant_answer_mode(_findings("information", "information")) == "information"
    )
    # empty findings → information
    assert digest_api._dominant_answer_mode([]) == "information"


def test_dominant_answer_mode_missing_attr_falls_to_information() -> None:
    # a finding with NO answer_mode attribute (getattr default None) → information
    findings = [SimpleNamespace()]  # no answer_mode set at all
    assert digest_api._dominant_answer_mode(findings) == "information"


# ── 4. PURE _disclaimer_text language selection ──────────────────────────────
def test_disclaimer_text_picks_language_and_handles_none() -> None:
    block = render.DisclaimerBlock(
        id="info.v1",
        answer_mode="information",
        confidence="normal",
        text_cs="Toto není právní rada.",
        text_en="This is not legal advice.",
    )
    assert digest_api._disclaimer_text(block, "cs") == "Toto není právní rada."
    assert digest_api._disclaimer_text(block, "en") == "This is not legal advice."
    assert digest_api._disclaimer_text(None, "cs") == ""
