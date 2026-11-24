"""TC-3c — the as-of PROSE renderer proof (additive, gated default-OFF, read-only).

Extends the TC-3b as-of renderer from /v1/retrieve (CitedProvision.text_md) to the /v1/answer PROSE path:
reasoning/answer.py._fetch overlays the prose `txt` (and the answer's cited text rebuilt from it) with the
version-in-force text at a strictly-PAST as_of, fetched THROUGH the door (NexusPort.asof_text — never the
graph impl, so the single-door boundary holds). Same flag as TC-3b (temporal_asof_render_enabled).

DB-free (a fake Nexus + the door's no-DB gating):
  (a) OFF byte-identical    — render_asof=False ⇒ `txt` is the head text, IDENTICAL whether or not a version
      overlay exists.
  (b) ON overlays           — render_asof=True + a strictly-past as_of ⇒ `txt` is the version-in-force text
      (truncated to 700), only for ids the overlay returns; others keep the head text.
  (c) ON honest fallback    — render_asof=True but as_of None, or the overlay empty ⇒ the head text.
  (d) door gating (no DB)   — NexusPort.asof_text returns {} when the substrate flag is OFF, or as_of is
      None/today (not strictly past), BEFORE any DB call.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import pythia.ports as ports
import pythia.reasoning.answer as answer_mod
from pythia.config import PythiaSettings

_HEAD = "HEAD prose text — the current head wording of the provision."
_VERSION = "OLD prose text — the wording in force at the past as_of date."
_PAST = date(2000, 1, 1)


def _row(pid: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=pid,
        label="§ 1",
        external_id="1/2020",
        source_title="Act",
        jurisdiction="CZ",
        text=text,
        bindingness="binding",
        is_authoritative=True,
        temporal_status="IN_FORCE",
        legal_hierarchy="ZAKON",
        pit_status=None,
        stale_vector=None,
    )


class _FakeNexus:
    """fetch_provisions returns the head rows; asof_text returns the supplied {id: version-text} overlay
    (the real past/substrate gating lives in the door method, tested separately)."""

    def __init__(self, rows, asof_overlay):
        self._rows = rows
        self._asof = asof_overlay

    async def fetch_provisions(self, ids):
        return [r for r in self._rows if r.id in ids]

    async def asof_text(self, ids, as_of):
        return {k: v for k, v in self._asof.items() if k in ids}


def _fetch(rows, asof_overlay, *, as_of, render_asof):
    fake = _FakeNexus(rows, asof_overlay)
    # _fetch instantiates NexusPort() internally — patch the name in the answer module.
    import unittest.mock as M

    with M.patch.object(answer_mod, "NexusPort", lambda: fake):
        return asyncio.run(answer_mod._fetch([r.id for r in rows], as_of=as_of, render_asof=render_asof))


# ── (a) OFF byte-identical ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("overlay", [{}, {"p1": _VERSION}])
def test_off_serves_head_text(overlay):
    meta = _fetch([_row("p1", _HEAD)], overlay, as_of=_PAST, render_asof=False)
    assert meta["p1"]["txt"] == _HEAD[:700]


# ── (b) ON overlays the past-version prose text ──────────────────────────────────────────────────
def test_on_overlays_version_text():
    meta = _fetch([_row("p1", _HEAD)], {"p1": _VERSION}, as_of=_PAST, render_asof=True)
    assert meta["p1"]["txt"] == _VERSION[:700]
    assert meta["p1"]["txt"] != _HEAD[:700]


def test_on_overlays_only_matched_ids():
    rows = [_row("p1", _HEAD), _row("p2", _HEAD)]
    meta = _fetch(rows, {"p1": _VERSION}, as_of=_PAST, render_asof=True)  # only p1 has a version
    assert meta["p1"]["txt"] == _VERSION[:700]
    assert meta["p2"]["txt"] == _HEAD[:700]  # p2 keeps the head text


def test_on_truncates_version_to_700():
    meta = _fetch([_row("p1", _HEAD)], {"p1": "V" * 1000}, as_of=_PAST, render_asof=True)
    assert len(meta["p1"]["txt"]) == 700


# ── (c) ON honest fallback ───────────────────────────────────────────────────────────────────────
def test_on_none_as_of_serves_head():
    meta = _fetch([_row("p1", _HEAD)], {"p1": _VERSION}, as_of=None, render_asof=True)
    assert meta["p1"]["txt"] == _HEAD[:700]  # no as_of → no overlay


def test_on_empty_overlay_serves_head():
    meta = _fetch([_row("p1", _HEAD)], {}, as_of=_PAST, render_asof=True)
    assert meta["p1"]["txt"] == _HEAD[:700]  # overlay returned nothing → head text


# ── (d) door gating (no DB) ──────────────────────────────────────────────────────────────────────
def _patch_substrate(monkeypatch, enabled: bool):
    s = PythiaSettings()
    s.temporal_substrate_enabled = enabled
    monkeypatch.setattr(ports, "get_settings", lambda: s, raising=True)


def test_door_returns_empty_when_substrate_off(monkeypatch):
    _patch_substrate(monkeypatch, False)  # OFF ⇒ {} for a past as_of, BEFORE any DB call
    out = asyncio.run(ports.NexusPort().asof_text(["p1"], _PAST))
    assert out == {}


@pytest.mark.parametrize("as_of", [None, date.today(), date.today() + timedelta(days=5)])
def test_door_returns_empty_for_non_past_as_of(monkeypatch, as_of):
    _patch_substrate(monkeypatch, True)  # substrate ON but as_of not strictly past ⇒ {} (no DB)
    out = asyncio.run(ports.NexusPort().asof_text(["p1"], as_of))
    assert out == {}


def test_door_returns_empty_for_empty_ids(monkeypatch):
    _patch_substrate(monkeypatch, True)
    assert asyncio.run(ports.NexusPort().asof_text([], _PAST)) == {}
