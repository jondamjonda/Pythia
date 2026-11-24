"""VL-G-10 — the mechanized flip-gate registry guards (ENGINE side).

Four independent guards over ``pythia/flag_registry.py``, all DB-free / pure / no-live:

  (b) LOCKSTEP — the committed ``docs/FLAG_GATES.md`` MUST equal the doc regenerated in memory
      from the live ``PythiaSettings``. A flag added/renamed without regenerating the doc fails here.

  (d) SUB-FLAG-WITHOUT-MASTER VALIDATOR — the log-only boot check warns (never raises, never changes
      behaviour) when a sub-flag is ON while its master is OFF.

  (e) METAGATE GREP-GUARD — NO loop/clarify/claim module (intake / det_intake / intake_verify / loop_budget /
      loop_stages / claims / claim_verify / claim_repair / claim_ledger / claim_eval / planner / draft_plan)
      READS a metagate verdict. Contract rule 5: the metagate stays advisory; the loop never consumes its
      verdict. ``answer.py`` is the SOLE sanctioned consumer.

  (f) DRIFT — the registry and the live Settings enumerate the SAME AQ flag set (no ungoverned flag, no stale
      registry row).
"""

from __future__ import annotations

import re
from pathlib import Path

from pythia.config import PythiaSettings
from pythia.flag_registry import (
    REGISTRY_DOC_NAME,
    SUBFLAG_MASTERS,
    registry_flag_names,
    registry_vs_settings_drift,
    render_markdown,
    settings_aq_flag_names,
    subflag_without_master_warnings,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent  # Build/
_REASONING = _REPO_ROOT / "pythia" / "reasoning"


# ===========================================================================
# (b) LOCKSTEP — committed doc == freshly generated doc.
# ===========================================================================
def test_committed_registry_doc_matches_generated():
    """The committed `docs/FLAG_GATES.md` must be byte-identical to `render_markdown()`.

    Regenerate with `python -m pythia.flag_registry --write` after any AQ flag change."""
    doc = _REPO_ROOT / REGISTRY_DOC_NAME
    assert doc.exists(), f"missing committed registry doc: {doc}"
    committed = doc.read_text(encoding="utf-8")
    generated = render_markdown()
    assert committed == generated, (
        "The committed 'docs/FLAG_GATES.md' is STALE vs the live PythiaSettings. "
        "Regenerate it: `python -m pythia.flag_registry --write`."
    )


# ===========================================================================
# (f) DRIFT — registry and Settings enumerate the same AQ flag set.
# ===========================================================================
def test_no_registry_settings_drift():
    drift = registry_vs_settings_drift()
    assert drift["missing_in_registry"] == [], (
        "AQ flag(s) on PythiaSettings with NO flip-gate registry row (ungoverned): "
        f"{drift['missing_in_registry']}. Add a FlagGate row in pythia/flag_registry.py."
    )
    assert drift["stale_in_registry"] == [], (
        "Registry row(s) whose PythiaSettings field no longer exists (rename left behind): "
        f"{drift['stale_in_registry']}."
    )


def test_every_aq_flag_has_a_named_gate():
    """Every registered boolean flag AND mode flag carries a non-empty named gate."""
    from pythia.flag_registry import _MODE_FLAGS, _REGISTRY

    for f in _REGISTRY:
        assert f.gate.strip(), f"flag {f.name!r} has no named gate"
        assert f.effect.strip(), f"flag {f.name!r} has no effect line"
    for name, _master, _safe, gate in _MODE_FLAGS:
        assert gate.strip(), f"mode flag {name!r} has no named gate"


def test_registry_and_settings_counts_agree():
    """Sanity: the two enumeration sides agree in size (the drift check proves set equality)."""
    assert registry_flag_names() == settings_aq_flag_names()


# ===========================================================================
# (d) SUB-FLAG-WITHOUT-MASTER VALIDATOR — log-only, never raises.
# ===========================================================================
def test_subflag_validator_silent_at_defaults():
    """At safe defaults (every flag OFF) the validator returns no warnings."""
    assert subflag_without_master_warnings(PythiaSettings()) == []


def test_subflag_validator_fires_when_subflag_on_master_off():
    """A sub-flag ON with its master OFF yields exactly one warning naming both flags."""
    from types import SimpleNamespace

    base = {name: False for name in SUBFLAG_MASTERS}
    base.update({master: False for master in SUBFLAG_MASTERS.values()})
    # gap_probe ON, its master claim_verify_enabled OFF.
    base["claim_verify_gap_probe_enabled"] = True
    warnings = subflag_without_master_warnings(SimpleNamespace(**base))
    assert any("claim_verify_gap_probe_enabled" in w and "claim_verify_enabled" in w for w in warnings)


def test_subflag_validator_silent_when_master_on():
    """Sub-flag ON AND master ON ⇒ no warning (the effective, intended combo)."""
    from types import SimpleNamespace

    base = {name: False for name in SUBFLAG_MASTERS}
    base.update({master: False for master in SUBFLAG_MASTERS.values()})
    base["structured_answer_enabled"] = True
    base["structured_plan_llm_enabled"] = True
    warnings = subflag_without_master_warnings(SimpleNamespace(**base))
    assert warnings == []


def test_run_subflag_validator_is_log_only(caplog):
    """run_subflag_validator emits at WARNING and returns the list — never raises."""
    import logging
    from types import SimpleNamespace

    from pythia.flag_registry import run_subflag_validator

    base = {name: False for name in SUBFLAG_MASTERS}
    base.update({master: False for master in SUBFLAG_MASTERS.values()})
    base["ctx_clause_snap_enabled"] = True  # master ctx_window_enabled OFF
    with caplog.at_level(logging.WARNING, logger="pythia.flag_registry"):
        out = run_subflag_validator(SimpleNamespace(**base))
    assert any("ctx_clause_snap_enabled" in w for w in out)
    assert any("ctx_clause_snap_enabled" in r.message for r in caplog.records)


def test_subflag_masters_reference_real_fields():
    """Every sub-flag and master named in SUBFLAG_MASTERS is a real PythiaSettings field."""
    fields = set(PythiaSettings.model_fields)
    for sub, master in SUBFLAG_MASTERS.items():
        assert sub in fields, f"SUBFLAG_MASTERS sub {sub!r} is not a Settings field"
        assert master in fields, f"SUBFLAG_MASTERS master {master!r} is not a Settings field"


# ===========================================================================
# (e) METAGATE GREP-GUARD — no loop/clarify/claim module reads a metagate verdict.
# ===========================================================================
# The modules that form the answer LOOP / clarify / claim-verify family. answer.py is DELIBERATELY excluded —
# it is the ONE sanctioned metagate consumer (it calls classify_meta + routes on meta_verdict.route).
_LOOP_MODULES = (
    "intake.py",
    "det_intake.py",
    "intake_verify.py",
    "loop_budget.py",
    "loop_stages.py",
    "claims.py",
    "claim_verify.py",
    "claim_repair.py",
    "claim_ledger.py",
    "claim_eval.py",
    "planner.py",
    "draft_plan.py",
)

# Verdict-CONSUMPTION tokens. These indicate a module is READING a metagate verdict / routing on it — the
# thing contract rule 5 forbids in the loop. Chosen to catch consumption while NOT flagging the legitimate
# "metagate idiom" mentions in comments/docstrings or the STAGE_METAGATE stage-NAME constant.
_VERDICT_TOKENS = (
    "meta_verdict",  # the verdict variable answer.py threads
    "classify_meta",  # the classifier call
    "MetaVerdict",  # the verdict type
    "_meta_answer",  # the enforce meta-answer builder
    "from pythia.reasoning.metagate",  # importing the metagate module
    "import metagate",  # importing the metagate module
    ".route",  # routing on a verdict's .route
)
# A line is only a real hit if the token is CODE, not inside a comment. We strip the comment tail and any
# string that is clearly a docstring line. (loop_stages defines STAGE_METAGATE = "metagate" — a string LABEL,
# not a verdict read — which none of the tokens above match, so it is correctly ignored.)
_COMMENT = re.compile(r"#.*$")


def _code_lines(text: str) -> list[str]:
    """Return source lines with the trailing ``# ...`` comment stripped, so a token that appears ONLY in a
    comment (the 'metagate idiom' notes) is not treated as consumption. Docstring-only mentions are handled by
    the token set: the docstrings in these modules say 'metagate'/'idiom', never a verdict token."""
    out: list[str] = []
    for raw in text.splitlines():
        out.append(_COMMENT.sub("", raw))
    return out


def test_no_loop_module_reads_a_metagate_verdict():
    """Contract rule 5: the metagate is ADVISORY — no loop/clarify/claim module may consume its verdict."""
    offenders: dict[str, list[str]] = {}
    for mod in _LOOP_MODULES:
        path = _REASONING / mod
        assert path.exists(), f"expected loop module missing: {path}"
        for line in _code_lines(path.read_text(encoding="utf-8")):
            for tok in _VERDICT_TOKENS:
                if tok in line:
                    offenders.setdefault(mod, []).append(f"{tok!r} in: {line.strip()}")
    assert not offenders, (
        "METAGATE INDEPENDENCE VIOLATED (contract rule 5) — a loop/clarify/claim module reads a metagate "
        f"verdict. The metagate must stay advisory; only answer.py consumes it. Offenders: {offenders}"
    )


def test_grep_guard_would_catch_a_real_consumption():
    """Meta-test: the token set + comment-stripping actually flags a CODE consumption but NOT a comment."""
    code = "    if meta_verdict.route == 'weak_coverage':\n"
    comment = "    # reuse the metagate idiom (WALL-0 fence); never read meta_verdict here\n"
    code_hit = any(tok in _code_lines(code)[0] for tok in _VERDICT_TOKENS)
    comment_hit = any(tok in _code_lines(comment)[0] for tok in _VERDICT_TOKENS)
    assert code_hit, "the guard must flag a real meta_verdict.route consumption"
    assert not comment_hit, "the guard must NOT flag a 'metagate' mention that lives in a comment"


def test_answer_py_is_still_the_sanctioned_consumer():
    """Guardrail on the guardrail: answer.py DOES read the verdict (so the guard is meaningfully scoping the
    LOOP modules, not vacuously passing because nobody reads metagate anywhere)."""
    answer = (_REASONING / "answer.py").read_text(encoding="utf-8")
    assert "meta_verdict" in answer and "classify_meta" in answer
