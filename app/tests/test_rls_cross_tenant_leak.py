"""AC-RLS-4 — the cross-tenant leak gate, integrated into the suite.

This is a real-DB integration test: it shells out to scripts/run_rls_leak_test.sh, which provisions an ISOLATED
scratch DB on the local Docker Postgres, applies the migration-015 tenant predicate, and asserts tenant isolation
(read isolation both ways, no-GUC fail-closed, WITH CHECK blocks cross-tenant writes, platform-admin break-glass).

It SKIPS cleanly when the local Docker `regsuper-postgres` is not available (CI without Docker), so it never
turns the suite red in a DB-less environment — but it RUNS and gates whenever the DB is up. The script exits
non-zero on any leak (ON_ERROR_STOP in the SQL), which fails this test.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_rls_leak_test.sh"


def _docker_pg_available() -> bool:
    if not shutil.which("docker") or not _SCRIPT.exists():
        return False
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=10
        )
        return "regsuper-postgres" in out.stdout
    except Exception:
        return False


@pytest.mark.skipif(
    not _docker_pg_available(),
    reason="local Docker 'regsuper-postgres' not available — AC-RLS-4 needs a live Postgres",
)
def test_ac_rls_4_cross_tenant_leak():
    """Tenant isolation must hold under the migration-015 predicate (no cross-tenant read OR write)."""
    result = subprocess.run(["bash", str(_SCRIPT)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, (
        "AC-RLS-4 cross-tenant leak test FAILED — tenant isolation is NOT enforced:\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "ALL TESTS PASSED" in result.stdout, f"unexpected output:\n{result.stdout}"
