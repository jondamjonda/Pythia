"""Flip-gate registry for the engine's answer-quality feature flags.

Every answer-quality (AQ) ``*_enabled`` / ``*_mode`` flag in ``PythiaSettings`` defaults to off. This
module is the single source that records, for each one, the named gate that must be cleared before
the flag may be enabled in a serving environment, plus a one-line effect. That prevents drift: a
flag added or renamed without a recorded gate could otherwise be switched on live with no review.

From this declaration the module:
  - generates the committed ``docs/FLAG_GATES.md`` (``render_markdown``), so the doc is never
    maintained by hand; and
  - is checked against the live ``PythiaSettings`` by ``registry_vs_settings_drift``. A settings
    flag without a registry row (or the reverse) is reported, and tests/test_flip_gate_registry.py
    fails on any drift or on a stale committed doc.

It also declares the master-to-sub-flag graph (``SUBFLAG_MASTERS``) that the log-only boot validator
(``subflag_without_master_warnings``) reads to warn when a sub-flag is on while its master is off.

Pure: no I/O, clock or database. Importing it changes no behaviour; it only describes the flags.

Usage:  python -m pythia.flag_registry [--write]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pythia.config import PythiaSettings

_log = logging.getLogger("pythia.flag_registry")

# The committed registry doc, relative to the engine root (the parent of this package).
REGISTRY_DOC_NAME = "docs/FLAG_GATES.md"


@dataclass(frozen=True)
class FlagGate:
    """One flag's governance row: its settings name, its gate and a one-line effect.

    The default value is not stored here; it is read from live settings when rendering or verifying.
    """

    name: str  # the exact PythiaSettings field name
    gate: str  # the named gate that must clear before this flag may be flipped in a serving env
    effect: str  # one-line effect when ON


# The registry: one row per answer-quality flag, naming the gate that governs enabling it live. Rows
# are grouped by feature family for the rendered doc. Other flags (temporal, fusion, auto-ingestion,
# discovery, clause read, provider/model/pool settings) are deliberately excluded: they have their own
# rollout gates (retrieval cutover, operator deploy) outside the verification-loop protocol.
_REGISTRY: tuple[FlagGate, ...] = (
    # Deterministic retrieval robustness (LLM-free; never affects refusal, trust or ranking semantics).
    FlagGate(
        "seed_token_idf_enabled",
        "gate #3 (retrieval A/B on gold)",
        "rank query terms by inverse-doc-frequency (keep top-14 discriminative) instead of the first-14",
    ),
    FlagGate(
        "seed_pool_scale_enabled",
        "gate #3 (retrieval A/B on gold)",
        "let a multi-aspect decompose widen the rerank shortlist beyond the fixed _RERANK_POOL=16",
    ),
    FlagGate(
        "retrieval_concurrent_legs_enabled",
        "gate #3 (retrieval A/B on gold) — latency-only, RRF order-invariant",
        "run the per-sub-query retrieve legs concurrently (asyncio.gather); merged set/scores unchanged",
    ),
    FlagGate(
        "retrieval_quotas_enabled",
        "gate #3 (retrieval A/B on gold)",
        "per-leg quota + leg-scaled effective-k so a multi-aspect question is not starved by global k=12",
    ),
    # Term normalization and the soft-law crowding cap.
    FlagGate(
        "term_norm_enabled",
        "gate #1 (lawyer-review of data/term_aliases.json layman→statutory lexicon)",
        "add a normalized statutory-term sparse leg + defining-provision seeds for a layman query (add-never-gate)",
    ),
    FlagGate(
        "softlaw_quota_enabled",
        "gate #3 (retrieval A/B on gold) — demote-only, binding never dropped",
        "cap SOFT_LAW at ceil(k/3) in the top-k, demoting excess soft-law below binding statute",
    ),
    # Unified intake call (the recall lever for vague queries).
    FlagGate(
        "intake_v2_enabled",
        "gate #3 (A2 recall A/B) + gate #1 (intake prompt lawyer-review)",
        "replace _decompose with a richer intake call (2–6 statute-register sub-Qs + optional HyDE + facts)",
    ),
    FlagGate(
        "intake_det_scan_enabled",
        "gate #1 (lawyer-review of pythia/data/intake_lexicon.json)",
        "deterministic pre-pass adds structured intake facts from a reviewable lexicon (add-never-override)",
    ),
    FlagGate(
        "intake_clarify_enabled",
        "gate #3 (clarify precision on gold) + gate #1 (clarify copy lawyer-review)",
        "engine-side stage-2 clarify short-circuit when intake non-sufficient AND retrieval below cos-floor",
    ),
    # Wider context windows and the seven-slot structured answer.
    FlagGate(
        "ctx_window_enabled",
        "gate #3 (deadline-recall A/B) — read-only, served grain unchanged",
        "add deterministic ±250-char fact/term windows to the LLM ctx + faithfulness pass (not the served text)",
    ),
    FlagGate(
        "ctx_clause_snap_enabled",
        "gate #3 (deadline-recall A/B) — child of ctx_window, read-only",
        "snap each widened ctx window to the smallest enclosing provision_clause span (degrade-safe if absent)",
    ),
    FlagGate(
        "structured_answer_enabled",
        "gate #4 (G14 lawyer sign-off on the 7-slot Czech DRAFT copy)",
        "replace the 3-heading essay with the fixed 7-slot Czech action plan (populates .practical)",
    ),
    FlagGate(
        "structured_plan_llm_enabled",
        "gate #4 (G14) — sub-flag of structured_answer",
        "one cheap-LLM call MAY refine slot placement (validated: refines, never drops/invents)",
    ),
    FlagGate(
        "draft_planner_enabled",
        "gate #3 (equivalence-pinned, lossless) — MP-C↔MP-D seam",
        "lift sections into a typed DraftPlan on audit_trace (pure; served answer unchanged)",
    ),
    # Evidence bundle and citation resolver.
    FlagGate(
        "evidence_bundle_enabled",
        "gate #3 (V-CIT grounding parity on gold) — read-only, one canonical carrier",
        "build one canonical full-text carrier once so V-CIT/practical/sections agree on the backing text",
    ),
    # Question gate (the core anti-hallucination check).
    FlagGate(
        "metagate_enabled",
        "gate #4 (AC-METAGATE green) — flip stays SHADOW; enforce needs G14 + EU-AI-Act",
        "classify a question on intent/coverage/premise before GENERATE (shadow: classify+log only)",
    ),
    FlagGate(
        "metagate_true_cosine_enabled",
        "gate #4 (CP4 lawyer-gold + CP5 threshold calibration)",
        "feed the true bge-m3 cosine into the coverage 'strong' arm so strong_cos actually gates",
    ),
    # Claim-verification loop (atomize, run typed verifiers, record a ledger).
    FlagGate(
        "claim_verify_enabled",
        "gate #4 (VL-G flip-gate protocol green) — flip stays SHADOW; enforce is MP-E/V2",
        "master gate: atomize + verify + attach the claim ledger (shadow: audit annotation, output unchanged)",
    ),
    FlagGate(
        "claim_typing_llm_enabled",
        "gate #4 — sub-flag of claim_verify (reserved; inert unless master ON)",
        "optional cheap-LLM claim-typing assist (re-types only bare-slot-default claims)",
    ),
    FlagGate(
        "claim_entailment_llm_enabled",
        "gate #4 — sub-flag of claim_verify (reserved; inert unless master ON)",
        "optional cheap-LLM per-claim entailment support check inside V-CIT",
    ),
    FlagGate(
        "claim_calc_worked_example_enabled",
        "gate #4 (LAWYER-gated) — sub-flag of claim_verify",
        "sanctioned worked-example emission for V-CALC (the only computation lane)",
    ),
    FlagGate(
        "claim_verify_gap_probe_enabled",
        "gate #4 — sub-flag of claim_verify (verification-only, inert unless master ON)",
        "for an ungrounded cite, probe whether the § exists in-corpus (missed_in_corpus vs not_in_corpus telemetry)",
    ),
    FlagGate(
        "claim_verify_promote_enabled",
        "gate #2 (lawyer sign-off) — FLIPS LAST; sub-flag of claim_verify + gap_probe",
        "append (never displace) a missed_in_corpus provision to the verify evidence + re-verify (labelled needs_review)",
    ),
    # Repair loop, verified-only assembly and refusal.
    FlagGate(
        "claim_repair_strip_enabled",
        "gate #4 (MP-E enforce) — inert unless claim_verify enforce",
        "strip-the-unverifiable-half deterministic repair (else whole-sentence demotion)",
    ),
    FlagGate(
        "claim_repair_llm_enabled",
        "gate #4 (MP-E enforce) — inert unless claim_verify enforce",
        "targeted per-claim LLM prose repair lane (re-cite/reframe/demote only; WALL-0 fenced + re-verified)",
    ),
    # Observe-only latency instrumentation and quality tripwires.
    FlagGate(
        "stage_latency_enabled",
        "gate #3 (observe-only) — never enforces a budget",
        "stamp per-stage wall-clock and surface metrics()['stage_latency_ms'] (measurement only)",
    ),
    FlagGate(
        "quality_tripwires_enabled",
        "gate #3 (observe-only) — never raises/demotes/refuses",
        "run the pure tripwire evaluator over the assembled answer's signals + emit telemetry (monitoring only)",
    ),
)

# The ``*_mode`` flags, each paired with a master ``*_enabled`` flag and inert while the master is off.
# The safe default is listed first. They are recorded so the doc lists the mode axis and so the drift
# check can enumerate them separately from the boolean flags.
_MODE_FLAGS: tuple[tuple[str, str, str, str], ...] = (
    # (name, master, safe_default, gate)
    (
        "metagate_mode",
        "metagate_enabled",
        "shadow",
        "gate #4 (AC-METAGATE green + G14 + EU-AI-Act) — 'enforce' routes non-sound branches; stays 'shadow'",
    ),
    (
        "claim_verify_mode",
        "claim_verify_enabled",
        "shadow",
        "gate #4 (VL-G protocol) — 'enforce' reassembles from verified claims (MP-E/V2); stays 'shadow' (T-LOCAL: enforce allowed LOCALLY only)",
    ),
    (
        "intake_v2_mode",
        "intake_v2_enabled",
        "shadow",
        "gate #3 (A2 recall A/B) — 'active' feeds intake sub-Qs into the RRF loop; 'shadow' logs only",
    ),
)

# Sub-flag to master, for the log-only boot validator. A sub-flag that is on while its master is off is
# inert (the code never reaches it), but it is almost always an operator mistake, so the validator
# warns; it never raises or changes behaviour.
SUBFLAG_MASTERS: dict[str, str] = {
    # Claim-verify family (all inert unless claim_verify_enabled is on).
    "claim_typing_llm_enabled": "claim_verify_enabled",
    "claim_entailment_llm_enabled": "claim_verify_enabled",
    "claim_calc_worked_example_enabled": "claim_verify_enabled",
    "claim_verify_gap_probe_enabled": "claim_verify_enabled",
    "claim_repair_strip_enabled": "claim_verify_enabled",
    "claim_repair_llm_enabled": "claim_verify_enabled",
    # Promotion is doubly gated: it also needs the gap probe, whose findings are its input.
    "claim_verify_promote_enabled": "claim_verify_gap_probe_enabled",
    # Structured-answer family.
    "structured_plan_llm_enabled": "structured_answer_enabled",
    # Context-window family.
    "ctx_clause_snap_enabled": "ctx_window_enabled",
    # Intake family.
    "intake_det_scan_enabled": "intake_v2_enabled",
    "intake_clarify_enabled": "intake_v2_enabled",
    "metagate_true_cosine_enabled": "metagate_enabled",
}


# Recorded deviations and reconciliations, rendered into the doc so the governance record is complete.
DEVIATIONS: tuple[str, ...] = (
    "FLAG-NAME RECONCILIATION — the registry rows use the REAL PythiaSettings field names. Any contract/plan "
    "'phantom' names (e.g. a bare `claim_verify` or `structured_plan` in prose) map to the concrete "
    "`*_enabled` field here; the lockstep drift check pins that no phantom leaks in and no real flag is missing.",
    "intake_hyde_weight = 0.5 (DEVIATION from the contract's 0.8). The reduced-weight internal HyDE legs run "
    "at 0.5; the 0.8 value is REVISITED at the step-88 intake weight sweep, not before.",
    "T-LOCAL TEST-POSTURE AUTHORIZATION — claim_verify 'enforce' is AUTHORIZED for LOCAL runs only "
    "(run_aq_all.sh sets PYTHIA_CLAIM_VERIFY_MODE=enforce). metagate STAYS SHADOW everywhere (AC-METAGATE RED); "
    "enforce is a documented future step behind G14 + EU-AI-Act, never a local test flip.",
    "R5 STANDING BREACH (honestly recorded) — the MP-C AQ flags (intake_v2 active, structured_answer, "
    "claim_verify, metagate shadow, ctx_window, evidence_bundle, term_norm, softlaw_quota, draft_planner, "
    "quality_tripwires, stage_latency) are set ON in the UNCOMMITTED local run scripts (run_aq_live.sh / "
    "run_aq_staged.sh / run_aq_all.sh). That is a RECORDED OPERATOR TEST POSTURE pending gate #4 — NOT a "
    "committed serving default. `main`/committed config remains 100% default-OFF (pinned by the byte-identical "
    "guard tests); the registry governs the serving FLIP, which has not happened.",
)


def _defaults() -> dict[str, object]:
    """Return the default of every registered flag and mode, read from a fresh PythiaSettings."""
    s = PythiaSettings()
    return {f.name: getattr(s, f.name) for f in _REGISTRY} | {m[0]: getattr(s, m[0]) for m in _MODE_FLAGS}


def registry_flag_names() -> set[str]:
    """Return the set of flag and mode names the registry governs."""
    return {f.name for f in _REGISTRY} | {m[0] for m in _MODE_FLAGS}


def settings_aq_flag_names() -> set[str]:
    """Return the answer-quality flag and mode names found on PythiaSettings by pattern.

    This is the other side of the drift check. An AQ flag is a boolean ``*_enabled`` field, or a
    ``*_mode`` field paired with one, excluding the non-AQ families (temporal, fusion,
    auto-ingestion, discovery, clause read). The exclusion list is explicit, so a new non-AQ
    ``*_enabled`` field has to be added here deliberately, as a reviewed decision; that closes the
    drift hole from both sides.
    """
    non_aq_prefixes = (
        "temporal_",
        "auto_ingest",
    )
    non_aq_exact = {
        "clause_read_enabled",
        "discovery_enabled",
        "gnn_staleness_check_enabled",
        "graph_changed_monitor_enabled",
        "version_diff_enabled",
        "fusion_enabled",
        "answer_path_fusion_enabled",
        "seed_rerank_enabled",  # memory toggle for small hosts (default on), not a verification-loop flag
    }
    out: set[str] = set()
    for name, field in PythiaSettings.model_fields.items():
        if name in non_aq_exact or any(name.startswith(p) for p in non_aq_prefixes):
            continue
        if name.endswith("_enabled") and field.annotation is bool:
            out.add(name)
    # Mode fields are strings, so detect them by their master/mode pairing instead of by pattern.
    for mode_name, master, _default, _gate in _MODE_FLAGS:
        if master in out:
            out.add(mode_name)
    return out


def registry_vs_settings_drift() -> dict[str, list[str]]:
    """Return the drift between the registry and live settings.

    ``missing_in_registry`` lists settings flags without a registry row (the dangerous, ungoverned
    case); ``stale_in_registry`` lists registry rows whose field no longer exists (a leftover from a
    rename). Two empty lists mean no drift. Pure.
    """
    reg = registry_flag_names()
    live = settings_aq_flag_names()
    settings_fields = set(PythiaSettings.model_fields)
    return {
        "missing_in_registry": sorted(live - reg),
        "stale_in_registry": sorted(n for n in reg if n not in settings_fields),
    }


def render_markdown() -> str:
    """Render ``docs/FLAG_GATES.md`` from the registry and the live settings defaults.

    Deterministic (defaults are read once), so the committed file is stable: the test regenerates it
    in memory and asserts it equals the committed file, so a flag added or renamed without
    regenerating the doc fails CI.
    """
    defaults = _defaults()
    lines: list[str] = []
    lines.append("# Verification Loop Flip Gates — ENGINE (`pythia`)")
    lines.append("")
    lines.append(
        "GENERATED by `pythia/flag_registry.py::render_markdown` from the live `PythiaSettings` — "
        "**do NOT hand-edit**. Regenerate with `python -m pythia.flag_registry --write`. Every AQ-era "
        "(WS4–WS8) engine flag appears here with its DEFAULT, the named GATE that must clear before a "
        "serving-env flip, and its one-line effect. `tests/test_flip_gate_registry.py` fails on any drift "
        "(a Settings flag with no registry row, or a stale row) or on a stale committed copy of this file."
    )
    lines.append("")
    lines.append("## Boolean flip flags")
    lines.append("")
    lines.append("| Flag | Default | Named gate (must clear before flip) | Effect when ON |")
    lines.append("| --- | --- | --- | --- |")
    for f in _REGISTRY:
        lines.append(f"| `{f.name}` | `{defaults[f.name]!r}` | {f.gate} | {f.effect} |")
    lines.append("")
    lines.append("## Mode flags (paired with a master `*_enabled`; inert while the master is OFF)")
    lines.append("")
    lines.append("| Mode flag | Default | Master | Named gate | Note |")
    lines.append("| --- | --- | --- | --- | --- |")
    for name, master, _safe, gate in _MODE_FLAGS:
        lines.append(f"| `{name}` | `{defaults[name]!r}` | `{master}` | {gate} | safe default is the first value |")
    lines.append("")
    lines.append(
        "## Sub-flag → master (log-only boot validator: a sub-flag ON with its master OFF is warned, never raised)"
    )
    lines.append("")
    lines.append("| Sub-flag | Master |")
    lines.append("| --- | --- |")
    for sub, master in sorted(SUBFLAG_MASTERS.items()):
        lines.append(f"| `{sub}` | `{master}` |")
    lines.append("")
    lines.append("## Recorded deviations / reconciliations")
    lines.append("")
    for d in DEVIATIONS:
        lines.append(f"- {d}")
    lines.append("")
    return "\n".join(lines)


def subflag_without_master_warnings(s: PythiaSettings | None = None) -> list[str]:
    """Return one warning per sub-flag that is on while its master is off.

    Log-only: reads settings, never raises and never changes behaviour (such a sub-flag is already
    inert because the code never reaches it). Boot wiring logs each returned string at WARNING so
    the misconfiguration is visible, then continues. Returns an empty list at the safe defaults.
    """
    s = s or PythiaSettings()
    out: list[str] = []
    for sub, master in sorted(SUBFLAG_MASTERS.items()):
        if bool(getattr(s, sub, False)) and not bool(getattr(s, master, False)):
            out.append(
                f"flip-gate: sub-flag {sub!r} is ON but its master {master!r} is OFF — the sub-flag is INERT "
                f"(the code never reaches it). Set {master.upper()}=1 to make {sub!r} effective, or unset {sub!r}."
            )
    return out


def run_subflag_validator(s: PythiaSettings | None = None) -> list[str]:
    """Log each sub-flag-without-master warning at WARNING level and return the list (boot hook).

    Never raises and never changes behaviour. At the safe defaults it logs nothing and returns [].
    """
    warnings = subflag_without_master_warnings(s)
    for w in warnings:
        _log.warning(w)
    return warnings


def _write_doc() -> str:
    """Write the generated markdown to the doc file and return its path.

    Used by the ``--write`` CLI. The drift test never writes; it compares in memory.
    """
    from pathlib import Path

    # This file lives in <engine>/pythia/; the doc path is relative to <engine>.

    doc = Path(__file__).resolve().parent.parent / REGISTRY_DOC_NAME
    doc.write_text(render_markdown(), encoding="utf-8")
    return str(doc)


if __name__ == "__main__":  # pragma: no cover - CLI convenience
    import sys

    if "--write" in sys.argv:
        print(f"wrote {_write_doc()}")
    else:
        print(render_markdown())
