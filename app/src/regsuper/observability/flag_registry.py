"""Registry of the answer-surface feature flags and the gate each must clear before it is enabled.

The answer-quality (AQ) work added a family of app flags for the answer surface (the `answer_*`
flags, the spend gate on the engine call, and the claim-ledger export), all off by default. The
risk this module removes is drift: a new or renamed flag landing in `Settings` without a
recorded gate and without a `flip_preflight._SAFE_DEFAULTS` entry, so it could be turned on with
no review and would be missing from the preflight's report of non-default flags.

This module is the single source of truth. For each flag it records the gate that must clear
before the flag is turned on in a serving environment, plus a one-line effect, and it:

* generates the committed `docs/FLAG_GATES.md` (`render_markdown`), which is never edited by
  hand;
* is compared with the live `Settings` by `registry_vs_settings_drift`, so a flag added or
  renamed without a matching registry row (or the reverse) fails CI; and
* feeds the `_SAFE_DEFAULTS` completeness test (`aq_flag_names`), so every registered flag is
  known to `flip_preflight` and is reported when turned on.

Pure: no I/O, clock or database access. Importing it changes no behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass

from regsuper.config import Settings

# The generated document, relative to the app's root directory.
REGISTRY_DOC_NAME = "docs/FLAG_GATES.md"


@dataclass(frozen=True)
class FlagGate:
    """One flag's governance row.

    Holds the exact ``Settings`` field name, the named gate that must clear before the flag is
    turned on, and a one-line effect. The default value is read from ``Settings`` when rendering,
    never typed by hand.
    """

    name: str
    gate: str
    effect: str


# The answer-surface flag registry. Each row names the gate that governs turning the flag on.
# Other app flags (tenancy, RLS, entitlements, compliance, L4, corpus cutover, Conduit, async jobs,
# security, accounting, PDF export, and the applicability-filter family) are deliberately excluded:
# they have their own rollout gates. They are still listed in ``_SAFE_DEFAULTS``, so the preflight
# still reports them when they differ from their defaults.
_REGISTRY: tuple[FlagGate, ...] = (
    # Put the app's call to the engine's /v1/answer behind the spend cap and kill switch.
    FlagGate(
        "pythia_boundary_spend_gate_enabled",
        "gate #4 (operator sets realistic data/limits.json caps) — else the $1/day test cap refuses answers",
        "gate the engine /v1/answer boundary call behind killswitch + enforce_spend_limit(provider=openai)",
    ),
    # The app's answer route, POST /api/answer.
    FlagGate(
        "answer_endpoint_enabled",
        "gate #4 (G14 lawyer sign-off for the PRODUCTION copy / any paid launch) — exposes the surface only",
        "mount POST /api/answer live (else the route 404s); WHAT the engine returns rides the engine flags",
    ),
    # Show a hedged draft instead of refusing when coverage is weak (shadow mode only).
    FlagGate(
        "answer_shadow_weak_coverage_hedged",
        "gate #4 (operator decision, metagate SHADOW) — NOT an enforce flip",
        "surface a grounded self-hedged draft for weak_coverage instead of refusing (shadow only; scoped)",
    ),
    # Pass a saved business profile into the /api/answer intake.
    FlagGate(
        "answer_profile_context_enabled",
        "gate #3 (A2b retrieval A/B) — retrieval/intake seed only, never reaches cascade/verdict",
        "pass a whitelisted profile fact dict as project_context so known facts seed intake extraction",
    ),
    # Ask deterministic clarifying questions in the app, before quota or the engine is used.
    FlagGate(
        "answer_clarify_enabled",
        "gate #1 (advocate/lawyer review of MATTER_REQUIRED_SLOTS + the Czech clarify copy)",
        "return a 200 clarify frame (≤3 fixed Czech Qs) BEFORE quota/engine when a required slot is missing",
    ),
    # Show the engine's structured practical block.
    FlagGate(
        "answer_practical_block_enabled",
        "gate #4 (rides the engine structured_answer_enabled G14 gate) — inert until the engine emits .practical",
        "extend AC-CITATION-CHECK to the typed practical block (drop ungrounded items, pass gaps through)",
    ),
    # Show the intake "understanding" panel.
    FlagGate(
        "answer_intake_panel_enabled",
        "gate #3 (rides the engine intake_v2 artifact) — whitelisted, never persisted to api_call.request_json",
        "surface a whitelisted intake block (sub-Qs/facts/conflicts/sufficiency/dropped) + profile suggestions",
    ),
    # Check the spend cap and kill switch in the answer handler, before the engine call.
    FlagGate(
        "answer_budget_precheck_enabled",
        "gate #4 (operator sets realistic data/limits.json caps) — belt-and-suspenders to AQ-02",
        "assert_not_killed + enforce_spend_limit BEFORE the sidecar call; capped ⇒ structured refuse + refund",
    ),
    # Export claim ledgers to disk as canonical-JSON audit bundles.
    FlagGate(
        "claim_ledger_export_enabled",
        "gate #3 (observe-only, best-effort background) — WP-029 redaction applies; query text only if capture ON",
        "fire-and-forget a canonical-JSON + SHA-256 claim-ledger bundle to data/exports/ after a verify run",
    ),
)


# Recorded deviations and reconciliations, rendered into the generated document.
DEVIATIONS: tuple[str, ...] = (
    "FLAG-NAME RECONCILIATION — the registry rows use the REAL Settings field names; any contract/plan "
    "'phantom' names map to the concrete field here. The lockstep drift check pins that no phantom leaks in "
    "and no real AQ flag is missing a row.",
    "_SAFE_DEFAULTS COMPLETENESS — this registry is the source the `_SAFE_DEFAULTS` completeness test reads: "
    "every AQ app flag here MUST have a `flip_preflight._SAFE_DEFAULTS` row, else the boot non-default-flag "
    "line would silently omit a flipped AQ flag. WS8 step VL-G-10 added the 6 missing answer-surface rows "
    "(answer_shadow_weak_coverage_hedged, answer_profile_context_enabled, answer_clarify_enabled, "
    "answer_practical_block_enabled, answer_intake_panel_enabled, pythia_boundary_spend_gate_enabled).",
    "T-LOCAL TEST-POSTURE — the app answer-surface flags are set ON in the UNCOMMITTED local run scripts "
    "(run_aq_live.sh: ANSWER_ENDPOINT_ENABLED / ANSWER_INTAKE_PANEL_ENABLED / ANSWER_PRACTICAL_BLOCK_ENABLED "
    "/ ANSWER_PROFILE_CONTEXT_ENABLED). metagate stays SHADOW; claim_verify enforce is authorized LOCALLY only.",
    "R5 STANDING BREACH (honestly recorded) — those local run scripts run the AQ surface ON as a RECORDED "
    "OPERATOR TEST POSTURE pending gate #4, NOT a committed serving default. `main`/committed config remains "
    "100% default-OFF (pinned by the byte-identical guard tests); the registry governs the serving FLIP, which "
    "has not happened. The answer PRODUCTION copy + any paid launch ride the deferred G14 human gate.",
    "WS13 GOVERNANCE RECORD (step 96/103) — every deliberate deviation from the plan-contract is recorded in "
    "`docs/deploy/deployment-amendments.md`; the human/operator gates that must clear before any user-visible "
    "flip (and the stacked shadow->enforce checklist) are the runway `docs/deploy/GATES_STATUS.md`. This "
    "registry governs WHAT flips; those docs govern WHY (amendments) and WHEN (gates). No flip has happened.",
)


def aq_flag_names() -> set[str]:
    """Return the flag names the registry governs (the set the _SAFE_DEFAULTS test checks)."""
    return {f.name for f in _REGISTRY}


def settings_aq_flag_names() -> set[str]:
    """Return the answer-surface flag names found on ``Settings`` by pattern.

    This is the other side of the drift check. A flag belongs to the family if it is a boolean
    field whose name starts with ``answer_``, or is one of the two listed exceptions (the engine
    spend gate and the claim-ledger export). The match is deliberately narrow; other flag
    families have their own gates.
    """
    explicit_aq = {"pythia_boundary_spend_gate_enabled", "claim_ledger_export_enabled"}
    out: set[str] = set()
    for name, field in Settings.model_fields.items():
        if field.annotation is not bool:
            continue
        if name.startswith("answer_") or name in explicit_aq:
            out.add(name)
    return out


def registry_vs_settings_drift() -> dict[str, list[str]]:
    """Report drift between the registry and ``Settings``.

    ``missing_in_registry`` lists answer-surface flags on ``Settings`` with no registry row (an
    ungoverned flag); ``stale_in_registry`` lists rows whose field no longer exists. Empty lists
    mean no drift. Pure.
    """
    reg = aq_flag_names()
    live = settings_aq_flag_names()
    settings_fields = set(Settings.model_fields)
    return {
        "missing_in_registry": sorted(live - reg),
        "stale_in_registry": sorted(n for n in reg if n not in settings_fields),
    }


def _defaults() -> dict[str, object]:
    """Return each registered flag's default, read from a fresh default ``Settings``."""
    s = Settings()
    return {f.name: getattr(s, f.name) for f in _REGISTRY}


def render_markdown() -> str:
    """Render ``docs/FLAG_GATES.md`` from the registry and the ``Settings`` defaults.

    Deterministic. A test regenerates the document in memory and compares it with the committed
    copy, so adding or renaming a flag without regenerating the document fails CI.
    """
    defaults = _defaults()
    lines: list[str] = []
    lines.append("# Verification Loop Flip Gates — APP (`regsuper`)")
    lines.append("")
    lines.append(
        "GENERATED by `regsuper.observability.flag_registry::render_markdown` from the live `Settings` — "
        "**do NOT hand-edit**. Regenerate with `python -m regsuper.observability.flag_registry --write`. "
        "Every AQ-era answer-surface app flag appears here with its DEFAULT, the named GATE that must clear "
        "before a serving-env flip, and its one-line effect. `tests/observability/test_flip_gate_registry.py` "
        "fails on any drift (an AQ Settings flag with no registry row, or a stale row), on a stale committed "
        "copy of this file, OR when an AQ flag is missing from `flip_preflight._SAFE_DEFAULTS`."
    )
    lines.append("")
    lines.append("## AQ answer-surface flip flags")
    lines.append("")
    lines.append("| Flag | Default | Named gate (must clear before flip) | Effect when ON |")
    lines.append("| --- | --- | --- | --- |")
    for f in _REGISTRY:
        lines.append(f"| `{f.name}` | `{defaults[f.name]!r}` | {f.gate} | {f.effect} |")
    lines.append("")
    lines.append("## Recorded deviations / reconciliations")
    lines.append("")
    for d in DEVIATIONS:
        lines.append(f"- {d}")
    lines.append("")
    return "\n".join(lines)


def _write_doc() -> str:
    """Write the generated markdown to the app's docs directory and return the path."""
    from pathlib import Path

    # This file is app/src/regsuper/observability/flag_registry.py, so parents[3] is app/.
    doc = Path(__file__).resolve().parents[3] / REGISTRY_DOC_NAME
    doc.write_text(render_markdown(), encoding="utf-8")
    return str(doc)


if __name__ == "__main__":  # pragma: no cover - CLI convenience
    import sys

    if "--write" in sys.argv:
        print(f"wrote {_write_doc()}")
    else:
        print(render_markdown())
