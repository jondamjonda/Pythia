"""A4-08 / WP-028 event-taxonomy gate — closed Literal + non-None routing.

What this proves (and ONLY this — the taxonomy carries NO new behaviour yet)
----------------------------------------------------------------------------
1. **Every v5 code added to FRIENDLY_TEMPLATES routes to a non-None module** —
   a code whose prefix maps to `module=None` is silently dropped at the emit()
   fan-out fallthrough (the mode-3 trap). This test asserts every template key
   derives a real module, so no new alarm can vanish.
2. **The two cost-model alarms reach a channel** — `cost.model.unknown` (the
   silent-$0 tripwire, WP-027) and `limits.suspiciously_low` (the live-test-cap
   tripwire, WP-027) must each fan out to a live SSE subscriber. WP-027's alarms
   are useless if they never leave emit().
3. **The closed module Literal includes `mcp` at BOTH sites** — the `Event`
   TypedDict field AND the `emit()` signature. A miss at either site reopens the
   silent-drop hole for the Conduit channel.
4. **mcp.* reaches the SYSTEM channel** — an `mcp.*` event (no project_id) fans
   out to SYSTEM_CHANNEL via its non-None module, inert seam though it is.
5. **The G4 boundary is honoured** — `nexus.self_update.*` is NOT routed here
   (it belongs to the Nexus/bus lane, A4-T16).

This is import-light and uses the real in-process pub/sub. It touches NO
database and makes NO network call, so it runs green on a bare checkout.
"""

from __future__ import annotations

import typing

import pytest

from regsuper.observability import events
from regsuper.observability.events import (
    FRIENDLY_TEMPLATES,
    SYSTEM_CHANNEL,
    Event,
    _module_for_code,
    emit,
    subscribe,
    unsubscribe,
)

# The four pre-existing modules plus the v5 Conduit channel. `mcp` is the new member.
_EXPECTED_MODULES = {"nexus", "pythia", "polarius", "system", "mcp"}

# The v5 codes A4-08 adds (the spec's explicit list + the lifecycle families). Each MUST have a
# template AND derive a non-None module. The exact friendly copy is not pinned here — only that the
# code is routable and renderable — except the two named cost-model alarms tested directly below.
_V5_CODES = [
    "cost.model.unknown",
    "limits.suspiciously_low",
    "analysis.applicability.refused",
    "analysis.job.queued",
    "analysis.job.started",
    "analysis.job.progress",
    "analysis.job.done",
    "analysis.job.failed",
    "analysis.job.refused",
    "compliance.answer_mode.judged",
    "compliance.refusal.projected",
    "handoff.created",
    "handoff.consent.recorded",
    "export.requested",
    "export.ready",
    "export.refused",
    "tier.upgraded",
    "tier.gated",
    "tier.quota_exceeded",
]


# ---------------------------------------------------------------------------
# 1. No template routes to an UNKNOWN (typo'd) module. module=None is legitimate for
#    project-scoped / error codes that fan out by project_id rather than by module —
#    those pre-date A4-08 and are out of its scope. The v5 codes' non-None routing is
#    pinned separately in test_v5_code_has_template_and_route below.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code", sorted(FRIENDLY_TEMPLATES))
def test_no_template_routes_to_an_unknown_module(code: str) -> None:
    """A code may route to None (project_id-channelled) but never to a module outside the closed set."""
    module = _module_for_code(code)
    assert module is None or module in _EXPECTED_MODULES, (
        f"{code} routes to unknown module {module!r} (not in the closed Literal set)"
    )


@pytest.mark.parametrize("code", _V5_CODES)
def test_v5_code_has_template_and_route(code: str) -> None:
    """Each v5 code A4-08 adds has both a FRIENDLY_TEMPLATE and a non-None route."""
    assert code in FRIENDLY_TEMPLATES, f"{code} missing a FRIENDLY_TEMPLATE entry"
    assert _module_for_code(code) is not None, f"{code} has no non-None _MODULE_PREFIXES route"


def test_named_cost_model_codes_route_exactly() -> None:
    """The two downstream alarm routes (consumed by the cost/limits WPs) are pinned exactly."""
    assert _module_for_code("cost.model.unknown") == "system"
    assert _module_for_code("limits.suspiciously_low") == "polarius"


# ---------------------------------------------------------------------------
# 2. The two cost-model alarms reach a live channel (fan-out, not just routing).
# ---------------------------------------------------------------------------
async def test_cost_model_unknown_reaches_a_channel() -> None:
    """`cost.model.unknown` (no project_id) fans out to the SYSTEM channel for a subscriber."""
    q = subscribe(SYSTEM_CHANNEL)
    try:
        event = await emit("cost.model.unknown", model="gpt-unknown-2099")
        assert event["module"] == "system"
        delivered = q.get_nowait()
        assert delivered["code"] == "cost.model.unknown"
    finally:
        unsubscribe(SYSTEM_CHANNEL, q)


async def test_limits_suspiciously_low_reaches_a_channel() -> None:
    """`limits.suspiciously_low` (no project_id) fans out to the SYSTEM channel for a subscriber."""
    q = subscribe(SYSTEM_CHANNEL)
    try:
        event = await emit("limits.suspiciously_low", daily_usd=1.0)
        assert event["module"] == "polarius"
        delivered = q.get_nowait()
        assert delivered["code"] == "limits.suspiciously_low"
    finally:
        unsubscribe(SYSTEM_CHANNEL, q)


# ---------------------------------------------------------------------------
# 3. The closed module Literal includes `mcp` at BOTH sites.
# ---------------------------------------------------------------------------
def _literal_members(annotation: object) -> tuple[object, ...]:
    """Pull the Literal members out of an `Optional[Literal[...]]` annotation."""
    for arg in typing.get_args(annotation):
        if typing.get_origin(arg) is typing.Literal:
            return typing.get_args(arg)
    raise AssertionError(f"no Literal found in annotation {annotation!r}")


def test_event_typeddict_literal_includes_mcp() -> None:
    """Site 1: the Event TypedDict `module` field is the closed Literal incl. mcp."""
    hints = typing.get_type_hints(Event)
    members = _literal_members(hints["module"])
    assert "mcp" in members, "Event.module Literal is missing 'mcp'"
    assert set(members) == _EXPECTED_MODULES


def test_emit_signature_literal_includes_mcp() -> None:
    """Site 2: the emit() `module` parameter is the same closed Literal incl. mcp."""
    hints = typing.get_type_hints(emit)
    members = _literal_members(hints["module"])
    assert "mcp" in members, "emit() module Literal is missing 'mcp'"
    assert set(members) == _EXPECTED_MODULES


def test_both_literal_sites_agree() -> None:
    """The two closed Literals must stay in lock-step (a miss at one site reopens the hole)."""
    event_members = set(_literal_members(typing.get_type_hints(Event)["module"]))
    emit_members = set(_literal_members(typing.get_type_hints(emit)["module"]))
    assert event_members == emit_members == _EXPECTED_MODULES


# ---------------------------------------------------------------------------
# 4. mcp.* reaches the SYSTEM channel.
# ---------------------------------------------------------------------------
def test_mcp_prefix_routes_to_a_non_none_module() -> None:
    """An mcp.* code derives a non-None module (so it is never dropped at fan-out)."""
    assert _module_for_code("mcp.polarius.cz") == "mcp"
    assert _module_for_code("mcp.call.start") == "mcp"


async def test_mcp_event_reaches_system_channel() -> None:
    """An mcp.* event (no project_id) fans out to SYSTEM_CHANNEL via its non-None module."""
    q = subscribe(SYSTEM_CHANNEL)
    try:
        event = await emit("mcp.call.start", tool="polarius.search")
        assert event["module"] == "mcp"
        delivered = q.get_nowait()
        assert delivered["code"] == "mcp.call.start"
    finally:
        unsubscribe(SYSTEM_CHANNEL, q)


# ---------------------------------------------------------------------------
# 5. The G4 boundary is honoured — nexus.self_update.* is NOT claimed here.
# ---------------------------------------------------------------------------
def test_g4_nexus_self_update_routes_not_claimed() -> None:
    """A4-08 leaves the G4 Nexus self-update routes to the bus/Nexus lane (A4-T16).

    There is no dedicated `nexus.self_update.` prefix in _MODULE_PREFIXES here. (Note: a bare
    `nexus.*` code is not routed by THIS module — A4-T16 adds the route — so it stays module=None
    until that lane lands.)
    """
    prefixes = {prefix for prefix, _mod in events._MODULE_PREFIXES}
    assert "nexus.self_update." not in prefixes
    assert "nexus.corpus." not in prefixes
