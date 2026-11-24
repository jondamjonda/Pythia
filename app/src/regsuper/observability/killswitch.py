"""Kill switch: a flag file that long-running scripts and request handlers check.

While `data/KILL_SWITCH` exists, code that respects the switch stops between units of work.
The mechanism is deliberately coarse (no IPC, no shared memory): a single `touch` from the
dashboard or a shell halts the whole pipeline, and a single `rm` re-enables it. The file's
content is the reason shown in the dashboard.

Long-running scripts call `check_kill_switch()` between work units; FastAPI endpoints call
`assert_not_killed()` when a request arrives.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from regsuper.config import get_settings

KILL_FILENAME = "KILL_SWITCH"


def kill_path() -> Path:
    return get_settings().data_dir / KILL_FILENAME


def is_killed() -> bool:
    return kill_path().exists()


def state() -> dict[str, Any]:
    """Return the switch state, reason and engagement time (used by the admin dashboard)."""
    p = kill_path()
    if not p.exists():
        return {"active": False, "path": str(p)}
    try:
        reason = p.read_text(encoding="utf-8").strip()
    except OSError:
        reason = ""
    try:
        st = p.stat()
        engaged_at = datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat()
    except OSError:
        engaged_at = None
    return {
        "active": True,
        "path": str(p),
        "reason": reason or "(no reason given)",
        "engaged_at": engaged_at,
    }


def engage(reason: str = "manual") -> dict[str, Any]:
    """Activate the switch. Idempotent: calling it again only updates the reason."""
    p = kill_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(reason, encoding="utf-8")
    return state()


def release() -> dict[str, Any]:
    """Deactivate the switch. Idempotent: does nothing if it is not active."""
    p = kill_path()
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass
    return state()


class KillSwitchEngaged(RuntimeError):
    """Raised when a kill-switch-respecting code path runs while the switch is on."""


def assert_not_killed() -> None:
    """Raise `KillSwitchEngaged` if the switch is active.

    Call it at FastAPI route entry or before starting a new batch of LLM calls.
    """
    if is_killed():
        s = state()
        raise KillSwitchEngaged(f"kill switch engaged at {s.get('engaged_at')}: {s.get('reason')}")


def check_kill_switch(*, raise_exc: bool = True) -> bool:
    """Check the switch from a long-running script.

    With `raise_exc=True` (the default) an engaged switch raises `KillSwitchEngaged`; with
    False it returns True instead, so the caller can leave its loop cleanly.
    """
    if not is_killed():
        return False
    if raise_exc:
        assert_not_killed()
    return True
