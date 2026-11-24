"""AC-METAGATE harness SMOKE test (Phase-C M4) — NON-LIVE, CI-runnable.

Proves the ACCEPTANCE-HARNESS ARITHMETIC (tests/metagate_eval/run_acceptance.py) without a live engine, LLM,
or corpus. It exercises:
  • the PURE scoring math (`score_rows`) over a tiny inline fixture with classify_meta MOCKED via scripted
    `Scored` rows — that it correctly computes fabrication_rate (catches an expected-not_in_corpus row that the
    mock routed to "sound"), sound_recall, per-route precision/recall, the confusion matrix, the crash count,
    and the overall PASS/FAIL against the two AC-METAGATE constraints;
  • the eval-set loader (comment-skipping + validation) against the REAL shipped eval_set.jsonl;
  • the fail-closed probe — that with the real classify_meta wired to an LLM that always raises, the probe
    detects neither a fabricated "sound" nor a crash (the safety guarantee), and that the probe FLAGS a
    deliberately-broken classifier that fabricates / crashes.

It touches NO DB and starts NO server: `score_rows`/`format_report`/`load_eval_set` are pure, and the
fail-closed probe drives the REAL pure metagate code with a fake LLM (the same shape tests/test_metagate.py
uses). This is the proof that the gate's arithmetic is correct before the operator ever runs it live.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# import the harness as a module (it lives outside the package, alongside the eval set). It must be
# registered in sys.modules BEFORE exec_module so the @dataclass machinery can resolve cls.__module__.
import importlib.util
import sys

_HARNESS_PATH = Path(__file__).parent / "metagate_eval" / "run_acceptance.py"
_spec = importlib.util.spec_from_file_location("metagate_run_acceptance", _HARNESS_PATH)
assert _spec is not None and _spec.loader is not None
ha = importlib.util.module_from_spec(_spec)
sys.modules["metagate_run_acceptance"] = ha
_spec.loader.exec_module(ha)

EvalRow, Scored = ha.EvalRow, ha.Scored


def _row(route: str, q: str = "q") -> EvalRow:
    return EvalRow(query=q, expected_route=route, note="")


# ════════════════════════════════════════════════════════════════════════════════════════════════════════
# 1) the loader: skips comments, validates, parses the REAL shipped eval set
# ════════════════════════════════════════════════════════════════════════════════════════════════════════
def test_loader_parses_shipped_eval_set() -> None:
    rows = ha.load_eval_set()
    # the set is balanced across the four routes; a healthy seed has a meaningful count in each.
    by_route: dict[str, int] = {r: 0 for r in ha.ROUTES}
    for row in rows:
        by_route[row.expected_route] += 1
    assert len(rows) >= 120, f"seed set should be ~120-160 rows, got {len(rows)}"
    for r in ha.ROUTES:
        assert by_route[r] >= 20, f"route {r} under-represented: {by_route[r]} rows"
    # every parsed row has a non-empty query and a valid route (the loader would have raised otherwise).
    assert all(row.query and row.expected_route in ha.ROUTES for row in rows)


def test_loader_skips_comments_and_validates(tmp_path: Path) -> None:
    p = tmp_path / "tiny.jsonl"
    p.write_text(
        "// a comment line\n"
        "\n"
        '{"query": "Jaké povinnosti má povinná osoba?", "expected_route": "sound", "note": "ok"}\n'
        "// another comment\n"
        '{"query": "recept na guláš", "expected_route": "not_a_legal_question"}\n',
        encoding="utf-8",
    )
    rows = ha.load_eval_set(p)
    assert [r.expected_route for r in rows] == ["sound", "not_a_legal_question"]

    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"query": "x", "expected_route": "totally_bogus"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        ha.load_eval_set(bad)


# ════════════════════════════════════════════════════════════════════════════════════════════════════════
# 2) the SCORING math over a tiny inline fixture (classify_meta MOCKED via scripted Scored rows)
# ════════════════════════════════════════════════════════════════════════════════════════════════════════
def test_score_rows_catches_a_fabrication() -> None:
    """The HARD constraint. A row labelled not_in_corpus that the (mocked) gateway routed to "sound" is a
    fabrication → fabrication_rate > 0 → the gate FAILS even if everything else is perfect."""
    scored = [
        Scored(_row("sound", "a"), actual_route="sound"),
        Scored(_row("sound", "b"), actual_route="sound"),
        Scored(_row("not_in_corpus", "c"), actual_route="not_in_corpus"),
        # the fabrication: a not_in_corpus question the mock routed to sound.
        Scored(_row("not_in_corpus", "d"), actual_route="sound"),
        Scored(_row("not_a_legal_question", "e"), actual_route="not_a_legal_question"),
        Scored(_row("false_premise", "f"), actual_route="false_premise"),
    ]
    m = ha.score_rows(scored)
    # 4 non-sound rows, 1 of them routed to sound → fabrication_rate = 1/4.
    assert m.fabrication_rate == pytest.approx(0.25)
    assert len(m.fabrications) == 1
    assert m.fabrications[0].row.query == "d"
    assert m.passed is False, "any fabrication must FAIL the gate"


def test_score_rows_zero_fabrication_high_recall_passes() -> None:
    """No fabrication + full sound-recall + no crash → the gate PASSES."""
    scored = [
        Scored(_row("sound", "a"), actual_route="sound"),
        Scored(_row("sound", "b"), actual_route="sound"),
        Scored(_row("sound", "c"), actual_route="sound"),
        Scored(_row("not_in_corpus", "d"), actual_route="not_in_corpus"),
        Scored(_row("not_a_legal_question", "e"), actual_route="not_a_legal_question"),
        Scored(_row("false_premise", "f"), actual_route="false_premise"),
    ]
    m = ha.score_rows(scored)
    assert m.fabrication_rate == 0.0
    assert m.sound_recall == 1.0
    assert m.crashes == 0
    assert m.passed is True


def test_score_rows_sound_recall_below_floor_fails() -> None:
    """No fabrication, but a sound row that routed to not_in_corpus drags sound_recall below 0.95 → FAIL.
    (Routing a sound question elsewhere is over-refusal — safe, but it must still fail the recall bar.)"""
    # 3 sound rows, only 2 routed to sound → recall = 0.667 < 0.95.
    scored = [
        Scored(_row("sound", "a"), actual_route="sound"),
        Scored(_row("sound", "b"), actual_route="sound"),
        Scored(_row("sound", "c"), actual_route="not_in_corpus"),  # over-refusal (no fabrication)
        Scored(_row("not_in_corpus", "d"), actual_route="not_in_corpus"),
    ]
    m = ha.score_rows(scored)
    assert m.fabrication_rate == 0.0, "over-refusal is not a fabrication"
    assert m.sound_recall == pytest.approx(2 / 3, abs=1e-4)  # score_rows rounds to 4 dp
    assert m.passed is False, "sound_recall below 0.95 must FAIL the gate"


def test_score_rows_counts_crashes_and_confusion_matrix() -> None:
    """A row whose classification crashed (actual_route=None) counts as a crash (failing the never-crash bar)
    and lands in the confusion matrix's __error__ column, never in a real route."""
    scored = [
        Scored(_row("sound", "a"), actual_route="sound"),
        Scored(_row("sound", "b"), actual_route=None, error="DBError: connection refused"),  # crash
        Scored(_row("not_in_corpus", "c"), actual_route="not_in_corpus"),
        Scored(_row("false_premise", "d"), actual_route="false_premise"),
    ]
    m = ha.score_rows(scored)
    assert m.crashes == 1
    assert m.passed is False, "any crash fails the never-crash bar"
    # the crashed sound row lands in the __error__ bucket, not under any route.
    assert m.confusion["sound"]["__error__"] == 1
    assert m.confusion["sound"]["sound"] == 1
    assert m.confusion["not_in_corpus"]["not_in_corpus"] == 1
    # per-route precision/recall: not_in_corpus is perfect; sound recall is 1/2 (1 hit, 1 crash).
    assert m.per_route["not_in_corpus"]["precision"] == 1.0
    assert m.per_route["sound"]["recall"] == pytest.approx(0.5)


def test_format_report_renders_verdict_and_matrix() -> None:
    """The pretty-printer is pure and produces a readable PASS/FAIL block + the confusion matrix header."""
    scored = [
        Scored(_row("sound", "a"), actual_route="sound"),
        Scored(_row("not_in_corpus", "b"), actual_route="sound"),  # a fabrication → FAIL
    ]
    m = ha.score_rows(scored)
    text = ha.format_report(m, show_misses=True, scored=scored)
    assert "fabrication_rate" in text
    assert "sound_recall" in text
    assert "confusion matrix" in text
    assert "AC-METAGATE: FAIL" in text
    assert "misclassifications" in text  # show_misses listed the fabricated row


# ════════════════════════════════════════════════════════════════════════════════════════════════════════
# 3) the FAIL-CLOSED probe drives the REAL pure metagate code with a forced-error LLM
# ════════════════════════════════════════════════════════════════════════════════════════════════════════
async def test_fail_closed_probe_passes_on_real_metagate() -> None:
    """With the real classify_meta + an LLM that always raises, the probe must find NO fabricated 'sound' and
    NO crash — the structural safety guarantee. Uses no DB (the probe feeds a synthetic retrieved set)."""
    rows = [_row("sound", "Mám nějakou povinnost ohledně toho, co jsem výše popsal?")]
    ok, problems = await ha.fail_closed_probe(rows, sample=6)
    assert ok, f"fail-closed probe unexpectedly reported problems: {problems}"
    assert problems == []


async def test_fail_closed_probe_detects_a_broken_classifier(monkeypatch) -> None:
    """Sanity: the probe is not vacuously green. Monkeypatch classify_meta inside the harness so it FABRICATES
    a 'sound' verdict under the forced LLM error → the probe must flag it. This proves the probe's detector
    arithmetic (it would catch a regression that removed the fail-closed guards)."""
    from pythia.contract import MetaVerdict

    async def _fabricating_classify(query, decomp, meta, ordered, rrf, raw):
        return MetaVerdict(route="sound", reason="(broken: ignored the LLM error)")

    monkeypatch.setattr("pythia.reasoning.metagate.classify_meta", _fabricating_classify, raising=True)
    ok, problems = await ha.fail_closed_probe([_row("not_in_corpus", "q")], sample=4)
    assert ok is False
    assert any("FABRICATED" in p for p in problems), problems


async def test_fail_closed_probe_detects_a_crashing_classifier(monkeypatch) -> None:
    """The other half: a classifier that RAISES (rather than failing closed) must be caught as a crash by the
    probe — proving the never-crash bar's detection works."""

    async def _crashing_classify(query, decomp, meta, ordered, rrf, raw):
        raise RuntimeError("classifier blew up instead of failing closed")

    monkeypatch.setattr("pythia.reasoning.metagate.classify_meta", _crashing_classify, raising=True)
    ok, problems = await ha.fail_closed_probe([_row("sound", "q")], sample=4)
    assert ok is False
    assert any("CRASH" in p for p in problems), problems
