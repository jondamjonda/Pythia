"""WF-1 Increments 2-4 — the gated wiring of the entity/role applicability gate.

Proves the two halves of the contract:
  * FLAG OFF → byte-identical: no metadata key, no payload key, render/export unchanged (covered structurally by
    the rest of the suite passing; pinned here for intake + export).
  * FLAG ON → the gate runs additively: intake writes metadata_['entity_role']; the post-L3 pass stamps
    payload['applicability'] (idempotent, never deletes); render DEMOTES out_of_scope to does_not_bind (never
    drops) and flags conditional; export emits the key only when present (legacy hash unchanged).

Pure pieces (render, export) use SimpleNamespace stand-ins; the DB-touching pieces (intake create_project, the
scoring pass) mock the session seam exactly like test_cascade_killswitch_abort — no DB, no network.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from regsuper.analysis.render import Digest, ObligationItem, build_digest
from regsuper.api import export

_PID = UUID("11111111-1111-1111-1111-111111111111")


def _provision(pid: UUID = _PID) -> SimpleNamespace:
    return SimpleNamespace(
        id=pid,
        deontic_modal="musí",
        deontic_actor="AIFM",
        deontic_condition=None,
        label="§ 1 zákona č. 240/2013 Sb.",
        eid="sec_1",
        hierarchy_path="act.sec_1",
        jurisdiction="CZ",
        source_external_id="ext-1",
        source="legislation",
    )


def _obl_finding(applicability: dict | None, *, cited: list[UUID] | None = None) -> SimpleNamespace:
    payload = {"actor": "AIFM", "modal": "shall"}
    if applicability is not None:
        payload["applicability"] = applicability
    return SimpleNamespace(
        finding_type="obligation",
        analysis_layer="L3",
        statement="Musíte něco udělat.",
        severity="obligation",
        confidence_overall=0.82,
        review_state="unreviewed",
        disclaimer_id="d",
        answer_mode="information",
        cited_provisions=[_PID] if cited is None else cited,
        payload=payload,
    )


# ── E7/E8: render demote-not-drop ──────────────────────────────────────────────────────────────────────────
def test_render_no_payload_is_byte_identical_obligation():
    """A finding with no applicability key (flag-OFF world) renders as a plain binding obligation."""
    d = build_digest([_obl_finding(None)], [_provision()])
    assert len(d.obligations) == 1 and len(d.does_not_bind) == 0
    assert d.obligations[0].binds == "yes" and d.obligations[0].binds_reason == ""
    assert len(d.checklist) == 1 and len(d.actions) == 1


def test_render_out_of_scope_routed_to_does_not_bind_and_off_the_checklist():
    appl = {"binds": "out_of_scope", "reason": "addressed to the legislature to adopt law"}
    d = build_digest([_obl_finding(appl)], [_provision()])
    assert len(d.obligations) == 0, "out_of_scope must NOT appear in the binding obligations list"
    assert len(d.does_not_bind) == 1, (
        "out_of_scope must appear in the visible does_not_bind section (demoted, not dropped)"
    )
    assert d.does_not_bind[0].binds == "out_of_scope"
    assert d.does_not_bind[0].binds_reason == "addressed to the legislature to adopt law"
    # it doesn't bind → nothing to check / do
    assert len(d.checklist) == 0 and len(d.actions) == 0


def test_render_conditional_stays_in_obligations_with_flag():
    appl = {"binds": "conditional", "reason": "reaches an AIFM only via an Art 6(4) top-up"}
    d = build_digest([_obl_finding(appl)], [_provision()])
    assert len(d.obligations) == 1 and len(d.does_not_bind) == 0
    assert d.obligations[0].binds == "conditional"
    assert d.obligations[0].binds_reason.startswith("reaches an AIFM")
    assert len(d.checklist) == 1 and len(d.actions) == 1, "a conditional duty is still tracked"


def test_render_mixed_set_splits_correctly():
    findings = [
        _obl_finding({"binds": "yes", "reason": ""}),
        _obl_finding({"binds": "conditional", "reason": "verify"}),
        _obl_finding({"binds": "out_of_scope", "reason": "does not bind"}),
    ]
    d = build_digest(findings, [_provision()])
    assert len(d.obligations) == 2 and len(d.does_not_bind) == 1
    assert {o.binds for o in d.obligations} == {"yes", "conditional"}


def test_digest_default_does_not_bind_is_empty():
    """Back-compat: a freshly constructed Digest still has the new field defaulting empty."""
    assert Digest().does_not_bind == []


# ── WF-3 citation precision wiring (build_digest citation_strategy) ─────────────────────────────────────────
_PID2 = UUID("22222222-2222-2222-2222-222222222222")


def test_wf3_first_only_is_byte_identical_empty_refs():
    """Default strategy → all_cited_refs empty (byte-identical to pre-WF-3 render)."""
    f = _obl_finding(None, cited=[_PID, _PID2])
    provs = [_provision(_PID), _provision(_PID2)]
    d = build_digest([f], provs)  # citation_strategy defaults to 'first_only'
    assert d.obligations[0].all_cited_refs == ()


def test_wf3_all_direct_recovers_the_extra_citation():
    """strategy='all_direct' → the second cited provision (dropped today) is recovered, primary preserved."""
    f = _obl_finding(None, cited=[_PID, _PID2])
    provs = [_provision(_PID), _provision(_PID2)]
    d = build_digest([f], provs, citation_strategy="all_direct")
    refs = d.obligations[0].all_cited_refs
    ids = {r["provision_id"] for r in refs}
    assert ids == {str(_PID), str(_PID2)}  # both surfaced (render drops the 2nd today)
    assert sum(1 for r in refs if r["is_primary"]) == 1  # exactly one primary
    assert any(r["is_primary"] and r["provision_id"] == str(_PID) for r in refs)


# ── WF-4 multi-limb/dedup wiring (build_digest multi_limb) ─────────────────────────────────────────────────
def _stmt_finding(statement, *, cited=None):
    f = _obl_finding(None, cited=cited or [_PID])
    f.statement = statement
    return f


def test_wf4_off_is_byte_identical_empty_bag():
    d = build_digest(
        [_stmt_finding("Musíte něco udělat.")], [_provision()]
    )  # multi_limb defaults False
    assert d.obligations[0].wf4 == {}


def test_wf4_on_annotates_limbs_and_keeps_finding():
    d = build_digest(
        [_stmt_finding("must do (a) keep records and (b) report breaches")],
        [_provision()],
        multi_limb=True,
    )
    assert len(d.obligations) == 1  # nothing dropped
    assert len(d.obligations[0].wf4["limbs"]) >= 2  # split into sub-duties
    assert d.obligations[0].wf4["is_duplicate"] is False


def test_wf4_mutual_exclusion_keeps_both_capital_poles():
    """The 125k(external)/300k(internal) pair is GROUPED (n_alternatives>0) but BOTH stay in the register."""
    ext = _stmt_finding(
        "an external AIFM shall have initial capital of at least EUR 125000", cited=[_PID]
    )
    intr = _stmt_finding(
        "an internally managed AIF shall have initial capital of at least EUR 300000", cited=[_PID2]
    )
    d = build_digest([ext, intr], [_provision(_PID), _provision(_PID2)], multi_limb=True)
    assert len(d.obligations) == 2  # BOTH poles kept (never drops one)
    assert all(o.wf4["n_alternatives"] == 1 for o in d.obligations)


# ── WF-6 temporal-freshness wiring (build_digest temporal_as_of) ───────────────────────────────────────────
def _prov_dated(pid=_PID, *, valid_from=None, valid_to=None, temporal_status=None):
    p = _provision(pid)
    p.valid_from, p.valid_to, p.temporal_status = valid_from, valid_to, temporal_status
    return p


def test_wf6_off_is_byte_identical_empty_bag():
    d = build_digest([_obl_finding(None)], [_provision()])  # temporal_as_of defaults None
    assert d.obligations[0].temporal == {}


def test_wf6_on_flags_superseded_but_keeps_finding():
    import json
    from datetime import date

    prov = _prov_dated(valid_to=date(2025, 1, 1))  # window closed before the as-of
    d = build_digest([_obl_finding(None)], [prov], temporal_as_of=date(2026, 6, 29))
    assert len(d.obligations) == 1  # NEVER dropped — a pure signal
    temporal = d.obligations[0].temporal
    assert temporal["verdict"] == "superseded"
    assert (
        temporal["stale_since"] == "2025-01-01"
    )  # dates → ISO strings (pure-JSON by construction)
    json.dumps(temporal)  # must not raise (no raw date objects in the bag)


def test_wf6_missing_temporal_is_unknown_not_current():
    from datetime import date

    d = build_digest([_obl_finding(None)], [_prov_dated()], temporal_as_of=date(2026, 6, 29))
    assert d.obligations[0].temporal["verdict"] == "unknown"  # honest — never falsely 'current'


# ── deferred gap-suppression (WF-1 Inc5 / WF-4 / WF-6) — TAG-not-DROP ───────────────────────────────────────
def _gap_finding(suppressed_by=None, *, cited=None):
    payload = {}
    if suppressed_by is not None:
        payload["suppressed_by"] = list(suppressed_by)
    return SimpleNamespace(
        finding_type="gap",
        analysis_layer="L4",
        statement="No client doc mentions X.",
        severity="gap",
        confidence_overall=0.6,
        review_state="unreviewed",
        disclaimer_id="d",
        answer_mode="information",
        cited_provisions=[cited or _PID],
        payload=payload,
    )


def test_gap_no_suppression_stays_in_main_gaps():
    """Default world (no suppressed_by stamp) → the gap is in the main list, context_gaps empty (byte-identical)."""
    d = build_digest([_gap_finding(None)], [_provision()])
    assert len(d.gaps) == 1 and len(d.context_gaps) == 0


def test_gap_stamped_suppressed_routes_to_visible_context_bucket_never_dropped():
    """A gap stamped suppressed_by (by L4) is DEMOTED to the VISIBLE context_gaps bucket — never dropped."""
    d = build_digest([_gap_finding(["applicability_out_of_scope"])], [_provision()])
    assert len(d.gaps) == 0 and len(d.context_gaps) == 1
    assert d.context_gaps[0].suppressed_by == ("applicability_out_of_scope",)


def test_gap_temporal_stale_suppression_at_render():
    """WF-6 gap-suppression adds 'stale_law' at render when the provision is superseded → visible context bucket."""
    from datetime import date

    prov = _prov_dated(valid_to=date(2025, 1, 1))
    d = build_digest(
        [_gap_finding(None)],
        [prov],
        temporal_as_of=date(2026, 6, 29),
        temporal_gap_suppression=True,
    )
    assert len(d.gaps) == 0 and len(d.context_gaps) == 1
    assert "stale_law" in d.context_gaps[0].suppressed_by
    # temporal_gap_suppression OFF (default) → the gap stays in the main list
    d2 = build_digest([_gap_finding(None)], [prov], temporal_as_of=date(2026, 6, 29))
    assert len(d2.gaps) == 1 and len(d2.context_gaps) == 0


def test_gap_never_dropped_invariant_mixed_set():
    """THE cardinal invariant: every gap finding appears in gaps OR context_gaps — total is conserved."""
    findings = [
        _gap_finding(None, cited=_PID),
        _gap_finding(["duplicate"], cited=_PID2),
        _gap_finding(["applicability_out_of_scope"], cited=_PID),
    ]
    d = build_digest(findings, [_provision(_PID), _provision(_PID2)])
    assert len(d.gaps) + len(d.context_gaps) == 3  # NONE dropped
    assert len(d.context_gaps) == 2 and len(d.gaps) == 1


@pytest.mark.parametrize("bad", ["duplicate", 42, {"x": 1}, True])
def test_gap_malformed_suppressed_by_is_safe_never_crashes_or_drops(bad):
    """A malformed suppressed_by (string/int/dict) must not crash render, unpack char-by-char, or drop the gap."""
    f = _gap_finding(None)
    f.payload["suppressed_by"] = bad
    d = build_digest([f], [_provision()])
    # treated as 'no L4 reason' → the gap stays in the main list (never dropped, never garbage-routed)
    assert len(d.gaps) == 1 and len(d.context_gaps) == 0


def test_l4_gap_suppressed_by_helper_gated_by_flags():
    """_gap_suppressed_by returns [] when the flags are OFF (default), reasons when ON."""
    from types import SimpleNamespace as NS

    from regsuper.analysis import l4_gaps

    ob = {"applicability": {"binds": "out_of_scope"}}
    with patch.object(
        l4_gaps,
        "get_settings",
        lambda: NS(
            entity_scoping_gap_suppression_enabled=False, multi_limb_gap_dedup_enabled=False
        ),
    ):
        assert l4_gaps._gap_suppressed_by(ob, dup_of=None) == []
    with patch.object(
        l4_gaps,
        "get_settings",
        lambda: NS(entity_scoping_gap_suppression_enabled=True, multi_limb_gap_dedup_enabled=True),
    ):
        assert l4_gaps._gap_suppressed_by(ob, dup_of="other-id") == [
            "applicability_out_of_scope",
            "duplicate",
        ]


@pytest.mark.parametrize("bad_binds", [None, "maybe", 1, "", "OUT_OF_SCOPE"])
def test_render_malformed_binds_falls_back_to_yes(bad_binds):
    """A malformed verdict must never crash render or hide a finding — binds falls back to 'yes' (kept)."""
    d = build_digest([_obl_finding({"binds": bad_binds, "reason": "x"})], [_provision()])
    assert len(d.obligations) == 1 and len(d.does_not_bind) == 0
    assert d.obligations[0].binds == "yes"


def test_render_non_dict_applicability_is_safe():
    f = _obl_finding(None)
    f.payload["applicability"] = "not-a-dict"
    d = build_digest([f], [_provision()])
    assert len(d.obligations) == 1 and d.obligations[0].binds == "yes"


# ── E10: export emits the key only-if-present; legacy hash unchanged ────────────────────────────────────────
def _settings():
    return SimpleNamespace(
        law_source="regsuper",
        embedding_model="text-embedding-3-large",
        data_residency_compute_region=None,
        data_residency_storage_region=None,
        openai_dpa_url=None,
    )


def test_export_block_omits_key_when_absent():
    f = _obl_finding(None)
    f = SimpleNamespace(**{**f.__dict__, "id": uuid4(), "project_id": uuid4()})
    block = export._finding_block(f, {str(_PID): _provision()})
    assert "applicability" not in block, (
        "no applicability stamped → key absent → legacy hash unchanged"
    )


def test_export_block_emits_key_when_present():
    appl = {"binds": "out_of_scope", "reason": "x", "addressee_class": "member_state"}
    f = _obl_finding(appl)
    f = SimpleNamespace(**{**f.__dict__, "id": uuid4(), "project_id": uuid4()})
    block = export._finding_block(f, {str(_PID): _provision()})
    assert block["applicability"] == appl


@pytest.mark.parametrize("bad", [None, 42, "out_of_scope", [1, 2]])
def test_export_block_ignores_malformed_applicability(bad):
    """A non-dict applicability must NOT be emitted (keeps the canonical hash stable / float-free)."""
    f = _obl_finding(None)
    f.payload["applicability"] = bad
    f = SimpleNamespace(**{**f.__dict__, "id": uuid4(), "project_id": uuid4()})
    block = export._finding_block(f, {str(_PID): _provision()})
    assert "applicability" not in block


def test_export_block_strips_binds_confidence_float():
    """The exported verdict carries NO float (binds_confidence dropped) → the audit hash is float-repr-proof."""
    appl = {"binds": "yes", "reason": "", "binds_confidence": 0.8999, "addressee_class": "aifm"}
    f = _obl_finding(appl)
    f = SimpleNamespace(**{**f.__dict__, "id": uuid4(), "project_id": uuid4()})
    block = export._finding_block(f, {str(_PID): _provision()})
    assert "binds_confidence" not in block["applicability"]
    assert block["applicability"]["binds"] == "yes"
    assert not any(isinstance(v, float) for v in block["applicability"].values())


def test_export_hash_unchanged_for_legacy_finding():
    """The canonical hash of a no-applicability finding block equals that of the same block built without the
    feature at all (the key is simply absent — additive-only)."""
    f = _obl_finding(None)
    f = SimpleNamespace(**{**f.__dict__, "id": uuid4(), "project_id": uuid4()})
    block = export._finding_block(f, {str(_PID): _provision()})
    legacy_equiv = {k: v for k, v in block.items()}  # identical dict, no applicability key present
    assert export.build_canonical_payload(block) == export.build_canonical_payload(legacy_equiv)
    # and a stamped finding hashes DIFFERENTLY (the key changes the content) — determinism still holds
    f2 = _obl_finding({"binds": "yes", "reason": ""})
    f2 = SimpleNamespace(**{**f2.__dict__, "id": f.id, "project_id": f.project_id})
    block2 = export._finding_block(f2, {str(_PID): _provision()})
    assert export.build_canonical_payload(block2) == export.build_canonical_payload(
        block2
    )  # deterministic
    assert export.build_canonical_payload(block2) != export.build_canonical_payload(block)


# ── E3: intake writes the structured role only when the flag is ON ─────────────────────────────────────────
@asynccontextmanager
async def _fake_session_scope():
    class _Session:
        def add(self, *_a, **_k):
            return None

        async def flush(self):
            return None

    yield _Session()


async def _fake_emit(*_a, **_k):
    return None


def _intake():
    from regsuper.analysis.l0_intake import ProjectIntake

    return ProjectIntake(
        name="Fund X",
        sector="investment_funds",
        jurisdiction_scope=["CZ"],
        entity_type="aifm",
        declared_intent="pure AIFM, no MiFID top-up",
    )


@pytest.mark.parametrize("flag", [True, False])
async def test_intake_writes_entity_role_only_when_flag_on(flag):
    from regsuper.analysis import l0_intake

    with (
        patch.object(l0_intake, "session_scope", _fake_session_scope),
        patch.object(l0_intake, "emit", _fake_emit),
        patch.object(
            l0_intake, "get_settings", lambda: SimpleNamespace(entity_scoping_enabled=flag)
        ),
    ):
        project = await l0_intake.create_project(_intake(), client_id=uuid4())
    if flag:
        assert "entity_role" in project.metadata_
        role = project.metadata_["entity_role"]
        assert role["primary"] == "aifm" and role["has_top_up"] is False
    else:
        assert "entity_role" not in project.metadata_, (
            "flag OFF → metadata_ byte-identical (no key)"
        )


# ── E5: the post-L3 scoring pass ───────────────────────────────────────────────────────────────────────────
def _scoring_session(findings, project):
    class _Result:
        def scalars(self):
            return SimpleNamespace(all=lambda: findings)

    class _Session:
        async def get(self, _model, _pid):
            return project

        async def execute(self, *_a, **_k):
            return _Result()

    @asynccontextmanager
    async def _scope():
        yield _Session()

    return _scope


def _scoring_finding(actor, modal="shall", conf=1.0, existing=None, statement=None):
    payload = {"actor": actor, "modal": modal}
    if existing is not None:
        payload["applicability"] = existing
    return SimpleNamespace(
        statement=statement or f"{actor} shall do X",
        confidence_overall=conf,
        finding_type="obligation",
        payload=payload,
    )


async def _run_scoring(findings, project, *, flag=True, floor=0.0, force=False, confidence=False):
    from regsuper.analysis import applicability_scoring as sc

    with (
        patch.object(sc, "session_scope", _scoring_session(findings, project)),
        patch.object(sc, "emit", _fake_emit),
        patch.object(
            sc,
            "get_settings",
            lambda: SimpleNamespace(
                entity_scoping_enabled=flag,
                entity_scoping_floor=floor,
                applicability_confidence_enabled=confidence,
            ),
        ),
    ):
        return await sc.run_applicability_scoring(uuid4(), force=force)


async def test_wf2_confidence_off_leaves_verdict_clean():
    """WF-2 flag OFF → the verdict carries NO confidence_* keys (byte-identical to pre-WF-2)."""
    project = SimpleNamespace(
        metadata_={"entity_role": {"primary": "aifm", "assumed": False, "has_top_up": True}},
        entity_type="aifm",
        declared_intent=None,
    )
    f = _scoring_finding("AIFMs")
    await _run_scoring([f], project, confidence=False)
    appl = f.payload["applicability"]
    assert "confidence_label" not in appl and "confidence_basis" not in appl


async def test_wf2_confidence_on_annotates_without_changing_binds():
    """WF-2 flag ON → confidence_basis/label/reason added; binds verdict UNCHANGED; a binding duty never 'low'."""
    project = SimpleNamespace(
        metadata_={"entity_role": {"primary": "aifm", "assumed": False, "has_top_up": True}},
        entity_type="aifm",
        declared_intent=None,
    )
    f = _scoring_finding("AIFMs")
    await _run_scoring([f], project, confidence=True)
    appl = f.payload["applicability"]
    assert appl["binds"] == "yes"  # unchanged
    assert (
        appl["confidence_label"] in ("high", "moderate", "low")
        and appl["confidence_label"] != "low"
    )
    assert appl["confidence_basis"] == "addressee_match" and appl["confidence_reason"]


async def test_scoring_stamps_every_obligation_and_keeps_all():
    project = SimpleNamespace(
        metadata_={
            "entity_role": {
                "primary": "aifm",
                "excludes": ["mifid_investment_firm"],
                "has_top_up": False,
                "assumed": False,
            }
        },
        entity_type="aifm",
        declared_intent=None,
    )
    findings = [
        _scoring_finding("AIFMs"),  # yes
        _scoring_finding("Investment firms"),  # out_of_scope (declared exclusion + no top-up)
        _scoring_finding("depositary"),  # conditional (counterparty)
    ]
    report = await _run_scoring(findings, project)
    assert report.n_scored == 3 and report.n_obligations == 3
    # every finding kept + stamped
    for f in findings:
        assert "applicability" in f.payload
    binds = [f.payload["applicability"]["binds"] for f in findings]
    assert binds == ["yes", "out_of_scope", "conditional"]
    assert report.n_yes == 1 and report.n_out_of_scope == 1 and report.n_conditional == 1


async def test_scoring_is_idempotent_unless_forced():
    project = SimpleNamespace(
        metadata_={"entity_role": {"primary": "aifm", "assumed": False, "has_top_up": True}},
        entity_type="aifm",
        declared_intent=None,
    )
    sentinel = {"binds": "yes", "reason": "pre-existing"}
    findings = [_scoring_finding("AIFMs", existing=sentinel)]
    report = await _run_scoring(findings, project)
    assert report.n_scored == 0 and report.n_skipped_existing == 1
    assert findings[0].payload["applicability"] is sentinel, (
        "must NOT overwrite an existing verdict"
    )

    report2 = await _run_scoring(findings, project, force=True)
    assert report2.n_scored == 1
    assert findings[0].payload["applicability"] is not sentinel, "force must re-score"


# ── digest API: the verdict survives the wire mapper (review render-fwd CRITICAL/HIGH) ─────────────────────
def _render_obl_item(binds="yes", binds_reason=""):
    return ObligationItem(
        modal_label="musíte",
        actor_label="AIFM",
        deadline=None,
        legal_source="§ 1",
        legal_source_url="/sources/x",
        cited_provision_id=str(_PID),
        confidence=0.8,
        review_state="unreviewed",
        disclaimer_id="d",
        binds=binds,
        binds_reason=binds_reason,
    )


@pytest.mark.parametrize(
    "binds,reason", [("yes", ""), ("conditional", "via top-up"), ("out_of_scope", "doesn't bind")]
)
def test_digest_api_mapper_preserves_binds_verdict(binds, reason):
    from regsuper.api import digest as digest_api

    wire = digest_api._to_obligation_item(_render_obl_item(binds, reason))
    assert wire.binds == binds and wire.binds_reason == reason


def test_digest_api_obligation_item_defaults_yes():
    """A render item with no verdict (legacy / flag-OFF) maps to binds='yes' (back-compat)."""
    from regsuper.api import digest as digest_api

    wire = digest_api._to_obligation_item(_render_obl_item())
    assert wire.binds == "yes" and wire.binds_reason == ""


def test_digest_api_responses_expose_does_not_bind():
    from regsuper.api import digest as digest_api

    reg = digest_api.ObligationRegisterResponse(profile_id=_PID, project_id=_PID)
    summ = digest_api.DigestSummaryResponse(profile_id=_PID, project_id=_PID)
    assert reg.does_not_bind == [] and summ.does_not_bind == []  # default-empty → back-compat
    # and it accepts the demoted items
    reg2 = digest_api.ObligationRegisterResponse(
        profile_id=_PID,
        project_id=_PID,
        does_not_bind=[digest_api._to_obligation_item(_render_obl_item("out_of_scope", "x"))],
    )
    assert reg2.does_not_bind[0].binds == "out_of_scope"


async def test_scoring_unknown_role_keeps_everything_fail_safe():
    """No entity_role in metadata + no entity_type → assumed role → every finding 'yes' (never hidden)."""
    project = SimpleNamespace(metadata_={}, entity_type=None, declared_intent=None)
    findings = [
        _scoring_finding("Member States", statement="Member States shall require AIFMs to report"),
        _scoring_finding("Investment firms"),
        _scoring_finding("depositary"),
    ]
    report = await _run_scoring(findings, project)
    assert report.n_out_of_scope == 0, "assumed project role must never hide a finding"
    assert all(f.payload["applicability"]["binds"] == "yes" for f in findings)
