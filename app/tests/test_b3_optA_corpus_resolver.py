"""B3 Option A — corpus-aware cited_provisions resolution for the citation-proof surface (export + evidence).

Under law_source='nexus' a finding cites a NEXUS provision id; its data lives in the live engine, not the app
`provision` table. The persist sites stamp payload['law_source']='nexus' (ONLY under nexus → regsuper findings
byte-unchanged), and export.resolve_cited_provisions partitions a project's findings by that marker: app ids
resolve against the provision table (the ORM object, byte-identical to before), nexus ids against the engine
(a NexusProvisionView with the same attribute surface). A nexus id the engine does not return is simply absent →
it surfaces as an honest dangling citation downstream, never silently substituted. Ships dark: nothing stamps
the marker unless law_source=='nexus', and the whole nexus cascade is still gated off by
require_cascade_law_source.

DB-free: session_scope / pythia_client / get_settings are stubbed. (asyncio_mode=auto → async tests run.)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from regsuper.analysis.provision_view import NexusProvisionView, _as_date
from regsuper.api import export


# --------------------------------------------------------------------------- NexusProvisionView adapter
def test_as_date_is_tolerant():
    assert _as_date(None) is None
    assert _as_date("") is None
    assert _as_date("2013-07-19") == date(2013, 7, 19)
    assert _as_date("2013-07-19T00:00:00") == date(2013, 7, 19)  # ISO datetime → date prefix
    assert _as_date(date(2020, 1, 1)) == date(2020, 1, 1)
    assert _as_date("garbage") is None  # degrades to open window, never raises


def test_provision_view_from_engine_dict_shape_and_decisions():
    v = NexusProvisionView.from_engine_dict(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        {
            "id": "DIFFERENT-ECHO",
            "text": "current text",
            "label": "§ 5",
            "jurisdiction": "CZ",
            "legal_hierarchy": "act",
            "temporal_status": "IN_FORCE",
            "valid_from": "2013-07-19",
            "valid_to": "2020-01-01",
            "bindingness": "HARD_LAW_ERGA_OMNES",
        },
    )
    assert v.id == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"  # the REQUESTED id, not the engine echo
    assert v.text == "current text"
    assert v.label == "§ 5"
    assert v.jurisdiction == "CZ"
    assert v.legal_hierarchy == "act"
    assert v.temporal_status == "IN_FORCE"
    assert v.valid_from == date(2013, 7, 19)  # ISO string parsed to date for the temporal proof
    assert v.valid_to == date(2020, 1, 1)
    assert v.eid is None  # app-only structural field
    assert v.hierarchy_path is None  # app-only LTREE field
    assert v.bindingness_tier is None  # different engine vocab NOT fabricated into the hash


# --------------------------------------------------------------------------- the per-finding corpus marker
def test_finding_is_nexus_reads_payload_marker():
    assert export._finding_is_nexus(SimpleNamespace(payload={"law_source": "nexus"})) is True
    assert export._finding_is_nexus(SimpleNamespace(payload={"law_source": "regsuper"})) is False
    assert export._finding_is_nexus(SimpleNamespace(payload={})) is False
    assert export._finding_is_nexus(SimpleNamespace(payload=None)) is False
    assert export._finding_is_nexus(SimpleNamespace()) is False  # no payload attr at all


# --------------------------------------------------------------------------- corpus-aware resolver
class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeExec:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeSession:
    """Stands in for the AsyncSession _load_provisions queries — returns the given app Provision rows."""

    def __init__(self, app_rows):
        self._app_rows = app_rows

    async def execute(self, *_a, **_k):
        return _FakeExec(self._app_rows)


def _engine_row(pid, **over):
    base = {
        "id": str(pid),
        "text": "engine text",
        "label": "§1",
        "jurisdiction": "CZ",
        "legal_hierarchy": "act",
        "temporal_status": "IN_FORCE",
        "valid_from": "2013-07-19",
        "valid_to": None,
    }
    base.update(over)
    return base


async def test_resolver_regsuper_only_never_calls_engine_and_returns_orm():
    """A project with only app-corpus findings resolves entirely against the provision table — no engine
    call — and returns the ORM object itself (byte-identical to the old _load_provisions path)."""
    app_pid = uuid4()
    prov = SimpleNamespace(id=app_pid)
    f = SimpleNamespace(cited_provisions=[app_pid], payload={})  # no marker → app corpus
    boom = AsyncMock(
        side_effect=AssertionError("engine must not be called for an app-only project")
    )

    with patch.object(export.pythia_client, "provisions", boom):
        out = await export.resolve_cited_provisions(_FakeSession([prov]), [f])

    assert set(out) == {str(app_pid)}
    assert out[str(app_pid)] is prov  # the ORM Provision itself, unchanged
    boom.assert_not_awaited()


async def test_resolver_nexus_finding_routes_to_engine_view():
    nexus_pid = uuid4()
    f = SimpleNamespace(cited_provisions=[nexus_pid], payload={"law_source": "nexus"})

    async def fake_provisions(ids, **_k):
        assert ids == [str(nexus_pid)]  # only the nexus ids go to the engine
        return [_engine_row(nexus_pid, text="current law")]

    with patch.object(export.pythia_client, "provisions", fake_provisions):
        out = await export.resolve_cited_provisions(_FakeSession([]), [f])

    assert set(out) == {str(nexus_pid)}
    v = out[str(nexus_pid)]
    assert isinstance(v, NexusProvisionView)
    assert v.id == str(nexus_pid) and v.text == "current law"


async def test_resolver_mixed_partitions_by_marker():
    app_pid, nexus_pid = uuid4(), uuid4()
    prov = SimpleNamespace(id=app_pid)
    fa = SimpleNamespace(cited_provisions=[app_pid], payload={})
    fn = SimpleNamespace(cited_provisions=[nexus_pid], payload={"law_source": "nexus"})

    async def fake_provisions(ids, **_k):
        assert ids == [str(nexus_pid)]  # the app id is NOT sent to the engine
        return [_engine_row(nexus_pid)]

    with patch.object(export.pythia_client, "provisions", fake_provisions):
        out = await export.resolve_cited_provisions(_FakeSession([prov]), [fa, fn])

    assert set(out) == {str(app_pid), str(nexus_pid)}
    assert out[str(app_pid)] is prov  # app → ORM
    assert isinstance(out[str(nexus_pid)], NexusProvisionView)  # nexus → engine view


async def test_resolver_nexus_engine_miss_is_absent_not_guessed():
    """A nexus id the engine does not return is absent from the map → an honest dangling citation downstream
    (the evidence proof's unresolved set), never silently substituted with a wrong provision."""
    nexus_pid = uuid4()
    fn = SimpleNamespace(cited_provisions=[nexus_pid], payload={"law_source": "nexus"})

    async def empty(_ids, **_k):
        return []

    with patch.object(export.pythia_client, "provisions", empty):
        out = await export.resolve_cited_provisions(_FakeSession([]), [fn])

    assert out == {}


async def test_resolver_engine_error_degrades_to_unresolved_not_500():
    """An engine TRANSPORT error (timeout/500) is treated as a miss — the nexus ids stay unresolved (honest
    dangling) rather than propagating a 500 out of the evidence-pack route (it has no fail-soft of its own).
    App ids resolved before the engine call are unaffected."""
    app_pid, nexus_pid = uuid4(), uuid4()
    prov = SimpleNamespace(id=app_pid)
    fa = SimpleNamespace(cited_provisions=[app_pid], payload={})
    fn = SimpleNamespace(cited_provisions=[nexus_pid], payload={"law_source": "nexus"})

    async def boom(_ids, **_k):
        raise RuntimeError("engine 500 / timeout")

    with patch.object(export.pythia_client, "provisions", boom):
        out = await export.resolve_cited_provisions(_FakeSession([prov]), [fa, fn])

    assert set(out) == {str(app_pid)}  # app id still resolved; nexus id absent (degraded), no raise
    assert str(nexus_pid) not in out


async def test_nexus_finding_resolves_green_through_the_temporal_proof():
    """The core Option A claim, end-to-end through the proof: a grounded nexus finding's citation resolves via
    the engine, so the evidence pack's all_cited_resolved / temporal_proof_ok flip to LEGITIMATELY True (not
    vacuously) — the exact thing that reads as 'unproven/dangling' without this change."""
    from datetime import date

    from regsuper.api import evidence

    nexus_pid = uuid4()
    fn = SimpleNamespace(cited_provisions=[nexus_pid], payload={"law_source": "nexus"})

    async def fake_provisions(_ids, **_k):
        return [
            _engine_row(nexus_pid, valid_from="2013-07-19", valid_to=None)
        ]  # in force since 2013

    with patch.object(export.pythia_client, "provisions", fake_provisions):
        resolved = await export.resolve_cited_provisions(_FakeSession([]), [fn])

    as_of = date(2024, 1, 1)  # a historical point-in-time; the 2013→open window brackets it
    proof = evidence._temporal_proof(as_of, list(resolved.values()), {str(nexus_pid)})

    assert proof["all_cited_resolved"] is True  # the nexus citation RESOLVED (via the engine)
    assert proof["unresolved_provision_ids"] == []
    assert proof["n_resolved"] == 1  # not vacuous
    assert proof["all_brackets"] is True  # in force at the as_of
    # temporal_proof_ok (the route's headline) = all_brackets AND all_cited_resolved AND n_resolved >= 1
    assert proof["all_brackets"] and proof["all_cited_resolved"] and proof["n_resolved"] >= 1


# --------------------------------------------------------------------------- the persist-side marker stamp
async def test_l3_persist_stamps_corpus_marker_only_under_nexus():
    """The L3 persist site stamps payload['law_source']='nexus' under the cutover, and stamps NOTHING under
    regsuper (so app findings stay byte-identical). L2's two persist sites mirror this exactly."""
    from regsuper.analysis import l3_obligations as l3

    captured: list = []

    @asynccontextmanager
    async def _cap_scope():
        class _S:
            def add(self, obj):
                captured.append(obj)

            async def flush(self):
                return None

        yield _S()

    ob = l3.Obligation(
        actor="bank", modal="shall", action="report", supporting_text="span", confidence=0.9
    )

    async def _run(law_source):
        captured.clear()
        with (
            patch.object(l3, "session_scope", _cap_scope),
            patch.object(l3, "get_settings", lambda: SimpleNamespace(law_source=law_source)),
        ):
            await l3._persist_obligation(
                project_id=uuid4(),
                org_id=uuid4(),
                provision_id=uuid4(),
                source_document_id=uuid4(),
                obligation=ob,
                extraction_latency_ms=1,
                provision_text="span text",
                model="m",
            )
        assert (
            captured[0].org_id is not None
        )  # findings always carry the owning project's tenant (007 NOT NULL)
        return captured[0].payload

    nexus_payload = await _run("nexus")
    assert nexus_payload.get("law_source") == "nexus"

    regsuper_payload = await _run("regsuper")
    assert "law_source" not in regsuper_payload


async def test_l4_gap_persist_stamps_corpus_marker_only_under_nexus():
    """L4 gap findings INHERIT their parent obligation's cited_provisions (nexus ids under the cutover), so
    the gap persist site must stamp the same marker or the resolver would misroute the inherited nexus
    citation to the empty app table. Only under nexus → regsuper gap payloads stay byte-unchanged."""
    from regsuper.analysis import l4_gaps

    captured: list = []

    @asynccontextmanager
    async def _cap_scope():
        class _S:
            def add(self, obj):
                captured.append(obj)

            async def flush(self):
                return None

        yield _S()

    obligation = {
        "id": uuid4(),
        "cited_provisions": [uuid4()],
        "cited_documents": [uuid4()],
        "statement": "must report X",
        "action": "report",
    }
    missing = [
        l4_gaps.Evidence(evidence_type="document", pattern_match="report", search_term="report")
    ]

    async def _run(law_source):
        captured.clear()
        with (
            patch.object(l4_gaps, "session_scope", _cap_scope),
            patch.object(l4_gaps, "get_settings", lambda: SimpleNamespace(law_source=law_source)),
        ):
            await l4_gaps._persist_gap(
                project_id=uuid4(), org_id=uuid4(), obligation=obligation, missing_evidence=missing
            )
        assert (
            captured[0].org_id is not None
        )  # gap findings inherit the owning project's tenant (007 NOT NULL)
        return captured[0].payload

    assert (await _run("nexus")).get("law_source") == "nexus"
    assert "law_source" not in (await _run("regsuper"))
