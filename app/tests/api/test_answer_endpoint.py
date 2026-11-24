"""T-ANS-M6 — the app consumer route POST /api/answer (Phase-C ANSWER SURFACE).

The sidecar is MOCKED (``pythia.client.answer``) — NO live sidecar runs. The router is
mounted on a bare FastAPI app (the ``test_digest._client`` pattern), so there is no
lifespan + no DB at import; the seams (the flag, the sidecar, entitlements/metering, the
compliance guard) are patched per-test.

What it pins (the manifest acceptance + the cardinal invariants):
  * OFF ⇒ 404 (+ the full suite byte-identical — the router is inert by default).
  * ON (sidecar mocked) ⇒ {sections, chips, meta, band} shaped from the engine dict.
  * AC-CITATION-CHECK — every emitted chip.provision_id ∈ cited_provisions; a forged
    chip (id NOT in cited_provisions) is DROPPED; all-forged → refused.
  * quota call-order — check_and_reserve_quota sits BEFORE pythia.client.answer.
  * org_id threaded — principal.org_id reaches the sidecar call.
  * SINGLE-SHOT — a body with history/messages is 422'd, never folded into query.
  * as_of × NULL valid_from — a temporal caveat ("datum účinnosti neuvedeno"), never a
    fabricated date.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from regsuper.api import answer as answer_api

_ANON_ORG = UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
def _client() -> TestClient:
    """A TestClient over JUST the answer router — no lifespan, so no DB at import."""
    app = FastAPI()
    app.include_router(answer_api.router)
    return TestClient(app, raise_server_exceptions=True)


def _settings(
    *,
    answer_endpoint_enabled: bool = True,
    compliance_guard_enabled: bool = False,
    compliance_guard_mode: str = "shadow",
    require_auth: bool = False,
    answer_shadow_weak_coverage_hedged: bool = False,
    answer_profile_context_enabled: bool = False,
    answer_clarify_enabled: bool = False,
    answer_practical_block_enabled: bool = False,
    answer_intake_panel_enabled: bool = False,
    authority_registry_enabled: bool = False,
    authority_seed_path: str | None = None,
    authority_topic_map_path: str | None = None,
    authority_gazetteer_path: str | None = None,
) -> SimpleNamespace:
    """A getattr-safe settings namespace for the seams the handler reads."""
    return SimpleNamespace(
        answer_endpoint_enabled=answer_endpoint_enabled,
        compliance_guard_enabled=compliance_guard_enabled,
        compliance_guard_mode=compliance_guard_mode,
        require_auth=require_auth,
        entitlements_enabled=False,
        answer_shadow_weak_coverage_hedged=answer_shadow_weak_coverage_hedged,
        answer_profile_context_enabled=answer_profile_context_enabled,
        answer_clarify_enabled=answer_clarify_enabled,
        answer_practical_block_enabled=answer_practical_block_enabled,
        answer_intake_panel_enabled=answer_intake_panel_enabled,
        authority_registry_enabled=authority_registry_enabled,
        authority_seed_path=authority_seed_path,
        authority_topic_map_path=authority_topic_map_path,
        authority_gazetteer_path=authority_gazetteer_path,
    )


def _weak_coverage_answer(
    *, coverage: str = "weak_coverage", metagate_mode: str = "shadow"
) -> dict:
    """An engine PythiaAnswer dict for a not_in_corpus route the engine STILL synthesized
    (shadow): refused=False, verdict ok, a grounded section + chip, but render_mode=closest
    (the metagate's advisory route mapped through). ``coverage`` / ``metagate_mode`` vary the
    scoping (weak_coverage+shadow is the only upgradeable shape)."""
    return {
        "refused": False,
        "verdict": "ok",
        "drafted_answer": "## Legislativní rámec\nZ AML/CFT ustanovení vyplývá [1].",
        "cited_provisions": [{"provision_id": "p-1"}],
        "citations_map": [{"start": 0, "end": 5, "provision_id": "p-1", "label": "§ 5"}],
        "sections": [
            {
                "kind": "legislative",
                "heading": "Legislativní rámec",
                "text": "Z AML/CFT ustanovení vyplývá pro povinné osoby.",
                "citations_map": [{"start": 0, "end": 5, "provision_id": "p-1", "label": "§ 5"}],
            }
        ],
        "render_mode": "closest",
        "band": "closest",
        "meta": {"route": "not_in_corpus", "intent": "legal", "coverage": coverage},
        "audit_trace": {"metagate_mode": metagate_mode},
    }


def _sound_answer() -> dict:
    """An engine PythiaAnswer dict: a sound, grounded, sectioned answer with one real
    chip (p-1) and one FORGED chip (id not in cited_provisions → must be dropped)."""
    return {
        "refused": False,
        "verdict": "ok",
        "drafted_answer": "## Legislativní rámec\nViz § 5 [1] a § 9 [2].",
        "cited_provisions": [{"provision_id": "p-1"}, {"provision_id": "p-2"}],
        "citations_map": [
            {"start": 0, "end": 5, "provision_id": "p-1", "label": "§ 5"},
            {"start": 6, "end": 9, "provision_id": "FORGED-99", "label": "§ X"},
        ],
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
    }


def _patch_handler(answer_dict: dict, *, settings: SimpleNamespace | None = None):
    """Patch the handler seams: the flag/settings, the mocked sidecar, and the
    anonymous principal (require_auth=False → no DB). Returns the AsyncMock sidecar."""
    settings = settings or _settings()
    sidecar = AsyncMock(return_value=answer_dict)
    return (
        patch.object(answer_api, "get_settings", lambda: settings),
        patch.object(answer_api.pythia, "answer", sidecar),
        sidecar,
    )


# ---------------------------------------------------------------------------
# 1. OFF ⇒ 404 (inert by default)
# ---------------------------------------------------------------------------
def test_off_returns_404() -> None:
    with patch.object(answer_api, "get_settings", lambda: _settings(answer_endpoint_enabled=False)):
        resp = _client().post("/api/answer", json={"query": "musím vést evidenci?"})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "not found"


def test_off_does_not_call_sidecar() -> None:
    sidecar = AsyncMock(return_value=_sound_answer())
    with (
        patch.object(answer_api, "get_settings", lambda: _settings(answer_endpoint_enabled=False)),
        patch.object(answer_api.pythia, "answer", sidecar),
    ):
        _client().post("/api/answer", json={"query": "x"})
        sidecar.assert_not_called()


# ---------------------------------------------------------------------------
# 2. ON ⇒ shaped {sections, chips, meta, band}; AC-CITATION-CHECK
# ---------------------------------------------------------------------------
def test_on_shapes_sections_chips_meta_band() -> None:
    p_settings, p_sidecar, _ = _patch_handler(_sound_answer())
    with p_settings, p_sidecar:
        resp = _client().post("/api/answer", json={"query": "musím vést evidenci?"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["render_mode"] == "full"
    assert body["refused"] is False
    assert body["band"] == "full"
    assert body["meta"]["route"] == "sound"
    assert len(body["sections"]) == 1
    assert body["sections"][0]["kind"] == "legislative"
    assert body["disclaimer"]  # information-not-advice always stamped


def test_every_chip_provision_id_in_cited_provisions() -> None:
    """AC-CITATION-CHECK: the forged chip (FORGED-99 ∉ cited_provisions) is DROPPED;
    only the grounded chip (p-1) survives — flat + per-section."""
    p_settings, p_sidecar, _ = _patch_handler(_sound_answer())
    with p_settings, p_sidecar:
        body = _client().post("/api/answer", json={"query": "x"}).json()
    cited = {"p-1", "p-2"}
    assert [c["provision_id"] for c in body["chips"]] == ["p-1"]
    assert all(c["provision_id"] in cited for c in body["chips"])
    sec_chip_ids = [c["provision_id"] for c in body["sections"][0]["chips"]]
    assert sec_chip_ids == ["p-1"]
    # corpus is stamped pythia on every chip
    assert all(c["corpus"] == "pythia" for c in body["chips"])


def test_all_forged_chips_refuses() -> None:
    """When the engine drafted prose but EVERY chip fails the grounded re-check, the
    answer is refused — never an uncited synthesized claim."""
    ans = _sound_answer()
    ans["citations_map"] = [{"start": 0, "end": 5, "provision_id": "FORGED-A", "label": "§ 5"}]
    ans["sections"][0]["citations_map"] = [
        {"start": 0, "end": 5, "provision_id": "FORGED-A", "label": "§ 5"}
    ]
    p_settings, p_sidecar, _ = _patch_handler(ans)
    with p_settings, p_sidecar:
        body = _client().post("/api/answer", json={"query": "x"}).json()
    assert body["refused"] is True
    assert body["render_mode"] == "refuse"
    assert body["chips"] == []
    assert body["sections"] == []
    assert body["title"]  # the honest refuse frame copy
    assert body["body"]


def test_section_level_all_forged_section_is_dropped_not_emitted() -> None:
    """SECTION-granularity AC-CITATION-CHECK: a multi-section answer where ONE section's
    prose is grounded (kept) and ANOTHER section's only citations are forged (its prose is
    an uncited claim) → the forged section is DROPPED entirely; the clean section survives;
    the answer is NOT refused (grounded prose remains)."""
    ans = _sound_answer()
    ans["cited_provisions"] = [{"provision_id": "p-1"}]
    ans["citations_map"] = [
        {"start": 0, "end": 5, "provision_id": "p-1", "label": "§ 5"},
        {"start": 6, "end": 9, "provision_id": "FORGED-9", "label": "§ X"},
    ]
    ans["sections"] = [
        {
            "kind": "legislative",
            "heading": "Legislativní rámec",
            "text": "Viz § 5.",
            "citations_map": [{"start": 0, "end": 5, "provision_id": "p-1", "label": "§ 5"}],
        },
        {
            "kind": "assessment",
            "heading": "Hodnocení",
            "text": "Musíte zaplatit pokutu 50000 Kč.",  # unsupported synthesized claim
            "citations_map": [{"start": 6, "end": 9, "provision_id": "FORGED-9", "label": "§ X"}],
        },
    ]
    out = answer_api._shape_answer(ans, lang="cs")
    assert out.refused is False
    assert out.render_mode == "full"
    headings = [s.heading for s in out.sections]
    assert "Legislativní rámec" in headings
    assert "Hodnocení" not in headings  # the forged-prose section never emits
    assert all(s.chips for s in out.sections if s.text.strip())


def test_section_level_every_prose_section_forged_refuses() -> None:
    """If EVERY prose-bearing section's citations are forged (no grounded prose survives),
    the whole answer downgrades to the honest refuse frame."""
    ans = _sound_answer()
    ans["cited_provisions"] = [{"provision_id": "p-1"}]
    # one real flat chip exists, but it backs no surviving prose section
    ans["citations_map"] = [{"start": 0, "end": 5, "provision_id": "p-1", "label": "§ 5"}]
    ans["sections"] = [
        {
            "kind": "legislative",
            "heading": "Legislativní rámec",
            "text": "Viz § 5.",
            "citations_map": [{"start": 0, "end": 5, "provision_id": "FORGED-A", "label": "§ 5"}],
        },
        {
            "kind": "assessment",
            "heading": "Hodnocení",
            "text": "Musíte zaplatit pokutu.",
            "citations_map": [{"start": 6, "end": 9, "provision_id": "FORGED-B", "label": "§ X"}],
        },
    ]
    out = answer_api._shape_answer(ans, lang="cs")
    assert out.refused is True
    assert out.render_mode == "refuse"
    assert out.sections == []


# ---------------------------------------------------------------------------
# 3. quota call-order: check_and_reserve_quota BEFORE pythia.client.answer
# ---------------------------------------------------------------------------
def test_quota_reserve_before_sidecar_call_order() -> None:
    """When entitlements resolve (metering returns an entitlement), the quota reserve
    is invoked BEFORE the sidecar call. We spy on both and assert the order."""
    calls: list[str] = []

    async def spy_reserve(session, org_id, metric, *, limit, now=None):
        calls.append("reserve")
        return 1

    async def spy_answer(*args, **kwargs):
        calls.append("answer")
        return _sound_answer()

    ent = SimpleNamespace(queries_per_day=10, analyses_per_month=3, plan_code="lite")

    async def fake_resolve(org_id, *, session=None):
        return ent

    from regsuper.observability import limits as limits_mod

    with (
        patch.object(answer_api, "get_settings", lambda: _settings()),
        patch.object(answer_api.pythia, "answer", spy_answer),
        patch.object(answer_api.metering, "resolve_for_entry", fake_resolve),
        patch.object(limits_mod, "check_and_reserve_quota", spy_reserve),
    ):
        resp = _client().post("/api/answer", json={"query": "x"})
    assert resp.status_code == 200, resp.text
    assert calls == ["reserve", "answer"], calls


def test_over_quota_429_before_spend() -> None:
    """An over-quota reserve raises QuotaExceeded(429) BEFORE the sidecar call — zero
    spend for the rejected request."""
    from regsuper.observability import limits as limits_mod

    answer_calls: list[str] = []

    async def spy_answer(*args, **kwargs):
        answer_calls.append("answer")
        return _sound_answer()

    async def over_quota(session, org_id, metric, *, limit, now=None):
        raise limits_mod.QuotaExceeded(metric, limit, count=limit + 1)

    ent = SimpleNamespace(queries_per_day=3, analyses_per_month=3, plan_code="lite")

    async def fake_resolve(org_id, *, session=None):
        return ent

    with (
        patch.object(answer_api, "get_settings", lambda: _settings()),
        patch.object(answer_api.pythia, "answer", spy_answer),
        patch.object(answer_api.metering, "resolve_for_entry", fake_resolve),
        patch.object(limits_mod, "check_and_reserve_quota", over_quota),
    ):
        # The handler does not catch QuotaExceeded — TestClient re-raises it (the C2
        # exception-handler maps it to a 429 when wired; here we assert it fired before spend).
        try:
            _client().post("/api/answer", json={"query": "x"})
        except limits_mod.QuotaExceeded as exc:
            assert exc.status_code == 429
        assert answer_calls == [], "sidecar must NOT be called on an over-quota reserve"


# ---------------------------------------------------------------------------
# 3b. AQ-02 — the engine-boundary spend/kill gate raise refunds + maps to a clean 503
# ---------------------------------------------------------------------------
def _fake_session_scope():
    """A DB-free async CM standing in for storage.session_scope so the reserve/refund
    blocks run without Postgres (the reserve + refund helpers are themselves patched)."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm():
        yield object()  # a stand-in session; the patched helpers ignore it

    return _cm()


def _run_gate_raise_test(exc):
    """Drive POST /api/answer with entitlements resolving (so a unit is reserved) and the
    sidecar answer() raising ``exc`` (the AQ-02 pre-call gate). Returns (resp, refund_calls)."""
    from regsuper.observability import limits as limits_mod

    refund_calls: list[str] = []

    async def spy_answer(*args, **kwargs):
        raise exc

    async def spy_reserve(session, org_id, metric, *, limit, now=None):
        return 1

    async def spy_refund(session, *, org_id, reserved_metrics):
        refund_calls.append(",".join(reserved_metrics))

    ent = SimpleNamespace(queries_per_day=10, analyses_per_month=3, plan_code="lite")

    async def fake_resolve(org_id, *, session=None):
        return ent

    with (
        patch.object(answer_api, "get_settings", lambda: _settings()),
        patch.object(answer_api.pythia, "answer", spy_answer),
        patch.object(answer_api.metering, "resolve_for_entry", fake_resolve),
        patch.object(answer_api.metering, "refund_for_run", spy_refund),
        patch.object(limits_mod, "check_and_reserve_quota", spy_reserve),
        patch.object(answer_api, "session_scope", lambda: _fake_session_scope()),
    ):
        resp = _client().post("/api/answer", json={"query": "x"})
    return resp, refund_calls


def test_spend_cap_raise_refunds_and_returns_503() -> None:
    """AQ-02: a SpendLimitExceeded from the engine boundary (pre-call gate, ZERO spend) is
    mapped to a clean 503 'temporarily paused' AND the reserved quota unit is refunded — no
    orphan quota, no ugly 500."""
    from regsuper.observability.limits import SpendLimitExceeded

    resp, refund_calls = _run_gate_raise_test(SpendLimitExceeded("daily cap hit"))
    assert resp.status_code == 503, resp.text
    assert "paused" in resp.json()["detail"]
    assert refund_calls == ["queries"], "the reserved queries unit must be refunded on a gate raise"


def test_kill_switch_raise_refunds_and_returns_503() -> None:
    """AQ-02: a KillSwitchEngaged from the engine boundary is mapped to the same clean 503 and
    refunds the reserved unit."""
    from regsuper.observability.killswitch import KillSwitchEngaged

    resp, refund_calls = _run_gate_raise_test(KillSwitchEngaged("switch on"))
    assert resp.status_code == 503, resp.text
    assert refund_calls == ["queries"], "the reserved queries unit must be refunded on a gate raise"


# ---------------------------------------------------------------------------
# 4. org_id threaded → principal.org_id reaches the sidecar
# ---------------------------------------------------------------------------
def test_org_id_threaded_to_sidecar() -> None:
    p_settings, p_sidecar, sidecar = _patch_handler(_sound_answer())
    with p_settings, p_sidecar:
        resp = _client().post("/api/answer", json={"query": "x"})
    assert resp.status_code == 200, resp.text
    # require_auth=False → the anonymous sentinel org is threaded as org_id.
    _, kwargs = sidecar.call_args
    assert kwargs["org_id"] == _ANON_ORG


def test_as_of_forwarded_to_sidecar() -> None:
    p_settings, p_sidecar, sidecar = _patch_handler(_sound_answer())
    with p_settings, p_sidecar:
        resp = _client().post("/api/answer", json={"query": "x", "as_of": "2024-01-01"})
    assert resp.status_code == 200, resp.text
    _, kwargs = sidecar.call_args
    assert kwargs["as_of"] == "2024-01-01"


# ---------------------------------------------------------------------------
# 5. SINGLE-SHOT — history/messages rejected (422), never folded into query
# ---------------------------------------------------------------------------
def test_single_shot_rejects_history() -> None:
    p_settings, p_sidecar, sidecar = _patch_handler(_sound_answer())
    with p_settings, p_sidecar:
        resp = _client().post(
            "/api/answer",
            json={"query": "x", "history": [{"role": "user", "content": "prior"}]},
        )
    assert resp.status_code == 422
    sidecar.assert_not_called()


def test_single_shot_rejects_messages() -> None:
    p_settings, p_sidecar, sidecar = _patch_handler(_sound_answer())
    with p_settings, p_sidecar:
        resp = _client().post(
            "/api/answer",
            json={"query": "x", "messages": [{"role": "user", "content": "prior"}]},
        )
    assert resp.status_code == 422
    sidecar.assert_not_called()


def test_empty_query_rejected_422() -> None:
    p_settings, p_sidecar, sidecar = _patch_handler(_sound_answer())
    with p_settings, p_sidecar:
        resp = _client().post("/api/answer", json={"query": ""})
    assert resp.status_code == 422
    sidecar.assert_not_called()


# ---------------------------------------------------------------------------
# 6. as_of × NULL valid_from → honest caveat, never a fabricated date
# ---------------------------------------------------------------------------
def test_null_valid_from_shows_caveat_not_fabricated_date() -> None:
    ans = _sound_answer()
    # An undated EU act: cited provision with a NULL valid_from (in-force, no date).
    ans["cited_provisions"] = [{"provision_id": "p-1", "valid_from": None}]
    p_settings, p_sidecar, _ = _patch_handler(ans)
    with p_settings, p_sidecar:
        body = _client().post("/api/answer", json={"query": "x", "as_of": "2024-06-01"}).json()
    assert body["as_of_unknown_note"]
    # The caveat is the honest "effective date not stated" copy — never the as_of value.
    assert "2024-06-01" not in body["as_of_unknown_note"]
    note_lc = body["as_of_unknown_note"].lower()
    assert "datum" in note_lc and "neuvádíme" in note_lc


def test_dated_provision_no_caveat() -> None:
    ans = _sound_answer()
    ans["cited_provisions"] = [{"provision_id": "p-1", "valid_from": "2018-05-25"}]
    p_settings, p_sidecar, _ = _patch_handler(ans)
    with p_settings, p_sidecar:
        body = _client().post("/api/answer", json={"query": "x"}).json()
    assert body["as_of_unknown_note"] == ""


# ---------------------------------------------------------------------------
# 7. non-sound routes render the honest frame + machine reason
# ---------------------------------------------------------------------------
def test_not_a_legal_question_frame() -> None:
    ans = {
        "refused": True,
        "render_mode": "notlegal",
        "drafted_answer": None,
        "cited_provisions": [],
        "citations_map": [],
        "meta": {"route": "not_a_legal_question", "intent": "not_legal"},
    }
    p_settings, p_sidecar, _ = _patch_handler(ans)
    with p_settings, p_sidecar:
        body = _client().post("/api/answer", json={"query": "jaké je počasí?"}).json()
    assert body["render_mode"] == "notlegal"
    assert body["refused"] is True
    assert body["reason"] == "out_of_scope"
    assert body["title"] and body["body"]
    assert body["chips"] == []


def test_false_premise_frame_keeps_grounded_correction_chips() -> None:
    ans = {
        "refused": True,
        "render_mode": "falsepremise",
        "drafted_answer": "Oprava: ...",
        "cited_provisions": [{"provision_id": "corr-1"}],
        "citations_map": [{"start": 0, "end": 6, "provision_id": "corr-1", "label": "§ 1"}],
        "meta": {
            "route": "false_premise",
            "correcting_citations": [{"provision_id": "corr-1"}],
        },
    }
    p_settings, p_sidecar, _ = _patch_handler(ans)
    with p_settings, p_sidecar:
        body = _client().post("/api/answer", json={"query": "x"}).json()
    assert body["render_mode"] == "falsepremise"
    assert body["reason"] == "false_premise"
    assert [c["provision_id"] for c in body["chips"]] == ["corr-1"]
    assert body["title"]


def test_hedged_band_adds_hedge_banner() -> None:
    ans = _sound_answer()
    ans["render_mode"] = "hedged"
    ans["band"] = "hedged"
    p_settings, p_sidecar, _ = _patch_handler(ans)
    with p_settings, p_sidecar:
        body = _client().post("/api/answer", json={"query": "x"}).json()
    assert body["render_mode"] == "hedged"
    assert body["refused"] is False
    assert body["hedge_banner"]  # the amber lower-confidence strip
    assert len(body["chips"]) == 1  # still a sound, grounded answer


# ---------------------------------------------------------------------------
# 8. pure-shaping coverage (DB-free, no client)
# ---------------------------------------------------------------------------
def test_shape_unknown_render_mode_degrades_to_full_when_grounded() -> None:
    ans = _sound_answer()
    ans["render_mode"] = None  # metagate OFF → no render_mode
    out = answer_api._shape_answer(ans, lang="cs")
    assert out.render_mode == "full"
    assert [c.provision_id for c in out.chips] == ["p-1"]


def test_shape_engine_refused_without_mode_is_refuse() -> None:
    ans = {
        "refused": True,
        "render_mode": None,
        "drafted_answer": None,
        "cited_provisions": [],
        "citations_map": [],
    }
    out = answer_api._shape_answer(ans, lang="cs")
    assert out.render_mode == "refuse"
    assert out.refused is True


# ---------------------------------------------------------------------------
# 9. shadow weak-coverage HEDGED surfacing (operator decision 2026-06-30; flag-gated)
# ---------------------------------------------------------------------------
def test_shadow_weak_coverage_upgraded_to_hedged_when_flag_on() -> None:
    """Flag ON + shadow + weak_coverage + engine-not-refused + verdict ok ⇒ the grounded
    draft is SURFACED as a hedged answer (sections + chips + hedge banner), not refused."""
    with patch.object(
        answer_api,
        "get_settings",
        lambda: _settings(answer_shadow_weak_coverage_hedged=True),
    ):
        out = answer_api._shape_answer(_weak_coverage_answer(), lang="cs")
    assert out.render_mode == "hedged"
    assert out.band == "hedged"  # not the engine's stale advisory 'closest'
    assert out.refused is False
    assert [c.provision_id for c in out.chips] == ["p-1"]
    assert any(s.text.strip() for s in out.sections)  # prose surfaced
    assert out.hedge_banner  # the "coverage may be incomplete" strip


def test_shadow_weak_coverage_refused_when_flag_off() -> None:
    """Flag OFF (the default) ⇒ byte-identical legacy behavior: weak_coverage still refuses
    with the closest-provisions frame (no synthesized sections)."""
    with patch.object(
        answer_api,
        "get_settings",
        lambda: _settings(answer_shadow_weak_coverage_hedged=False),
    ):
        out = answer_api._shape_answer(_weak_coverage_answer(), lang="cs")
    assert out.render_mode == "closest"
    assert out.refused is True
    assert out.sections == []


def test_no_coverage_not_upgraded_even_with_flag_on() -> None:
    """SAFETY SCOPING: no_coverage (the off-corpus / fake-law case whose draft cites
    UNRELATED law) stays refused even with the flag ON — only weak_coverage upgrades."""
    with patch.object(
        answer_api,
        "get_settings",
        lambda: _settings(answer_shadow_weak_coverage_hedged=True),
    ):
        out = answer_api._shape_answer(_weak_coverage_answer(coverage="no_coverage"), lang="cs")
    assert out.render_mode == "closest"
    assert out.refused is True
    assert out.sections == []


def test_enforce_mode_weak_coverage_not_upgraded() -> None:
    """The upgrade is shadow-only — an enforce-mode shape is never upgraded by the app
    (enforce gating is the engine's job; the app must not second-guess it)."""
    with patch.object(
        answer_api,
        "get_settings",
        lambda: _settings(answer_shadow_weak_coverage_hedged=True),
    ):
        out = answer_api._shape_answer(_weak_coverage_answer(metagate_mode="enforce"), lang="cs")
    assert out.render_mode == "closest"
    assert out.refused is True


# ---------------------------------------------------------------------------
# Track A / A3 STAGE-1 — the app-side DETERMINISTIC pre-quota clarify
# ---------------------------------------------------------------------------
def test_clarify_flag_off_is_byte_identical_no_clarify() -> None:
    """Flag OFF ⇒ NO clarify: a thin employment query (no profile) proceeds straight to
    the engine and returns the normal shaped answer (byte-identical to today)."""
    sidecar = AsyncMock(return_value=_sound_answer())
    with (
        patch.object(
            answer_api,
            "get_settings",
            lambda: _settings(answer_clarify_enabled=False),
        ),
        patch.object(answer_api.pythia, "answer", sidecar),
    ):
        resp = _client().post("/api/answer", json={"query": "Musím dát zaměstnanci výpověď?"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["render_mode"] == "full"
    assert body["questions"] == []
    sidecar.assert_called_once()  # the engine WAS called (no clarify short-circuit)


def test_clarify_flag_on_missing_slot_returns_clarify_no_spend() -> None:
    """Flag ON + a matter with a missing REQUIRED slot + NO slots (first turn) ⇒ 200
    clarify frame: ≤3 questions, disclaimer + referral present, quota NOT reserved, engine
    NOT called."""
    from regsuper.observability import limits as limits_mod

    reserve_calls: list[str] = []

    async def spy_reserve(session, org_id, metric, *, limit, now=None):
        reserve_calls.append("reserve")
        return 1

    sidecar = AsyncMock(return_value=_sound_answer())

    # entitlement resolves so that IF quota were reached it would reserve — proving the
    # clarify short-circuits BEFORE the reserve.
    ent = SimpleNamespace(queries_per_day=10, analyses_per_month=3, plan_code="lite")

    async def fake_resolve(org_id, *, session=None):
        return ent

    with (
        patch.object(
            answer_api,
            "get_settings",
            lambda: _settings(answer_clarify_enabled=True),
        ),
        patch.object(answer_api.pythia, "answer", sidecar),
        patch.object(answer_api.metering, "resolve_for_entry", fake_resolve),
        patch.object(limits_mod, "check_and_reserve_quota", spy_reserve),
    ):
        resp = _client().post("/api/answer", json={"query": "Musím dát zaměstnanci výpověď?"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["render_mode"] == "clarify"
    assert body["refused"] is False
    assert body["matter_type"] == "employment"
    assert 1 <= len(body["questions"]) <= 3
    assert all(q["text"] and q["slot"] for q in body["questions"])
    assert body["disclaimer"]  # information-not-advice present
    # referral line renders (body + reason)
    assert "advokát" in body["body"].lower()
    assert body["reason"]
    # NO spend: quota NOT reserved, engine NOT called
    assert reserve_calls == []
    sidecar.assert_not_called()


def test_clarify_flag_on_slots_provided_proceeds_no_second_clarify() -> None:
    """Flag ON + a `slots` dict (the follow-up turn) ⇒ NEVER clarifies again — proceeds to
    quota+engine even if still incomplete (MAX 1 round)."""
    sidecar = AsyncMock(return_value=_sound_answer())
    with (
        patch.object(
            answer_api,
            "get_settings",
            lambda: _settings(answer_clarify_enabled=True),
        ),
        patch.object(answer_api.pythia, "answer", sidecar),
    ):
        # only ONE of the 3 employment slots supplied → still incomplete, but slots present
        resp = _client().post(
            "/api/answer",
            json={"query": "Musím dát zaměstnanci výpověď?", "slots": {"is_employer": "yes"}},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["render_mode"] == "full"  # engine answered, not a clarify
    assert body["questions"] == []
    sidecar.assert_called_once()


def test_clarify_flag_on_general_matter_no_clarify() -> None:
    """A query typing to `general` (no required slots) never clarifies, even flag ON."""
    sidecar = AsyncMock(return_value=_sound_answer())
    with (
        patch.object(
            answer_api,
            "get_settings",
            lambda: _settings(answer_clarify_enabled=True),
        ),
        patch.object(answer_api.pythia, "answer", sidecar),
    ):
        resp = _client().post("/api/answer", json={"query": "Jaké je počasí dnes?"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["render_mode"] == "full"
    sidecar.assert_called_once()


def test_clarify_bad_slots_rejected_422() -> None:
    """A follow-up with an invalid slot value is rejected 422 (validated against the enums)
    while extra='forbid' guards the top-level shape."""
    sidecar = AsyncMock(return_value=_sound_answer())
    with (
        patch.object(
            answer_api,
            "get_settings",
            lambda: _settings(answer_clarify_enabled=True),
        ),
        patch.object(answer_api.pythia, "answer", sidecar),
    ):
        resp = _client().post(
            "/api/answer",
            json={"query": "Musím dát zaměstnanci výpověď?", "slots": {"legal_form": "nope"}},
        )
    assert resp.status_code == 422
    sidecar.assert_not_called()


def test_input_compliance_gate_precedes_clarify() -> None:
    """HARD PIN: an enforce-mode reserved_advice INPUT-gate refusal short-circuits BEFORE
    any clarify — a compliance_guard=enforce request gets the lawyer-handoff refuse frame,
    NOT a clarify prompt."""
    reserved_verdict = SimpleNamespace(answer_mode="reserved_advice", confidence=1.0)

    async def fake_classify(*, text, where, org_id=None, project_id=None):
        return reserved_verdict

    sidecar = AsyncMock(return_value=_sound_answer())
    with (
        patch.object(
            answer_api,
            "get_settings",
            lambda: _settings(
                answer_clarify_enabled=True,
                compliance_guard_enabled=True,
                compliance_guard_mode="enforce",
            ),
        ),
        patch.object(answer_api, "classify_answer_mode", fake_classify),
        patch.object(answer_api.pythia, "answer", sidecar),
    ):
        # a query that WOULD otherwise clarify (thin employment) — the enforce refusal wins
        resp = _client().post("/api/answer", json={"query": "Vyřiďte za mě výpověď zaměstnanci."})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["render_mode"] == "refuse"
    assert body["refused"] is True
    assert body["questions"] == []  # NOT a clarify
    sidecar.assert_not_called()


# ---------------------------------------------------------------------------
# B1c — the STRUCTURED practical block: byte-identical OFF/absent, AC-checked,
# gaps pass through, gap-frequency telemetry.
# ---------------------------------------------------------------------------
def _practical_answer(*, items=None, gaps=None, demoted=None, profile_used=False) -> dict:
    """A sound answer that ALSO carries the engine's structured `.practical` sidecar. Items default
    to one grounded (p-1) + one forged (FORGED-9) so the AC-CITATION-CHECK is exercised."""
    ans = _sound_answer()
    # keep only the grounded p-1 (drop the flat forged chip noise from _sound_answer for clarity)
    ans["cited_provisions"] = [{"provision_id": "p-1"}]
    ans["citations_map"] = [{"start": 0, "end": 5, "provision_id": "p-1", "label": "§ 5"}]
    ans["sections"] = [
        {
            "kind": "legislative",
            "heading": "Legislativní rámec",
            "text": "Viz § 5.",
            "citations_map": [{"start": 0, "end": 5, "provision_id": "p-1", "label": "§ 5"}],
        },
    ]
    ans["practical"] = {
        "items": items
        if items is not None
        else [
            {
                "slot": "obligations",
                "fact_type": "deadline",
                "value_raw": "do 8 dnů",
                "provision_id": "p-1",
                "grounded": True,
            },
            {
                "slot": "steps",
                "fact_type": "authority",
                "value_raw": "u úřadu",
                "provision_id": "FORGED-9",
                "grounded": True,
            },
        ],
        "gaps": gaps
        if gaps is not None
        else [
            {
                "gap_class": "office_registry",
                "text": "Rejstřík úřadů není v korpusu.",
                "status": "known_gap",
            },
        ],
        "demoted_slots": demoted if demoted is not None else [],
        "profile_used": profile_used,
    }
    return ans


def test_practical_absent_is_byte_identical_none() -> None:
    """No `.practical` in the engine dict ⇒ AnswerResponse.practical is None even with the flag ON
    (byte-identical: the field defaults None and nothing is fabricated)."""
    with patch.object(
        answer_api, "get_settings", lambda: _settings(answer_practical_block_enabled=True)
    ):
        out = answer_api._shape_answer(_sound_answer(), lang="cs")
    assert out.practical is None


def test_practical_present_flag_off_is_none() -> None:
    """The engine sent `.practical` but the app flag is OFF ⇒ practical stays None (byte-identical)."""
    with patch.object(
        answer_api, "get_settings", lambda: _settings(answer_practical_block_enabled=False)
    ):
        out = answer_api._shape_answer(_practical_answer(), lang="cs")
    assert out.practical is None


def test_practical_ungrounded_item_dropped_and_slot_demoted() -> None:
    """AC-CITATION-CHECK on items: an item whose provision_id is NOT grounded is DROPPED from items
    and its slot recorded in demoted_slots; the grounded item survives; gaps pass through."""
    with patch.object(
        answer_api, "get_settings", lambda: _settings(answer_practical_block_enabled=True)
    ):
        out = answer_api._shape_answer(_practical_answer(), lang="cs")
    assert out.practical is not None
    pids = [it.provision_id for it in out.practical.items]
    assert pids == ["p-1"]  # only the grounded item survives
    assert "FORGED-9" not in pids
    assert (
        "steps" in out.practical.demoted_slots
    )  # the forged item's slot is demoted (honest non-claim)
    assert len(out.practical.gaps) == 1  # gaps pass through untouched


def test_practical_none_provision_item_kept() -> None:
    """An item with provision_id=None makes no per-provision citation claim → it is KEPT (no forged
    claim to drop)."""
    ans = _practical_answer(
        items=[
            {
                "slot": "applies",
                "fact_type": "role",
                "value_raw": "zaměstnavatel",
                "provision_id": None,
            },
        ]
    )
    with patch.object(
        answer_api, "get_settings", lambda: _settings(answer_practical_block_enabled=True)
    ):
        out = answer_api._shape_answer(ans, lang="cs")
    assert out.practical is not None
    assert len(out.practical.items) == 1
    assert out.practical.items[0].provision_id is None
    assert out.practical.demoted_slots == []


def test_practical_engine_demoted_slots_pass_through() -> None:
    """The engine's own demoted_slots pass through and merge with any app-side demotions (no dup)."""
    ans = _practical_answer(
        demoted=["risks"],
        items=[
            {
                "slot": "steps",
                "fact_type": "authority",
                "value_raw": "x",
                "provision_id": "FORGED-9",
            },
        ],
    )
    with patch.object(
        answer_api, "get_settings", lambda: _settings(answer_practical_block_enabled=True)
    ):
        out = answer_api._shape_answer(ans, lang="cs")
    assert out.practical is not None
    assert "risks" in out.practical.demoted_slots  # engine demotion preserved
    assert "steps" in out.practical.demoted_slots  # app-side demotion added
    assert out.practical.items == []  # the only item was forged → dropped


def test_practical_not_attached_on_refused_frame() -> None:
    """A refused/non-sound answer surfaces NO practical block (no practical claims on a refusal)."""
    ans = _practical_answer()
    ans["render_mode"] = "refuse"
    ans["refused"] = True
    with patch.object(
        answer_api, "get_settings", lambda: _settings(answer_practical_block_enabled=True)
    ):
        out = answer_api._shape_answer(ans, lang="cs")
    assert out.refused is True
    assert out.practical is None


def test_practical_gap_telemetry_emitted() -> None:
    """Handler-level: one `answer.practical.gap` event is emitted per gap in the surfaced block."""
    emitted: list[tuple[str, dict]] = []

    async def fake_emit(code, **kw):
        emitted.append((code, kw))
        return {}

    ans = _practical_answer(
        gaps=[
            {"gap_class": "office_registry", "text": "a", "status": "known_gap"},
            {"gap_class": "decrees", "text": "b", "status": "known_gap"},
        ]
    )
    sidecar = AsyncMock(return_value=ans)
    with (
        patch.object(
            answer_api, "get_settings", lambda: _settings(answer_practical_block_enabled=True)
        ),
        patch.object(answer_api.pythia, "answer", sidecar),
        patch.object(answer_api.events, "emit", fake_emit),
    ):
        resp = _client().post("/api/answer", json={"query": "Kdy musím podat?"})
    assert resp.status_code == 200, resp.text
    gap_events = [kw for code, kw in emitted if code == "answer.practical.gap"]
    assert len(gap_events) == 2
    assert {e["gap_class"] for e in gap_events} == {"office_registry", "decrees"}


def test_practical_no_gap_telemetry_when_flag_off() -> None:
    """Flag OFF ⇒ no practical block ⇒ no gap telemetry (byte-identical)."""
    emitted: list[str] = []

    async def fake_emit(code, **kw):
        emitted.append(code)
        return {}

    sidecar = AsyncMock(return_value=_practical_answer())
    with (
        patch.object(
            answer_api, "get_settings", lambda: _settings(answer_practical_block_enabled=False)
        ),
        patch.object(answer_api.pythia, "answer", sidecar),
        patch.object(answer_api.events, "emit", fake_emit),
    ):
        resp = _client().post("/api/answer", json={"query": "Kdy musím podat?"})
    assert resp.status_code == 200, resp.text
    assert "answer.practical.gap" not in emitted


# ---------------------------------------------------------------------------
# T7 — the UNDERSTANDING PANEL (audit_trace["intake"] → whitelisted intake block)
# ---------------------------------------------------------------------------
def _intake_answer(*, intake=None) -> dict:
    """A sound answer that ALSO carries the engine's intake_v2 artifact on audit_trace["intake"]."""
    ans = _sound_answer()
    if intake is None:
        intake = {
            "sub_questions": [
                "Jaká je právní forma podnikání?",
                "Zpracovává osobní údaje?",
            ],
            "facts": [
                {"field": "legal_form", "value": "sro", "source": "det"},
                {"field": "processes_personal_data", "value": "true", "source": "llm"},
                {"field": "nace_code", "value": "62.01", "source": "profile"},
                # a NON-whitelisted field (free-text-ish) that MUST be dropped:
                {"field": "free_text_note", "value": "the asker rambled here", "source": "llm"},
                # a query-shape field: surfaced in the panel but NOT a profile suggestion:
                {"field": "obligation_type", "value": "registration", "source": "det"},
            ],
            # VL-A-10: the REAL engine shape — profile_conflicts = [{field, profile_value, query_value}].
            "profile_conflicts": [
                {"field": "headcount_band", "profile_value": "12", "query_value": "5"},
                # a conflict about a NON-whitelisted field → the whole conflict is DROPPED:
                {"field": "free_text_note", "profile_value": "x", "query_value": "y"},
            ],
            "sufficiency": "sufficient",
            # VL-A-10: the REAL IVVerdict shape {item_id, verifier, status, reason, evidence}; a DROP is
            # status=='drop', the human field is evidence['field'] (IV-ENUM) else the item_id.
            "iv_verdicts": [
                {
                    "item_id": "fact:3",
                    "verifier": "IV-ENUM",
                    "status": "drop",
                    "reason": "low confidence extraction",
                    "evidence": {"field": "turnover_band"},
                },
                {
                    "item_id": "fact:0",
                    "verifier": "IV-ENUM",
                    "status": "pass",
                    "reason": "deterministic",
                    "evidence": {"field": "legal_form"},
                },
            ],
        }
    ans["audit_trace"] = {**(ans.get("audit_trace") or {}), "intake": intake}
    return ans


def test_intake_flag_off_is_none_byte_identical() -> None:
    """Flag OFF ⇒ AnswerResponse.intake is None and profile_suggestions empty even when the engine
    attached audit_trace["intake"] (byte-identical baseline)."""
    with patch.object(
        answer_api, "get_settings", lambda: _settings(answer_intake_panel_enabled=False)
    ):
        out = answer_api._shape_answer(_intake_answer(), lang="cs")
    assert out.intake is None
    assert out.profile_suggestions == []


def test_intake_absent_artifact_is_none() -> None:
    """No audit_trace["intake"] in the engine dict ⇒ intake stays None even with the flag ON."""
    with patch.object(
        answer_api, "get_settings", lambda: _settings(answer_intake_panel_enabled=True)
    ):
        out = answer_api._shape_answer(_sound_answer(), lang="cs")
    assert out.intake is None
    assert out.profile_suggestions == []


def test_intake_present_flag_on_surfaces_whitelisted() -> None:
    """Flag ON + artifact ⇒ the whitelisted intake block is surfaced (sub_questions, whitelisted facts
    badged by source, conflicts, sufficiency, dropped[]); the non-whitelisted field is DROPPED."""
    with patch.object(
        answer_api, "get_settings", lambda: _settings(answer_intake_panel_enabled=True)
    ):
        out = answer_api._shape_answer(_intake_answer(), lang="cs")
    assert out.intake is not None
    assert out.intake.sub_questions == [
        "Jaká je právní forma podnikání?",
        "Zpracovává osobní údaje?",
    ]
    fields = {f.field for f in out.intake.facts}
    assert "legal_form" in fields and "processes_personal_data" in fields and "nace_code" in fields
    assert "obligation_type" in fields  # query-shape field surfaced in the panel
    assert "free_text_note" not in fields  # non-whitelisted field DROPPED (PII defence)
    # source badge preserved
    lf = next(f for f in out.intake.facts if f.field == "legal_form")
    assert lf.value == "sro" and lf.source == "det"
    assert out.intake.sufficiency == "sufficient"
    # conflicts are whitelisted to {field, values}: the non-whitelisted-field conflict is dropped, and
    # the free-text `detail` never surfaces (only the candidate values do).
    assert len(out.intake.conflicts) == 1  # the non-whitelisted-field conflict is dropped
    conf = out.intake.conflicts[0]
    assert conf.field == "headcount_band" and conf.values == [
        "12",
        "5",
    ]  # [profile_value, query_value]
    assert not hasattr(conf, "detail")
    # dropped derived from iv_verdicts (real shape): the status=='drop' verdict surfaces, 'pass' does not
    dropped_fields = {d.field for d in out.intake.dropped}
    assert dropped_fields == {"turnover_band"}  # from evidence['field']
    assert out.intake.dropped[0].reason == "low confidence extraction"


def test_intake_profile_suggestions_derived_from_facts() -> None:
    """profile_suggestions are derived from the whitelisted intake facts, restricted to PROFILE columns
    (the query-shape obligation_type is NOT suggested)."""
    with patch.object(
        answer_api, "get_settings", lambda: _settings(answer_intake_panel_enabled=True)
    ):
        out = answer_api._shape_answer(_intake_answer(), lang="cs")
    sug = {s.field: s.value for s in out.profile_suggestions}
    assert sug.get("legal_form") == "sro"
    assert sug.get("processes_personal_data") == "true"
    assert sug.get("nace_code") == "62.01"
    assert "obligation_type" not in sug  # query-shape, not a standing-profile column
    assert "free_text_note" not in sug  # never whitelisted


def test_intake_dropped_from_iv_verdicts_drop_status() -> None:
    """VL-A-10: the dropped list is derived from the REAL iv_verdicts — a status=='drop' verdict surfaces
    (field from evidence['field'], else the item_id); a non-drop status does not. No back-compat `dropped` key
    (the engine never emits one)."""
    ans = _intake_answer(
        intake={
            "sub_questions": [],
            "facts": [],
            "iv_verdicts": [
                {
                    "item_id": "fact:1",
                    "verifier": "IV-ENUM",
                    "status": "drop",
                    "reason": "no signal in query",
                    "evidence": {"field": "cross_border"},
                },
                {
                    "item_id": "fact:2",
                    "verifier": "IV-SPAN",
                    "status": "pass",
                    "reason": "grounded",
                },
                # a drop with NO evidence field ⇒ the item_id is the safe (non-PII) identifier
                {
                    "item_id": "subq:0",
                    "verifier": "IV-LANG",
                    "status": "drop",
                    "reason": "duplicate",
                },
            ],
        }
    )
    with patch.object(
        answer_api, "get_settings", lambda: _settings(answer_intake_panel_enabled=True)
    ):
        out = answer_api._shape_answer(ans, lang="cs")
    assert out.intake is not None
    fields = [d.field for d in out.intake.dropped]
    assert fields == ["cross_border", "subq:0"]  # drop-status only; field or item_id fallback
    assert out.intake.dropped[0].reason == "no signal in query"


def test_intake_unknown_shape_is_safe() -> None:
    """A malformed intake artifact (non-dict facts, missing keys) never crashes → empty-but-present block."""
    ans = _intake_answer(intake={"facts": "not a list", "sub_questions": [None, "  ", "ok"]})
    with patch.object(
        answer_api, "get_settings", lambda: _settings(answer_intake_panel_enabled=True)
    ):
        out = answer_api._shape_answer(ans, lang="cs")
    assert out.intake is not None
    assert out.intake.facts == []
    assert out.intake.sub_questions == ["ok"]  # blanks/None filtered


def test_intake_request_json_stays_empty_not_persisted() -> None:
    """WP-029: the intake artifact is read off the RESPONSE only and is NEVER persisted into the boundary
    cost row's request_json — the app never writes the artifact to any cost row (the boundary client
    hardcodes request_json={}). Here we assert the handler surfaces intake WITHOUT ever passing it back
    into pythia.answer's request payload (the mocked sidecar receives only the query/kwargs)."""
    ans = _intake_answer()
    sidecar = AsyncMock(return_value=ans)
    with (
        patch.object(
            answer_api, "get_settings", lambda: _settings(answer_intake_panel_enabled=True)
        ),
        patch.object(answer_api.pythia, "answer", sidecar),
    ):
        resp = _client().post("/api/answer", json={"query": "musím se registrovat?"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intake"] is not None
    assert body["intake"]["facts"]  # surfaced
    # the sidecar call carried NO intake artifact in its request (the app never echoes it back)
    _, kwargs = sidecar.call_args
    assert "intake" not in kwargs
    assert "audit_trace" not in kwargs


# ---------------------------------------------------------------------------
# W4 — the competent-authority "kam se obrátit / where" slot (authority_registry_enabled)
# ---------------------------------------------------------------------------
from pathlib import Path as _Path  # noqa: E402

_AUTH_FIX = _Path(__file__).resolve().parents[1] / "fixtures" / "authority"
_SEED = str(_AUTH_FIX / "authority_registry.sample.json")
_TOPICS = str(_AUTH_FIX / "topic_authority.sample.json")
_GAZ = str(_AUTH_FIX / "municipality_gazetteer.sample.json")


def _auth_settings(**kw):
    """Settings with the authority slot ON + the DRAFT sample seed/map/gazetteer loaded."""
    return _settings(
        authority_registry_enabled=True,
        authority_seed_path=_SEED,
        authority_topic_map_path=_TOPICS,
        authority_gazetteer_path=_GAZ,
        **kw,
    )


def _answer_with_facts(*, obligation=None, municipality=None) -> dict:
    """A sound answer carrying the whitelisted intake facts the W4 slot derives topic/municipality from."""
    ans = _sound_answer()
    facts = []
    if obligation is not None:
        facts.append({"field": "obligation_type", "value": obligation, "source": "det"})
    if municipality is not None:
        facts.append({"field": "municipality_code", "value": municipality, "source": "det"})
    ans["audit_trace"] = {"intake": {"facts": facts}}
    return ans


def test_authority_flag_off_is_empty_byte_identical() -> None:
    """Flag OFF (default) ⇒ resp.authorities stays empty even when the engine carried a mappable
    obligation + a known municipality (byte-identical: the where slot behaves exactly as today)."""
    answer_api._reset_authority_registry_cache()
    ans = _answer_with_facts(obligation="tax", municipality="582786")
    sidecar = AsyncMock(return_value=ans)
    with (
        patch.object(answer_api, "get_settings", lambda: _settings()),
        patch.object(answer_api.pythia, "answer", sidecar),
    ):
        resp = _client().post("/api/answer", json={"query": "Kam mám podat daň?"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["authorities"] == []


def test_authority_flag_on_surfaces_concrete_office() -> None:
    """Flag ON + loaded sample seed + a known municipality + a mappable obligation ⇒ the where slot carries
    the concrete competent office (WHITELISTED shape, DRAFT posture)."""
    answer_api._reset_authority_registry_cache()
    ans = _answer_with_facts(obligation="tax", municipality="582786")  # Brno → FÚ okres CZ0642
    sidecar = AsyncMock(return_value=ans)
    with (
        patch.object(answer_api, "get_settings", lambda: _auth_settings()),
        patch.object(answer_api.pythia, "answer", sidecar),
    ):
        resp = _client().post("/api/answer", json={"query": "Kam mám podat daň z příjmu?"})
    assert resp.status_code == 200, resp.text
    auth = resp.json()["authorities"]
    assert len(auth) == 1
    a = auth[0]
    assert a["authority_type"] == "financni_urad"
    assert a["match_kind"] == "okres"
    assert a["is_draft"] is True
    assert "Finanční úřad" in a["name"]
    # WHITELISTED: no free-text address / databox / source_uri leaks into the surface.
    assert set(a.keys()) == {"authority_type", "name", "match_kind", "is_draft"}
    answer_api._reset_authority_registry_cache()


def test_authority_flag_on_unknown_topic_is_empty() -> None:
    """Flag ON but the obligation maps to no topic_key ⇒ honest empty (never a guessed office)."""
    answer_api._reset_authority_registry_cache()
    ans = _answer_with_facts(obligation="nonsense_obligation", municipality="582786")
    sidecar = AsyncMock(return_value=ans)
    with (
        patch.object(answer_api, "get_settings", lambda: _auth_settings()),
        patch.object(answer_api.pythia, "answer", sidecar),
    ):
        resp = _client().post("/api/answer", json={"query": "x"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["authorities"] == []
    answer_api._reset_authority_registry_cache()


def test_authority_flag_on_no_municipality_national_only() -> None:
    """Flag ON + a mappable GDPR obligation but NO municipality ⇒ only the national office (ÚOOÚ) resolves;
    a territorial-only topic (tax) with no municipality resolves nothing."""
    answer_api._reset_authority_registry_cache()
    # national: gdpr → ÚOOÚ resolves even with no municipality
    ans_gdpr = _answer_with_facts(obligation="gdpr", municipality=None)
    with (
        patch.object(answer_api, "get_settings", lambda: _auth_settings()),
        patch.object(answer_api.pythia, "answer", AsyncMock(return_value=ans_gdpr)),
    ):
        resp = _client().post("/api/answer", json={"query": "GDPR?"})
    got = resp.json()["authorities"]
    assert [a["authority_type"] for a in got] == ["uoou"]
    assert got[0]["match_kind"] == "national"
    answer_api._reset_authority_registry_cache()
    # territorial-only: tax with no municipality → empty
    ans_tax = _answer_with_facts(obligation="tax", municipality=None)
    with (
        patch.object(answer_api, "get_settings", lambda: _auth_settings()),
        patch.object(answer_api.pythia, "answer", AsyncMock(return_value=ans_tax)),
    ):
        resp = _client().post("/api/answer", json={"query": "daň?"})
    assert resp.json()["authorities"] == []
    answer_api._reset_authority_registry_cache()


def test_authority_flag_on_empty_seed_never_guesses() -> None:
    """Flag ON but NO seed path configured (a bare flip) ⇒ the office index is EMPTY ⇒ no office is surfaced
    even for a mappable topic + known municipality (never a guessed office)."""
    answer_api._reset_authority_registry_cache()
    ans = _answer_with_facts(obligation="tax", municipality="582786")
    # topic map + gazetteer configured, but seed path unset → empty office registry
    settings = _settings(
        authority_registry_enabled=True,
        authority_seed_path=None,
        authority_topic_map_path=_TOPICS,
        authority_gazetteer_path=_GAZ,
    )
    with (
        patch.object(answer_api, "get_settings", lambda: settings),
        patch.object(answer_api.pythia, "answer", AsyncMock(return_value=ans)),
    ):
        resp = _client().post("/api/answer", json={"query": "daň?"})
    assert resp.json()["authorities"] == []
    answer_api._reset_authority_registry_cache()


def test_authority_not_attached_on_refused_frame() -> None:
    """A refused/non-sound answer surfaces NO where-slot office (no practical claim on a refusal)."""
    answer_api._reset_authority_registry_cache()
    ans = _answer_with_facts(obligation="tax", municipality="582786")
    ans["render_mode"] = "refuse"
    ans["refused"] = True
    with (
        patch.object(answer_api, "get_settings", lambda: _auth_settings()),
        patch.object(answer_api.pythia, "answer", AsyncMock(return_value=ans)),
    ):
        resp = _client().post("/api/answer", json={"query": "x"})
    body = resp.json()
    assert body["refused"] is True
    assert body["authorities"] == []
    answer_api._reset_authority_registry_cache()


def test_authority_municipality_from_profile_context() -> None:
    """The municipality is taken from the loaded profile AnswerContext when present (preferred over the
    intake fact) — verified at the pure helper level."""
    answer_api._reset_authority_registry_cache()
    ans = _answer_with_facts(obligation="tax", municipality=None)  # no intake municipality
    ctx = SimpleNamespace(municipality_code="582786")  # Brno from the profile
    with patch.object(answer_api, "get_settings", lambda: _auth_settings()):
        got = answer_api._resolve_authorities(ans, ctx)
    assert [a.authority_type for a in got] == ["financni_urad"]
    assert got[0].match_kind == "okres"
    answer_api._reset_authority_registry_cache()
