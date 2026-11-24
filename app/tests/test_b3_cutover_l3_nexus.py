"""B3-04 — L3 obligation extraction resolves provisions from the canonical Nexus corpus (the cutover).

Today the app reasons against a frozen 2024 snapshot (law_source='regsuper'). B3-04 teaches L3's
`_load_provision` to ALSO resolve a provision from the live Nexus engine when law_source='nexus', so a
cut-over domain's obligations are extracted from CURRENT law (the engine's text) while the finding still
cites a real app document (the act's source_document_id resolved via the regsuper↔nexus crosswalk,
`provision_id_map`). It SHIPS DARK: law_source defaults to 'regsuper', so the nexus branch never fires and
the regsuper path is byte-unchanged. A nexus provision whose act is not in the app corpus has NO crosswalk
row → source_document_id None → run_l3 logs the miss and skips persistence (never a guessed citation).

NOT LIVE YET: the nexus branch is UNREACHABLE through run_l3 today — run_l3's first statement
require_cascade_law_source('L3') refuses law_source='nexus' (→ HTTP 409) before any provision is loaded
(test_run_l3_still_refuses_nexus_today pins this). These tests therefore call _load_provision DIRECTLY to
exercise the staged branch; 'tests green' does NOT mean the cutover is flippable — that is the separate,
human-gated B3-06 gate-relax.

All DB-free: session_scope, pythia_client, and get_settings are stubbed so nothing touches Postgres or the
sidecar. (asyncio_mode=auto → async tests run directly.)
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from regsuper import analysis as analysis_pkg
from regsuper.analysis import LawSourceNotSupported, l3_obligations

PID = uuid4()
SDID = uuid4()

# The shape pythia_client.provisions() returns (mirrors the L2 via_nexus contract).
ENGINE_ROW = {
    "label": "§ 5",
    "text": "Current Nexus text of the provision — the live, point-in-time-correct law.",
    "legal_hierarchy": "act",
    "external_id": "240/2013",
    "source_title": "Act on Investment Companies and Funds",
}


def _nexus_settings():
    return SimpleNamespace(law_source="nexus")


def _regsuper_settings():
    return SimpleNamespace(law_source="regsuper")


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeSession:
    def __init__(self, row):
        self._row = row

    async def execute(self, *_a, **_k):
        return _FakeResult(self._row)


def _fake_scope(row):
    """A drop-in for session_scope() — an async context manager yielding a session whose
    execute(...).first() returns `row`. Lets us exercise the SQL-shaped code paths without Postgres."""

    @contextlib.asynccontextmanager
    async def _cm():
        yield _FakeSession(row)

    return _cm()


# --------------------------------------------------------------------------- nexus routing
async def test_nexus_route_uses_engine_text_and_crosswalk_sdid():
    """law_source='nexus' → text comes from the engine (CURRENT law); source_document_id is the
    crosswalk-resolved app document; Nexus-absent fields are None. Citation identity holds."""

    async def fake_provisions(ids, **_k):
        assert ids == [str(PID)]  # queried by the exact requested id, single round-trip
        return [ENGINE_ROW]

    async def fake_sdid(pid):
        assert pid == PID
        return SDID

    with (
        patch.object(l3_obligations, "get_settings", _nexus_settings),
        patch.object(l3_obligations.pythia_client, "provisions", fake_provisions),
        patch.object(l3_obligations, "_nexus_source_document_id", fake_sdid),
    ):
        prov = await l3_obligations._load_provision(PID)

    assert prov is not None
    assert prov["id"] == PID  # line-184 citation-identity assert holds
    assert prov["text"] == ENGINE_ROW["text"]  # CURRENT engine text, not the frozen copy
    assert prov["source_document_id"] == SDID  # crosswalk-resolved app document
    assert prov["external_id"] == "240/2013"
    assert prov["title"] == ENGINE_ROW["source_title"]
    assert prov["hierarchy_path"] is None  # not exposed by Nexus — tolerated by the prompt
    assert prov["publisher"] is None


async def test_nexus_route_returns_none_when_engine_has_no_such_provision():
    async def empty(_ids, **_k):
        return []

    with (
        patch.object(l3_obligations, "get_settings", _nexus_settings),
        patch.object(l3_obligations.pythia_client, "provisions", empty),
    ):
        assert await l3_obligations._load_provision(PID) is None


async def test_nexus_crosswalk_miss_yields_none_sdid_never_a_guess():
    """A nexus provision whose act is not in the app corpus has no crosswalk row → source_document_id
    None. run_l3 then logs 'missing source_document_id' and skips persistence; it is NEVER a guessed id."""

    async def fake_provisions(_ids, **_k):
        return [ENGINE_ROW]

    async def no_sdid(_pid):
        return None

    with (
        patch.object(l3_obligations, "get_settings", _nexus_settings),
        patch.object(l3_obligations.pythia_client, "provisions", fake_provisions),
        patch.object(l3_obligations, "_nexus_source_document_id", no_sdid),
    ):
        prov = await l3_obligations._load_provision(PID)

    assert prov is not None
    assert prov["source_document_id"] is None


async def test_nexus_route_refuses_engine_echo_mismatch():
    """Defense-in-depth: if the engine echoes a DIFFERENT id than requested, the provision is REFUSED
    (→ None) rather than extracting an obligation from one provision's text and citing it to another. This
    is what makes the nexus path genuinely identity-safe (not the tautological assert in extract_obligations,
    which only re-checks the id _load_provision_via_nexus forces)."""

    async def echo_other(_ids, **_k):
        return [{**ENGINE_ROW, "id": str(uuid4())}]

    with (
        patch.object(l3_obligations, "get_settings", _nexus_settings),
        patch.object(l3_obligations.pythia_client, "provisions", echo_other),
    ):
        assert await l3_obligations._load_provision(PID) is None


async def test_nexus_route_accepts_matching_engine_echo():
    """An engine row that echoes the requested id is accepted, with id forced to the requested provision id
    so the citation-identity assert downstream holds. (No-id rows are also accepted — see the first test.)"""

    async def echo_match(_ids, **_k):
        return [{**ENGINE_ROW, "id": str(PID)}]

    async def fake_sdid(_pid):
        return SDID

    with (
        patch.object(l3_obligations, "get_settings", _nexus_settings),
        patch.object(l3_obligations.pythia_client, "provisions", echo_match),
        patch.object(l3_obligations, "_nexus_source_document_id", fake_sdid),
    ):
        prov = await l3_obligations._load_provision(PID)

    assert prov is not None
    assert prov["id"] == PID


async def test_nexus_route_tolerates_empty_engine_text():
    """A definition-only / empty-text engine row is returned as-is (text None); extraction tolerates empty
    text the same way the regsuper path does (empty prompt → empty obligations). No crash, no special-case."""

    async def empty_text(_ids, **_k):
        return [
            {
                "label": None,
                "text": None,
                "legal_hierarchy": None,
                "external_id": None,
                "source_title": None,
            }
        ]

    async def fake_sdid(_pid):
        return SDID

    with (
        patch.object(l3_obligations, "get_settings", _nexus_settings),
        patch.object(l3_obligations.pythia_client, "provisions", empty_text),
        patch.object(l3_obligations, "_nexus_source_document_id", fake_sdid),
    ):
        prov = await l3_obligations._load_provision(PID)

    assert prov is not None
    assert prov["text"] is None
    assert prov["label"] is None
    assert prov["id"] == PID


# --------------------------------------------------------------------------- regsuper path unchanged
async def test_regsuper_is_default_and_never_touches_the_engine():
    """law_source='regsuper' (the default) loads from the app tables and must NOT call the sidecar or the
    nexus branch at all — the cutover code is inert until a domain is deliberately flipped."""
    row = (PID, "§ 1", "Frozen 2024 app text.", "/a/b", "act", SDID, "100/2020", "Some Act", "MoJ")
    boom = AsyncMock(
        side_effect=AssertionError("the engine must not be touched on the regsuper path")
    )

    with (
        patch.object(l3_obligations, "get_settings", _regsuper_settings),
        patch.object(l3_obligations, "session_scope", lambda: _fake_scope(row)),
        patch.object(l3_obligations.pythia_client, "provisions", boom),
        patch.object(l3_obligations, "_load_provision_via_nexus", boom),
    ):
        prov = await l3_obligations._load_provision(PID)

    assert prov["id"] == PID
    assert prov["text"] == "Frozen 2024 app text."
    assert prov["source_document_id"] == SDID
    assert prov["external_id"] == "100/2020"
    boom.assert_not_awaited()


async def test_regsuper_load_returns_none_for_unknown_provision():
    with (
        patch.object(l3_obligations, "get_settings", _regsuper_settings),
        patch.object(l3_obligations, "session_scope", lambda: _fake_scope(None)),
    ):
        assert await l3_obligations._load_provision(PID) is None


# --------------------------------------------------------------------------- the crosswalk SQL helper
async def test_nexus_source_document_id_maps_row_and_handles_miss():
    """provision_id_map.nexus_id → regsuper_id → provision.source_document_id. A matched row yields the
    app document id; no row yields None (honest under-coverage, never a fabricated id)."""
    with patch.object(l3_obligations, "session_scope", lambda: _fake_scope((SDID,))):
        assert await l3_obligations._nexus_source_document_id(PID) == SDID

    with patch.object(l3_obligations, "session_scope", lambda: _fake_scope(None)):
        assert await l3_obligations._nexus_source_document_id(PID) is None


# --------------------------------------------------------------------------- the gate still stands
async def test_run_l3_still_refuses_nexus_today():
    """Pins the gate↔branch contradiction the review surfaced: the cutover is NOT flippable by config alone.
    run_l3's first statement (require_cascade_law_source('L3')) refuses law_source='nexus' before any
    provision is loaded, so the staged nexus branch is unreachable end-to-end. Lifting this gate is the
    separate, human-gated B3-06 step — 'tests green' must never be read as 'cutover live'."""
    with patch.object(analysis_pkg, "get_settings", _nexus_settings):
        with pytest.raises(LawSourceNotSupported):
            await l3_obligations.run_l3(uuid4())
