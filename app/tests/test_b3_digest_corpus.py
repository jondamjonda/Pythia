"""B3 Option A — the SME digest/render layer is corpus-aware (a current-law obligation renders, not dropped).

The recon flagged the digest as a near-cutover-blocker: build_digest pairs each finding with its cited
provision via a by_id lookup, and a finding whose provision is absent is DROPPED whole. Under nexus the cited
id is a NEXUS id resolved to a NexusProvisionView (string id), while build_digest historically keyed by_id by
the raw UUID — so a current-law obligation would silently vanish from the SME register (an empty register is
worse than a degraded one). This pins: (1) build_digest keys by stringified id, so a nexus view resolves;
(2) a nexus STATUTE is grounded (never dropped by the case-law guard); (3) the app path is unchanged;
(4) _load_digest routes through the shared corpus-aware resolver.

All DB-free. (asyncio_mode=auto → async tests run directly.)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from regsuper.analysis.provision_view import NexusProvisionView
from regsuper.analysis.render import build_digest
from regsuper.api import digest as digest_api


def _nexus_view(pid):
    return NexusProvisionView.from_engine_dict(
        str(pid),
        {
            "id": str(pid),
            "text": "current law text",
            "label": "§ 5",
            "jurisdiction": "CZ",
            "legal_hierarchy": "act",
            "external_id": "240/2013",
            "temporal_status": "IN_FORCE",
        },
    )


def _obligation_finding(pid, *, nexus=True):
    return SimpleNamespace(
        finding_type="obligation",
        analysis_layer="L3",
        statement="Musíte oznámit zahájení činnosti.",
        severity="obligation",
        confidence_overall=0.82,
        review_state="unreviewed",
        disclaimer_id=None,
        cited_provisions=[pid],
        answer_mode="information",
        payload={"law_source": "nexus"} if nexus else {},
    )


def test_nexus_obligation_renders_through_build_digest():
    """A nexus-cited obligation paired with a NexusProvisionView (string id) is rendered into the register —
    NOT dropped. This is the corpus-agnostic str-keying that keeps the SME register from silently emptying."""
    pid = uuid4()
    digest = build_digest([_obligation_finding(pid)], [_nexus_view(pid)])
    assert len(digest.obligations) == 1, (
        "the current-law obligation must appear in the SME register"
    )


def test_nexus_statute_is_grounded_not_dropped_by_caselaw_guard():
    """A nexus STATUTE (source=None → not 'court', legal_hierarchy='act') is never dropped by the case-law
    grounding guard — _is_case_law is False, so it is always grounded."""
    pid = uuid4()
    digest = build_digest([_obligation_finding(pid)], [_nexus_view(pid)])
    assert len(digest.obligations) == 1
    assert len(digest.checklist) == 1  # the obligation also yields a checklist row


def test_app_provision_still_renders_under_stringified_keying():
    """The app path (UUID-id Provision stand-in) still resolves under the stringified by_id keying —
    behaviour-preserving. str(UUID) == str(UUID)."""
    pid = UUID("11111111-1111-1111-1111-111111111111")
    prov = SimpleNamespace(
        id=pid,
        deontic_modal="musí",
        deontic_actor="provozovatel",
        deontic_action="oznámit",
        deontic_condition="do 15 dnů",
        label="§ 1",
        eid="sec_1",
        hierarchy_path="act.sec_1",
        jurisdiction="CZ",
        legal_hierarchy="act",
    )
    digest = build_digest([_obligation_finding(pid, nexus=False)], [prov])
    assert len(digest.obligations) == 1


async def test_load_digest_routes_through_corpus_aware_resolver():
    """_load_digest resolves provisions via resolve_cited_provisions (corpus-aware), not a raw app-table
    select — so the digest inherits the app/nexus routing for free."""
    pid = uuid4()
    findings = [_obligation_finding(pid)]
    view = _nexus_view(pid)

    class _Scalars:
        def all(self):
            return findings

    class _Exec:
        def scalars(self):
            return _Scalars()

    class _Session:
        async def execute(self, *_a, **_k):
            return _Exec()

    async def fake_resolve(session, fnds):
        assert fnds == findings  # the resolver is handed the findings (so it can read the marker)
        return {str(pid): view}

    captured = {}

    def fake_build(findings_arg, provisions_arg, *, answer_mode, **_kw):
        captured["provisions"] = provisions_arg
        return SimpleNamespace(obligations=[], checklist=[], gaps=[], actions=[])

    with (
        patch.object(digest_api, "resolve_cited_provisions", fake_resolve),
        patch.object(digest_api.render, "build_digest", fake_build),
    ):
        await digest_api._load_digest(_Session(), SimpleNamespace(id=uuid4()))

    assert captured["provisions"] == [
        view
    ]  # the corpus-resolved provision flowed into build_digest


def test_nexus_ungrounded_caselaw_citation_is_dropped():
    """The grounding chokepoint still DROPS an ungrounded nexus CASE-LAW citation (source='court' provenance,
    no resolvable source_external_id) — a fabricated apex chip can never ride into the SME register, even from
    the engine corpus. Inert today (0 case law), but the safety property must hold the instant case law lands."""
    pid = uuid4()
    caselaw_view = NexusProvisionView.from_engine_dict(
        str(pid),
        {
            "id": str(pid),
            "text": "a ruling",
            "label": "8 Afs 21/2022",
            "jurisdiction": "CZ",
            "legal_hierarchy": "CASE_LAW",
            "source": "court",
        },  # provenance court → case law; NO external_id
    )
    assert caselaw_view.source == "court" and caselaw_view.source_external_id is None
    digest = build_digest([_obligation_finding(pid)], [caselaw_view])
    assert digest.obligations == [], "an ungrounded nexus case-law citation must be dropped whole"


async def test_load_digest_renders_nexus_obligation_through_the_wire_mapper():
    """End-to-end: real _load_digest → real resolver (engine mocked) → real build_digest → the wire mapper.
    Proves a current-law obligation renders AND _to_obligation_item's UUID() coercion succeeds (the nexus id
    is a real UUID per the cited_provisions uuid[] column), so the digest endpoint cannot 500 on it."""
    from regsuper.api import export

    pid = uuid4()
    finding = _obligation_finding(pid)

    class _Scalars:
        def all(self):
            return [finding]

    class _Exec:
        def scalars(self):
            return _Scalars()

    class _Session:
        async def execute(self, *_a, **_k):
            return _Exec()

    async def fake_provisions(ids, **_k):
        assert ids == [str(pid)]
        return [
            {
                "id": str(pid),
                "text": "current law",
                "label": "§ 5",
                "jurisdiction": "CZ",
                "legal_hierarchy": "act",
                "external_id": "240/2013",
                "temporal_status": "IN_FORCE",
            }
        ]

    with patch.object(export.pythia_client, "provisions", fake_provisions):
        digest = await digest_api._load_digest(_Session(), SimpleNamespace(id=uuid4()))

    assert len(digest.obligations) == 1
    wire = digest_api._to_obligation_item(digest.obligations[0])  # UUID coercion must NOT raise
    assert wire.cited_provision_id == pid
