"""Engine-side cross-process control signals (post-MVP audit: the embed/GNN loops never polled the
kill-switch and never wrote the MPS heartbeat).

`pythia.ingest._resource_signals` is the shared helper the long torch loops call between batches:
  • check_between_batches() — raises KillSwitchEngaged if Polarius dropped a KILL_SWITCH flag in the
    shared data dir, or FreeDiskExhausted if free disk fell below the floor; no-op otherwise.
  • write_mps_heartbeat(device) — writes gpu_mps.json (read by Polarius's MPS gate + System panel) only
    on MPS; a no-op on CPU and whenever torch/the data dir is unavailable.

Fully isolated: we point PYTHIA_DATA_DIR at a tmp dir and patch shutil.disk_usage. No torch, no DB,
no network. asyncio_mode=auto (pyproject) — but these are plain sync functions, so no async needed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pythia.ingest import _resource_signals as rs


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """Point the helper's settings.data_dir at an isolated tmp dir."""
    monkeypatch.setattr(rs, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    return tmp_path


# --------------------------------------------------------------------------- kill-switch
def test_check_is_noop_when_no_signals(data_dir, monkeypatch):
    # Plenty of free disk, no KILL_SWITCH → returns cleanly.
    monkeypatch.setattr(rs.shutil, "disk_usage", lambda _p: SimpleNamespace(free=500 * rs._GB))
    rs.check_between_batches()  # must not raise


def test_kill_switch_present_raises(data_dir, monkeypatch):
    monkeypatch.setattr(rs.shutil, "disk_usage", lambda _p: SimpleNamespace(free=500 * rs._GB))
    (data_dir / rs.KILL_FILENAME).write_text("watchdog: memory over cap", encoding="utf-8")
    with pytest.raises(rs.KillSwitchEngaged) as ei:
        rs.check_between_batches()
    # the reason from the flag file is surfaced in the message
    assert "watchdog: memory over cap" in str(ei.value)


def test_kill_switch_checked_before_disk(data_dir, monkeypatch):
    # Both tripped → the kill-switch wins (it's checked first; the operator's explicit stop takes priority).
    monkeypatch.setattr(rs.shutil, "disk_usage", lambda _p: SimpleNamespace(free=0))
    (data_dir / rs.KILL_FILENAME).write_text("manual", encoding="utf-8")
    with pytest.raises(rs.KillSwitchEngaged):
        rs.check_between_batches()


# --------------------------------------------------------------------------- free-disk floor
def test_below_floor_raises(data_dir, monkeypatch):
    monkeypatch.setenv("PYTHIA_MIN_FREE_DISK_GB", "5")
    monkeypatch.setattr(rs.shutil, "disk_usage", lambda _p: SimpleNamespace(free=2 * rs._GB))  # 2 < 5
    with pytest.raises(rs.FreeDiskExhausted) as ei:
        rs.check_between_batches()
    assert "below the 5 GB floor" in str(ei.value)


def test_at_or_above_floor_ok(data_dir, monkeypatch):
    monkeypatch.setenv("PYTHIA_MIN_FREE_DISK_GB", "5")
    monkeypatch.setattr(rs.shutil, "disk_usage", lambda _p: SimpleNamespace(free=6 * rs._GB))  # 6 > 5
    rs.check_between_batches()  # must not raise


def test_floor_zero_disables_disk_check(data_dir, monkeypatch):
    monkeypatch.setenv("PYTHIA_MIN_FREE_DISK_GB", "0")
    monkeypatch.setattr(rs.shutil, "disk_usage", lambda _p: SimpleNamespace(free=0))  # bone dry
    rs.check_between_batches()  # floor off → no raise


def test_disk_stat_failure_does_not_block(data_dir, monkeypatch):
    # A stat failure must not halt the harvest — we can't measure, so we don't block.
    monkeypatch.setenv("PYTHIA_MIN_FREE_DISK_GB", "5")

    def boom(_p):
        raise OSError("stat failed")

    monkeypatch.setattr(rs.shutil, "disk_usage", boom)
    rs.check_between_batches()  # must not raise


# --------------------------------------------------------------------------- MPS heartbeat
def test_heartbeat_noop_on_cpu(data_dir):
    rs.write_mps_heartbeat("cpu")
    assert not (data_dir / rs.MPS_HEARTBEAT_FILENAME).exists(), "no heartbeat off MPS"


def test_heartbeat_written_on_mps(data_dir, monkeypatch):
    # Fake a torch with the MPS allocation API so we don't need a GPU.
    fake_torch = SimpleNamespace(
        mps=SimpleNamespace(
            current_allocated_memory=lambda: 1_234_567,
            driver_allocated_memory=lambda: 7_654_321,
        )
    )
    monkeypatch.setitem(__import__("sys").modules, "torch", fake_torch)
    rs.write_mps_heartbeat("mps")
    hb = data_dir / rs.MPS_HEARTBEAT_FILENAME
    assert hb.exists(), "heartbeat file must be written on MPS"
    d = json.loads(hb.read_text(encoding="utf-8"))
    assert d["device"] == "mps"
    assert d["allocated_bytes"] == 1_234_567
    assert d["driver_allocated_bytes"] == 7_654_321
    assert isinstance(d["ts"], (int, float)) and d["ts"] > 0  # wall-clock for the >30s staleness check


def test_heartbeat_swallows_torch_failure(data_dir, monkeypatch):
    # torch present but the MPS API blows up → no crash, no file (advisory only).
    def boom():
        raise RuntimeError("no mps")

    fake_torch = SimpleNamespace(mps=SimpleNamespace(current_allocated_memory=boom, driver_allocated_memory=boom))
    monkeypatch.setitem(__import__("sys").modules, "torch", fake_torch)
    rs.write_mps_heartbeat("mps")  # must not raise
    assert not (data_dir / rs.MPS_HEARTBEAT_FILENAME).exists()
