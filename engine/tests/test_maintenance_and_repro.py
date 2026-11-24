"""Regression guards for the three remediations — all DB-free (import + filesystem only):

1) NexusPort exposes the control-room feeds (nexus_stats / nexus_events / nexus_health), so the
   sidecar routes through the single door instead of importing pythia.graph.nexus_stats directly.
   (The import-time boundary itself is enforced by test_nexus_port_boundary.py.)
2) The scheduler module exists and exposes the schedulable entrypoints + emits valid cron lines.
3) The committed GNN checkpoint manifest verifies against the on-disk .pt files (reproducibility).
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

_BUILD = pathlib.Path(__file__).resolve().parent.parent


# ── 1) NexusPort owns the control-room feeds (no graph.* import in service.py) ────────────────────
def test_nexus_port_exposes_control_room_feeds():
    from pythia.ports import NexusPort

    port = NexusPort()
    for name in ("nexus_stats", "nexus_events", "nexus_health"):
        assert callable(getattr(port, name, None)), f"NexusPort.{name} missing — sidecar can't route via the door"


def test_service_does_not_import_nexus_stats_impl():
    src = (_BUILD / "pythia" / "service.py").read_text()
    assert "from pythia.graph.nexus_stats" not in src, (
        "service.py must reach nexus stats through NexusPort, not import the impl module"
    )


# ── 2) the scheduler is a real entrypoint (replaces the commented-out stub) ───────────────────────
def test_scheduler_entrypoints_exist():
    from pythia.ingest import scheduler

    for name in (
        "run_temporal",
        "run_refresh",
        "run_daily",
        "main",
        "run_discover",
        "run_auto_ingest",
        "run_graph_changed_drain",
    ):
        assert hasattr(scheduler, name), f"scheduler.{name} missing"


def test_scheduler_prints_valid_crontab():
    out = subprocess.run(
        [sys.executable, "-m", "pythia.ingest.scheduler", "--print-crontab"],
        cwd=_BUILD,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    lines = [line for line in out.splitlines() if line and not line.startswith("#")]
    # 4 scheduled jobs: temporal + refresh (maintenance) and discover + auto-ingest (auto-ingestion).
    assert len(lines) == 4, f"expected 4 cron lines, got {lines}"
    for line in lines:
        assert "pythia.ingest.scheduler" in line and str(_BUILD) in line, f"cron line not self-contained: {line}"
        assert len(line.split()[:5]) == 5, f"cron line missing its 5 schedule fields: {line}"
    assert any("temporal --apply" in line for line in lines), "missing the daily temporal recompute line"
    assert any("scheduler discover" in line for line in lines), "missing the daily discovery line"
    assert any("scheduler auto-ingest" in line for line in lines), "missing the weekly auto-ingest line"


# ── 3) the GNN checkpoint manifest matches the on-disk weights (reproducibility) ──────────────────
def test_checkpoint_manifest_verifies():
    manifest = _BUILD / "train" / "checkpoints.manifest.json"
    if not manifest.exists():
        pytest.skip("no checkpoint manifest committed yet (run: python train/checkpoints.py write)")
    files = json.loads(manifest.read_text()).get("files", {})
    assert files, "manifest tracks no checkpoints"
    if not any((_BUILD / "train" / name).exists() for name in files):
        pytest.skip("no local .pt checkpoints on this machine — manifest is the only record (expected on a clone)")
    r = subprocess.run(
        [sys.executable, str(_BUILD / "train" / "checkpoints.py"), "verify"], cwd=_BUILD, capture_output=True, text=True
    )
    assert r.returncode == 0, f"checkpoint verify failed:\n{r.stdout}\n{r.stderr}"
