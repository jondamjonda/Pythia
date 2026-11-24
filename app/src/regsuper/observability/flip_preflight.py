"""Startup preflight for feature-flag combinations that are unsafe together.

Several flags are only safe in combination, and a single environment variable could otherwise
break that silently. This module encodes those rules, and it reports every flag that differs
from its safe default so an accidental change is visible in the log.

Rules:

* `rls_enforced` requires `tenancy_enabled`, and requires a database role that is not exempt
  from row-level security (NOBYPASSRLS, not the table owner). The configuration half is checked
  here; the role half is the once-per-process probe in `storage.database`, which runs at the
  first enforced session and can also be run from here (`run_preflight(check_db_role=True)`).
* `cutover_approved` requires `law_source == 'nexus'`, `pythia_enabled` and a configured
  `pythia_url`.
* `conduit_enabled` is refused, because the Conduit service is not finished (its REST handlers
  and MCP registry are still unimplemented). Refusing it here catches the misconfiguration when
  the main app starts rather than in the separate Conduit process.

With every flag at its safe default the checks do nothing: `run_preflight` performs no database
I/O, raises nothing, and logs a single INFO line saying all flags are at their defaults. A
violated rule raises `FlipPreflightError` (or `RlsSafetyError` for the RLS rules), so an unsafe
configuration cannot start silently.

`run_preflight()` is not yet called by the app. To enable it, call it once near the top of the
API lifespan in `api/main.py`, after `configure_logging()`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from regsuper.config import Settings, get_settings
from regsuper.observability.logging_config import get_logger
from regsuper.storage.database import RlsSafetyError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

# The safe default of every flag the preflight reports on; keep in step with config.Settings. A flag
# whose live value differs from the value here is reported at startup. Only feature flags are
# listed: secrets, URLs, regions and tuning knobs are excluded, because their values are
# environment-specific rather than safe or unsafe.
_SAFE_DEFAULTS: dict[str, object] = {
    # Tenancy and row-level security (they must be enabled together).
    "tenancy_enabled": False,
    "rls_enforced": False,
    # Commercial lane.
    "entitlements_enabled": False,
    # Compliance guard (on, in shadow mode, is the safe default).
    "compliance_guard_enabled": True,
    "compliance_guard_mode": "shadow",
    # L4 / corpus cutover.
    "l4_mode": "stub",
    "law_source": "regsuper",
    "cutover_approved": False,
    # Pythia engine.
    "pythia_enabled": False,
    # Conduit (a separate process, not finished yet).
    "conduit_enabled": False,
    # Async analysis.
    "analyze_async": False,
    # Security / admin / accounting.
    "require_auth": False,
    "replay_enabled": False,
    "accounting_integration_enabled": False,
    "project_deletion_enabled": False,
    # Export / answer / digest consumer surfaces.
    "audit_export_enabled": False,
    "export_pdf_enabled": False,
    "audit_evidence_enabled": False,
    "profiles_enabled": False,
    "digest_enabled": False,
    "answer_endpoint_enabled": False,
    # Answer-surface flags. Every flag in flag_registry must appear here, otherwise turning it on
    # would go unreported; a test enforces this.
    "pythia_boundary_spend_gate_enabled": False,
    "answer_shadow_weak_coverage_hedged": False,
    "answer_profile_context_enabled": False,
    "answer_clarify_enabled": False,
    "answer_practical_block_enabled": False,
    "answer_intake_panel_enabled": False,
    # Spend-cap and kill-switch check in the answer handler, before the engine call.
    "answer_budget_precheck_enabled": False,
    # Claim-ledger export to disk (best effort, in the background).
    "claim_ledger_export_enabled": False,
    # Applicability-filter flags and their gap-suppression variants.
    "entity_scoping_enabled": False,
    "applicability_confidence_enabled": False,
    "digest_citation_strategy": "first_only",
    "multi_limb_enabled": False,
    "temporal_freshness_enabled": False,
    "entity_scoping_gap_suppression_enabled": False,
    "multi_limb_gap_dedup_enabled": False,
    "temporal_gap_suppression_enabled": False,
}


class FlipPreflightError(RuntimeError):
    """An unsafe combination of flags that must not be allowed to start.

    The message names the violated rule and how to fix it.
    """


# ---------------------------------------------------------------------------
# Individual rule checks. Each reads only Settings and does nothing unless its triggering flag is
# set.
# ---------------------------------------------------------------------------
def _check_rls_invariants(s: Settings) -> None:
    """Require ``tenancy_enabled`` whenever ``rls_enforced`` is on.

    Raises ``RlsSafetyError``, the same type ``storage.database`` raises, so callers handle one
    exception type for every RLS readiness failure. The database-role check is not a
    configuration check: it is the probe in
    ``storage.database._assert_connection_not_rls_exempt``, run at the first enforced session
    or via ``run_preflight(check_db_role=True)``. Does nothing unless ``rls_enforced`` is True.
    """
    if s.rls_enforced and not s.tenancy_enabled:
        raise RlsSafetyError(
            "flip-preflight: refusing rls_enforced=True while tenancy_enabled=False "
            "(LEDGER-M1). The org RLS GUC is published only by TenancyMiddleware "
            "(tenancy_enabled=True); enforcing RLS without tenancy locks out every "
            "read (fail-closed). Set TENANCY_ENABLED=true together with RLS_ENFORCED=true."
        )


def _check_cutover_invariants(s: Settings) -> None:
    """Require the engine reading path whenever ``cutover_approved`` is on.

    That means ``law_source == 'nexus'``, ``pythia_enabled`` and a configured ``pythia_url``.
    Does nothing unless ``cutover_approved`` is True.
    """
    if not s.cutover_approved:
        return
    problems: list[str] = []
    if s.law_source != "nexus":
        problems.append(
            f"law_source must be 'nexus' (is {s.law_source!r}) — the cutover reads "
            "current law from the Nexus corpus"
        )
    if not s.pythia_enabled:
        problems.append(
            "pythia_enabled must be True — the nexus L1/L2 reads go through the sidecar"
        )
    if not s.pythia_url:
        problems.append("pythia_url must be set — the sidecar must be reachable")
    if problems:
        raise FlipPreflightError(
            "flip-preflight: cutover_approved=True requires the nexus reading path "
            "to be configured (flags-gates-09). Unmet: " + "; ".join(problems) + "."
        )


def _check_conduit_invariants(s: Settings) -> None:
    """Refuse ``conduit_enabled`` while the Conduit service is unfinished.

    Conduit runs as a separate process. Refusing the flag here catches the misconfiguration when
    the main app starts, rather than later in the Conduit process. Does nothing unless
    ``conduit_enabled`` is True.
    """
    if s.conduit_enabled:
        raise FlipPreflightError(
            "flip-preflight: refusing conduit_enabled=True — the Conduit listener is "
            "NOT built yet (WP-062 / LEDGER-M2). create_conduit_app raises "
            "NotImplementedError when True. Keep CONDUIT_ENABLED unset until WP-062 ships."
        )


def non_default_flags(s: Settings | None = None) -> dict[str, object]:
    """Return {flag: live_value} for every flag whose value differs from its safe default.

    Pure: reads Settings only. With every flag at its default this is an empty dict.
    """
    s = s or get_settings()
    out: dict[str, object] = {}
    for name, safe in _SAFE_DEFAULTS.items():
        live = getattr(s, name, safe)
        if live != safe:
            out[name] = live
    return out


def check_invariants(s: Settings | None = None) -> None:
    """Run every configuration rule, raising on the first violation.

    Raises ``RlsSafetyError`` for the RLS and tenancy pairing and ``FlipPreflightError`` for
    the others. With default flags nothing is triggered, so it returns without I/O.
    """
    s = s or get_settings()
    _check_rls_invariants(s)
    _check_cutover_invariants(s)
    _check_conduit_invariants(s)


async def run_preflight(
    *,
    session: AsyncSession | None = None,
    check_db_role: bool = False,
) -> dict[str, object]:
    """Run the startup flag-safety preflight.

    1. Check the configuration rules (``check_invariants``), raising on any unsafe combination.
    2. If ``rls_enforced`` and ``check_db_role`` are both True and a ``session`` is supplied,
       verify that the connection role is not exempt from RLS (reusing
       ``storage.database._assert_connection_not_rls_exempt``). This is off by default so the
       preflight needs no database; the same probe runs at the first enforced session anyway.
    3. Log one INFO line: either that all flags are at their safe defaults, or the sorted flags
       that differ, so an accidental change is visible.

    Returns the map of non-default flags (empty when everything is at its default).
    """
    s = get_settings()
    check_invariants(s)

    if s.rls_enforced and check_db_role and session is not None:
        # Reuse the existing database-role probe rather than reimplementing it.
        from regsuper.storage.database import _assert_connection_not_rls_exempt

        await _assert_connection_not_rls_exempt(session)

    diffs = non_default_flags(s)
    logger = get_logger(__name__)
    if diffs:
        logger.info(
            "flip_preflight_non_default_flags",
            count=len(diffs),
            flags={k: diffs[k] for k in sorted(diffs)},
        )
    else:
        logger.info("flip_preflight_all_flags_at_safe_default")
    return diffs
