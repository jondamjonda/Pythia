"""B2a-16 — the stateless L1 core `candidates_for_tags`, fully mocked (no DB, no project load).

`_l1_candidates_regsuper` loads a project then DELEGATES the three-tier degrade strategy here; this
pins that the stateless core (a) triggers the same degrade tiers at the same MIN_CANDIDATES_BEFORE_DEGRADE
threshold, (b) emits the start/complete events safely when project_id is None, and (c) is genuinely
stateless — it NEVER calls get_project (patched to raise if invoked). (asyncio_mode=auto → async tests
run directly.)
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

from regsuper.analysis import l1_candidates as L
from regsuper.analysis.l1_candidates import MIN_CANDIDATES_BEFORE_DEGRADE


def _no_get_project(*_a, **_k):
    # Proves statelessness: candidates_for_tags must NEVER load a project.
    raise AssertionError("candidates_for_tags must not call get_project")


def _candidate(i: int) -> L.L1Candidate:
    return L.L1Candidate(
        provision_id=uuid4(),
        label=f"§ {i}",
        legal_hierarchy="cz-act",
        jurisdiction="CZ",
        source_external_id="240/2013",
        source_title="ZISIF",
        priority_weight=80,
    )


class _Recorder:
    """A deterministic fake for `_run_filtered`: returns a programmed row-count per call so we can
    drive the degrade ladder precisely, and records the filters each tier passed."""

    def __init__(self, counts: list[int]) -> None:
        self._counts = counts
        self.calls: list[L.RetrievalFilters] = []

    async def __call__(self, filters, *, limit, per_document_cap=30):
        self.calls.append(filters)
        n = self._counts[len(self.calls) - 1]
        return [_candidate(i) for i in range(n)]


async def _emits():
    """Patch emit to capture (event_name, kwargs) tuples."""
    captured: list[tuple[str, dict]] = []

    async def fake_emit(name, **kw):
        captured.append((name, kw))

    return captured, fake_emit


async def test_tier1_strict_when_enough_candidates_no_degrade():
    fake = _Recorder([MIN_CANDIDATES_BEFORE_DEGRADE])  # exactly the floor → no degrade
    captured, fake_emit = await _emits()

    with (
        patch.object(L, "get_project", _no_get_project),
        patch.object(L, "_run_filtered", fake),
        patch.object(L, "emit", fake_emit),
    ):
        out = await L.candidates_for_tags(
            jurisdictions=["CZ"], sector_tags=["investment_funds"], top_k=200, project_id=None
        )

    assert len(fake.calls) == 1  # stopped at tier 1
    assert len(out) == MIN_CANDIDATES_BEFORE_DEGRADE
    # tier 1 filters: jurisdiction + sector overlap + active
    assert fake.calls[0].sector_tags == ["investment_funds"]
    assert fake.calls[0].active_only is True
    start = next(kw for name, kw in captured if name == "analysis.l1.start")
    complete = next(kw for name, kw in captured if name == "analysis.l1.complete")
    assert complete["strategy"] == "strict"
    assert complete["n_candidates"] == MIN_CANDIDATES_BEFORE_DEGRADE
    # project_id=None → neutral marker, no crash on str(None)
    assert start["project_id"] == "-"
    assert complete["project_id"] == "-"


async def test_degrade_tier2_drops_sector_at_threshold():
    # tier 1 returns just below the floor → degrade to jurisdiction+active, which then clears the floor.
    fake = _Recorder([MIN_CANDIDATES_BEFORE_DEGRADE - 1, MIN_CANDIDATES_BEFORE_DEGRADE])
    captured, fake_emit = await _emits()

    with (
        patch.object(L, "get_project", _no_get_project),
        patch.object(L, "_run_filtered", fake),
        patch.object(L, "emit", fake_emit),
    ):
        out = await L.candidates_for_tags(
            jurisdictions=["CZ"], sector_tags=["investment_funds"], top_k=200, project_id=None
        )

    assert len(fake.calls) == 2  # degraded once
    assert fake.calls[0].sector_tags == ["investment_funds"]  # tier 1 had sector
    assert fake.calls[1].sector_tags == []  # tier 2 dropped sector
    assert fake.calls[1].active_only is True
    assert len(out) == MIN_CANDIDATES_BEFORE_DEGRADE
    complete = next(kw for name, kw in captured if name == "analysis.l1.complete")
    assert complete["strategy"] == "jurisdiction+active"


async def test_degrade_tier3_drops_active_when_jurisdiction_still_thin():
    # tier 1 and tier 2 both below the floor → degrade all the way to jurisdiction_only (active dropped).
    fake = _Recorder(
        [
            MIN_CANDIDATES_BEFORE_DEGRADE - 1,
            MIN_CANDIDATES_BEFORE_DEGRADE - 1,
            MIN_CANDIDATES_BEFORE_DEGRADE + 5,
        ]
    )
    captured, fake_emit = await _emits()

    with (
        patch.object(L, "get_project", _no_get_project),
        patch.object(L, "_run_filtered", fake),
        patch.object(L, "emit", fake_emit),
    ):
        out = await L.candidates_for_tags(
            jurisdictions=["CZ"], sector_tags=["investment_funds"], top_k=200, project_id=None
        )

    assert len(fake.calls) == 3  # degraded twice
    assert fake.calls[2].sector_tags == []
    assert fake.calls[2].active_only is False  # tier 3 dropped active_only
    assert len(out) == MIN_CANDIDATES_BEFORE_DEGRADE + 5
    complete = next(kw for name, kw in captured if name == "analysis.l1.complete")
    assert complete["strategy"] == "jurisdiction_only"


async def test_as_of_threads_onto_every_tier_filter():
    from datetime import date

    as_of = date(2024, 1, 1)
    fake = _Recorder([0, 0, MIN_CANDIDATES_BEFORE_DEGRADE])  # force all three tiers
    _captured, fake_emit = await _emits()

    with (
        patch.object(L, "get_project", _no_get_project),
        patch.object(L, "_run_filtered", fake),
        patch.object(L, "emit", fake_emit),
    ):
        await L.candidates_for_tags(
            jurisdictions=["CZ"], sector_tags=["x"], top_k=50, as_of=as_of, project_id=None
        )

    # the PIT window holds across every degrade tier
    assert [f.as_of for f in fake.calls] == [as_of, as_of, as_of]


async def test_project_id_marker_when_present():
    fake = _Recorder([MIN_CANDIDATES_BEFORE_DEGRADE])
    captured, fake_emit = await _emits()
    pid = uuid4()

    with (
        patch.object(L, "get_project", _no_get_project),
        patch.object(L, "_run_filtered", fake),
        patch.object(L, "emit", fake_emit),
    ):
        await L.candidates_for_tags(jurisdictions=["CZ"], sector_tags=[], top_k=200, project_id=pid)

    start = next(kw for name, kw in captured if name == "analysis.l1.start")
    assert start["project_id"] == str(pid)  # correlated, not the neutral marker


async def test_activity_tags_emit_only_default_empty():
    # activity_tags is emit-only (never enters a filter). Default (None) → [] in the start event;
    # passed-through values appear verbatim. Either way it must NOT alter any tier's filters.
    fake = _Recorder([MIN_CANDIDATES_BEFORE_DEGRADE])
    captured, fake_emit = await _emits()

    with (
        patch.object(L, "get_project", _no_get_project),
        patch.object(L, "_run_filtered", fake),
        patch.object(L, "emit", fake_emit),
    ):
        await L.candidates_for_tags(
            jurisdictions=["CZ"],
            sector_tags=["s"],
            top_k=200,
            project_id=None,
            activity_tags=["fund_management"],
        )

    start = next(kw for name, kw in captured if name == "analysis.l1.start")
    assert start["activity_tags"] == ["fund_management"]
    assert start["sector_tags"] == ["s"]


async def test_regsuper_delegates_to_stateless_core():
    # _l1_candidates_regsuper loads the project then DELEGATES to candidates_for_tags with the
    # derived tags + the real project_id (byte-parity contract: same observable output + events).
    from types import SimpleNamespace

    seen: dict = {}

    async def fake_core(*, jurisdictions, sector_tags, top_k, as_of, project_id, activity_tags):
        seen.update(
            jurisdictions=jurisdictions,
            sector_tags=sector_tags,
            top_k=top_k,
            as_of=as_of,
            project_id=project_id,
            activity_tags=activity_tags,
        )
        return [_candidate(0)]

    async def fake_get_project(_pid):
        return SimpleNamespace(
            jurisdiction_scope=["EU", "CZ"],
            metadata_={"sector_tags": ["investment_funds"], "activity_tags": ["fund_management"]},
        )

    pid = uuid4()
    with (
        patch.object(L, "get_project", fake_get_project),
        patch.object(L, "candidates_for_tags", fake_core),
    ):
        out = await L._l1_candidates_regsuper(pid, top_k=77)

    assert len(out) == 1
    assert seen["jurisdictions"] == ["EU", "CZ"]
    assert seen["sector_tags"] == ["investment_funds"]
    assert seen["activity_tags"] == ["fund_management"]
    assert seen["project_id"] == pid
    assert seen["top_k"] == 77
