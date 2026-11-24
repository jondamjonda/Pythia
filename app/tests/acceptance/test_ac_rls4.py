"""AC-RLS-4 — the NAMED cross-tenant-isolation gate for the ``rls_enforced`` flip.

This is the acceptance gate the C1 enforce-flip checklist hangs on (manifest
T-C1a, FLAG_CATALOGUE: ``rls_enforced`` HOLD on LEDGER-C1). It has TWO halves,
both of which must be green before ``rls_enforced`` may flip:

  PART A — the two-request-same-pooled-connection LEAK PROBE. We drive the
  migration-015 tenant predicate through ``scripts/rls_leak_test.sql`` (the T1–T8
  battery: read isolation both ways, no-GUC fail-closed, WITH CHECK blocks
  cross-tenant + NULL-org writes, platform-admin break-glass). The script acts as
  the NOBYPASSRLS ``regsuper_app`` role on ONE connection across multiple GUC
  switches, which is exactly the pooled-connection reuse the gate must isolate. A
  leak RAISEs and exits non-zero (ON_ERROR_STOP), failing PART A.

  PART B — the CONNECTION-ROLE assertion (the load-bearing LEDGER-C1 check the
  existing ``tests/test_rls_cross_tenant_leak.py`` does NOT make): the app's live
  DB role MUST be NOBYPASSRLS (``rolbypassrls = false``) and non-superuser. A
  superuser / BYPASSRLS / RLS-exempt-owner connection is EXEMPT from every policy,
  so PART A could pass in a scratch DB while the real app still silently leaks
  across tenants. PART B closes that gap by probing the actual connection role.

WHY PART B is designed to FAIL on the default config and PASS as ``regsuper_app``:
  The default local DSN connects as the ``regsuper`` table OWNER (BYPASSRLS /
  superuser in dev). Running PART B against THAT connection proves the gate
  DETECTS the leak: it would assert-fail. Running it against the ``regsuper_app``
  NOBYPASSRLS connection (the ``run_local.sh`` mode) is what makes it PASS. To keep
  the DEFAULT pytest suite green in a DB-less / owner-connection environment, PART
  B SKIPS cleanly unless explicitly armed — see ``REGSUPER_RLS4_ENFORCE_ROLE`` below.

HOW THE OPERATOR RUNS THIS AS THE GATE (documented for the C1 flip checklist):

  1. PART A (no special config — runs whenever local Docker Postgres is up):
         .venv/bin/pytest tests/acceptance/test_ac_rls4.py::test_ac_rls4_part_a_leak_probe

  2. PART B as the app role (asserts PASS — this is the green the flip needs):
         DATABASE_URL='postgresql+asyncpg://regsuper_app:<pw>@localhost:5432/regsuper' \
         REGSUPER_RLS4_ENFORCE_ROLE=1 \
         .venv/bin/pytest tests/acceptance/test_ac_rls4.py::test_ac_rls4_part_b_connection_role_is_nobypassrls

  3. (Optional) PART B armed against the OWNER DSN proves the gate detects the
     leak — it FAILS, demonstrating the owner connection is RLS-exempt:
         REGSUPER_RLS4_ENFORCE_ROLE=1 \
         .venv/bin/pytest tests/acceptance/test_ac_rls4.py::test_ac_rls4_part_b_connection_role_is_nobypassrls

In the default (unarmed) suite PART B reports the role and SKIPs when the
connection is RLS-exempt, so it never reddens CI; armed, it is the hard gate.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_rls_leak_test.sh"

# When set to a truthy value, PART B turns an RLS-exempt connection from a SKIP
# into a hard FAILURE — i.e. it ENFORCES that the connection is NOBYPASSRLS. The
# operator arms this when running the gate; the default suite leaves it unset so a
# DB-less / owner-connection dev box stays green.
_ENFORCE_ROLE = os.environ.get("REGSUPER_RLS4_ENFORCE_ROLE", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)


# ===========================================================================
# PART A — the leak probe (drives scripts/rls_leak_test.sql via its runner).
# ===========================================================================
def _docker_pg_available() -> bool:
    if not shutil.which("docker") or not _SCRIPT.exists():
        return False
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return "regsuper-postgres" in out.stdout
    except Exception:
        return False


@pytest.mark.skipif(
    not _docker_pg_available(),
    reason="local Docker 'regsuper-postgres' not available — AC-RLS-4 PART A needs a live Postgres",
)
def test_ac_rls4_part_a_leak_probe():
    """The migration-015 predicate isolates tenants across a single pooled
    (NOBYPASSRLS regsuper_app) connection that switches the org GUC between
    requests — no cross-tenant read OR write (T1–T8). Any leak fails."""
    result = subprocess.run(["bash", str(_SCRIPT)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, (
        "AC-RLS-4 PART A FAILED — tenant isolation is NOT enforced by the "
        "migration-015 predicate:\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "ALL TESTS PASSED" in result.stdout, f"unexpected output:\n{result.stdout}"


# ===========================================================================
# PART B — the connection-role assertion (the LEDGER-C1 load-bearing check).
# ===========================================================================
async def _resolve_connection_role() -> tuple[str, bool, bool] | None:
    """Return (current_user, is_superuser, is_bypassrls) for the app's live DB
    connection, or None if no DB is reachable / the role row is unresolvable.

    Disposes the module-level engine first so this runs on the current event loop
    (the same dispose-between-DB-tests pattern the entitlements conftest uses)."""
    from sqlalchemy import text

    from regsuper.storage.database import dispose_engine, session_scope

    await dispose_engine()
    try:
        async with session_scope() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT current_user, rolsuper, rolbypassrls "
                        "FROM pg_roles WHERE rolname = current_user"
                    )
                )
            ).first()
    except Exception:
        return None
    finally:
        await dispose_engine()
    if row is None:
        return None
    return str(row[0]), bool(row[1]), bool(row[2])


async def test_ac_rls4_part_b_connection_role_is_nobypassrls():
    """The app's live DB role must be NOBYPASSRLS + non-superuser, else RLS
    isolates nothing (LEDGER-C1).

      * no DB reachable          → SKIP (DB-less CI stays green).
      * role is RLS-exempt, NOT armed → SKIP (default owner DSN; reports the role).
      * role is RLS-exempt, ARMED     → FAIL (proves the gate detects the leak).
      * role is NOBYPASSRLS           → PASS (the green the C1 flip requires).
    """
    resolved = await _resolve_connection_role()
    if resolved is None:
        pytest.skip("no Postgres reachable (DATABASE_URL) — AC-RLS-4 PART B skipped")

    current_user, is_super, is_bypass = resolved
    rls_exempt = is_super or is_bypass

    if rls_exempt and not _ENFORCE_ROLE:
        attr = "superuser" if is_super else "BYPASSRLS"
        pytest.skip(
            f"app connects as DB role {current_user!r} which is {attr} (RLS-EXEMPT). "
            "AC-RLS-4 PART B is the gate that this is WRONG for an enforced deploy; "
            "it is skipped in the default suite. Re-run as the NOBYPASSRLS "
            "regsuper_app connection (run_local.sh mode) — or arm "
            "REGSUPER_RLS4_ENFORCE_ROLE=1 to make this a hard failure that proves "
            "the gate detects the owner-connection leak (LEDGER-C1)."
        )

    # Armed against an exempt role → this assertion FAILS (the detection proof).
    # Against the regsuper_app NOBYPASSRLS role → both are False → PASS.
    assert not is_super, (
        f"AC-RLS-4 PART B FAILED: app connects as SUPERUSER role {current_user!r}, "
        "which is exempt from every RLS policy — enforcing RLS would isolate "
        "NOTHING (silent cross-tenant reads). Connect as regsuper_app (LEDGER-C1)."
    )
    assert not is_bypass, (
        f"AC-RLS-4 PART B FAILED: app connects as BYPASSRLS role {current_user!r}, "
        "which is exempt from every RLS policy — enforcing RLS would isolate "
        "NOTHING (silent cross-tenant reads). Connect as a NOBYPASSRLS role "
        "(regsuper_app) before flipping RLS_ENFORCED (LEDGER-C1)."
    )
