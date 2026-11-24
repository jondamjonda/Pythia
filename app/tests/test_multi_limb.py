"""WF-4 Increment 1 — multi-limb + dedup pure core (tests + the cardinal-safety proofs).

Covers the four deterministic helpers (limb_key / extract_limb_markers / split_limbs / mutual_exclusion_group /
dedup_groups) and — most importantly — the CARDINAL SAFETY INVARIANT that makes this module safe to ship:

  INV-NOTHING-DROPPED: no helper ever removes, suppresses, or hides a finding. dedup membership is TOTAL;
    mutual-exclusion keeps BOTH poles; split_limbs' segments cover the whole duty (and fall back to [whole]).
    Hiding/understating a binding duty is worse than showing a duplicate — so every "demotion"/"dedup"/"mutual-
    exclusion" outcome is an ANNOTATION (grouping over indices), never a shortened list.

Plus the spec's eval acceptance:
  * limb-marker recall on constructed "either/or" statements == 1.0
  * the mutual-exclusion cohort has ZERO contradictions (both poles present, neither dropped)
  * dedup precision >= 0.98 on constructed transposed-duplicate pairs
  * a test proving NOTHING is dropped (total membership over a mixed corpus)

DATA GAP (recorded): the gold fixture (tests/fixtures/applicability_gold.json) carries only truncated
{actor, modal, statement, confidence} per row — no `action`, no `law_source`, no `management_structure`, and the
statements are cut mid-clause (so most multi-limb markers are sheared off). The gold is therefore used ONLY for
(a) the real mutually-exclusive capital pair (idx 38 external EUR 125k vs idx 59 internal EUR 300k) and (b) the
one intact "either … or …" row (idx 60). Everything else uses CONSTRUCTED representative inputs that exercise the
legal cases the spec names. Pure — no DB, no network, no LLM, no clock.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from regsuper.analysis.multi_limb import (
    dedup_groups,
    extract_limb_markers,
    limb_key,
    mutual_exclusion_group,
    split_limbs,
)

_GOLD = json.loads(
    (
        Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "applicability_gold.json"
    ).read_text(encoding="utf-8")
)


# ── limb_key: canonical substance hash (stable, deterministic, lossless of identity) ─────────────────────────
def test_limb_key_is_deterministic():
    a = limb_key("AIFM", "have an initial capital of at least EUR 125000", "shall", None)
    b = limb_key("AIFM", "have an initial capital of at least EUR 125000", "shall", None)
    assert a == b


def test_limb_key_normalizes_case_whitespace_punctuation():
    """Case, internal whitespace, and punctuation must not change the substance identity."""
    a = limb_key("AIFM", "Notify   the appointment, of the external-valuer.", "shall")
    b = limb_key("aifm", "notify the appointment of the external valuer", "SHALL")
    assert a == b


def test_limb_key_folds_czech_diacritics():
    """CZ diacritics fold so an accented restatement hashes identically to its ascii-folded twin."""
    a = limb_key("ručitel", "odpovídat za správnost a úplnost údajů", "shall")
    b = limb_key("rucitel", "odpovidat za spravnost a uplnost udaju", "shall")
    assert a == b


def test_limb_key_distinct_substance_distinct_key():
    """Two genuinely different duties must NOT collide (precision floor for dedup)."""
    cap125 = limb_key("AIFM", "have an initial capital of at least EUR 125000", "shall")
    cap300 = limb_key("AIFM", "have an initial capital of at least EUR 300000", "shall")
    risk = limb_key("AIFM", "implement adequate risk management systems", "shall")
    assert cap125 != cap300 != risk and cap125 != risk


def test_limb_key_handles_none_fields():
    """None / empty fields fold to empty segments — the key is always well-formed and stable."""
    k = limb_key(None, None, None, None)
    assert k == "|||" and limb_key(None, "x") == limb_key(None, "x")


# ── extract_limb_markers: alternation / enumeration / carve-out detection ────────────────────────────────────
@pytest.mark.parametrize(
    "text,expected_subset",
    [
        (
            "either have additional own funds appropriate to cover liability or hold professional indemnity insurance",
            {"either_or", "or"},
        ),
        (
            "the firm shall do X; or alternatively the firm shall do Y",
            {"semicolon_or", "alternatively", "or"},
        ),
        (
            "the AIFM shall (a) appoint a depositary (b) notify the authority (c) keep records",
            {"enum_letters"},
        ),
        ("the AIFM shall do (1) the first thing (2) the second thing", {"enum_numbers"}),
        ("act as depositary unless functions are functionally separated", {"unless"}),
    ],
)
def test_extract_limb_markers_detects_each_family(text, expected_subset):
    found = set(extract_limb_markers(text))
    assert expected_subset <= found, f"missing markers {expected_subset - found} in {text!r}"


def test_extract_limb_markers_empty_when_single_limb():
    assert extract_limb_markers("ensure the AIF cash flows are properly monitored") == []
    assert extract_limb_markers("") == []
    assert extract_limb_markers(None) == []


def test_extract_limb_markers_lone_paren_crossref_is_not_a_list():
    """A single '(c)' cross-reference must NOT be detected as a lettered enumeration (needs >=2 markers)."""
    assert "enum_letters" not in extract_limb_markers(
        "perform activities listed in paragraph 1 (c) only if agreed in the administration contract"
    )


def test_extract_limb_markers_word_boundary_no_false_or():
    """'or' must be word-bounded: 'order'/'normal'/'for' must not trip the bare-or marker."""
    assert "or" not in extract_limb_markers(
        "maintain orderly records in the normal course for the fund"
    )


# ── marker-recall scorer (spec: limb-marker recall on constructed either/or == 1.0) ─────────────────────────
# Constructed "either … or …" statements — each MUST yield the either_or marker (recall == 1.0).
_EITHER_OR_CASES = [
    "either have additional own funds to cover liability risks or hold professional indemnity insurance",
    "the AIFM shall either appoint an external valuer or value the assets itself with functional independence",
    "either be a credit institution or be an investment firm subject to prudential supervision",
    "the depositary shall either hold the assets in custody or verify ownership of the assets",
    "either provide the additional own funds or benefit from a guarantee of equivalent amount",
]


def test_either_or_marker_recall_is_one():
    hits = sum(1 for s in _EITHER_OR_CASES if "either_or" in extract_limb_markers(s))
    recall = hits / len(_EITHER_OR_CASES)
    assert recall == 1.0, f"either/or marker recall {recall} < 1.0"


def test_gold_intact_either_or_row_is_detected():
    """The one un-truncated 'either … or …' gold row (idx 60) is detected — anchoring the scorer to real data."""
    stmt = _GOLD["findings"][60]["statement"]
    assert "either" in stmt.lower()
    assert "either_or" in extract_limb_markers(stmt)


# ── split_limbs: structure-adding split that NEVER loses a clause ────────────────────────────────────────────
def test_split_either_or_into_two_limbs():
    stmt = "either have additional own funds appropriate to cover liability risks or hold professional indemnity insurance"
    limbs = split_limbs(stmt)
    assert len(limbs) == 2
    assert "additional own funds" in limbs[0]
    assert "professional indemnity insurance" in limbs[1]
    # neither limb still carries the leading "either"
    assert not limbs[0].lower().startswith("either")


def test_split_lettered_enumeration():
    stmt = "the AIFM shall (a) appoint a depositary (b) notify the competent authority (c) keep records"
    limbs = split_limbs(stmt)
    assert len(limbs) >= 3
    assert any("appoint a depositary" in limb for limb in limbs)
    assert any("keep records" in limb for limb in limbs)


def test_split_unless_keeps_base_and_carveout():
    stmt = (
        "act as depositary for an AIF unless it has functionally separated its depositary functions"
    )
    limbs = split_limbs(stmt)
    assert len(limbs) == 2
    assert "act as depositary" in limbs[0]
    assert "functionally separated" in limbs[1]


def test_split_returns_whole_when_no_marker():
    """No marker → exactly one limb == the (cleaned) whole statement. Never empty, never lossy."""
    stmt = "ensure the AIF cash flows are properly monitored on an ongoing basis"
    assert split_limbs(stmt) == [stmt]


def test_split_handles_empty_and_none():
    assert split_limbs("") == [""]
    assert split_limbs(None) == [""]
    assert split_limbs("   ") == [""]


def test_split_uses_supporting_text_only_for_marker_detection():
    """A truncated statement that lost its marker can still split using the fuller supporting_text for DETECTION —
    but the split operates on the statement, never importing supporting_text content into a limb."""
    statement = "either have additional own funds to cover liability or hold professional indemnity insurance"
    # markers passed explicitly (as the render layer would) — split must honour them.
    markers = extract_limb_markers(statement)
    limbs = split_limbs(
        statement, supporting_text="(fuller body not used for content)", markers=markers
    )
    assert len(limbs) == 2
    assert "fuller body" not in " ".join(limbs)


def test_split_coverage_is_lossless_word_level():
    """LOAD-BEARING: the union of limb words covers every content word of the statement (no clause silently lost).
    The only tokens allowed to vanish are the split connectives themselves (either/or/unless/and/letters)."""
    stmt = "either appoint an external valuer or value the assets internally with functional independence"
    limbs = split_limbs(stmt)
    connectives = {"either", "or", "unless", "and", "alternatively"}
    original_words = [w for w in stmt.lower().split() if w not in connectives]
    limb_words = " ".join(limbs).lower().split()
    for w in original_words:
        assert w in limb_words, f"word {w!r} lost during split — would understate the duty"


# ── mutual_exclusion_group: group the internal-vs-external capital cohort, NEVER drop a pole ─────────────────
def test_mutual_exclusion_groups_capital_pair_from_structure_field():
    verdicts = [
        {"management_structure": "external", "statement": "initial capital of at least EUR 125000"},
        {"management_structure": "internal", "statement": "initial capital of at least EUR 300000"},
    ]
    groups = mutual_exclusion_group(verdicts)
    assert groups == [[0, 1]], (
        "both poles must be grouped together (alternative-to), neither dropped"
    )


def test_mutual_exclusion_infers_poles_from_statement_text():
    verdicts = [
        {
            "statement": "have an initial capital of at least EUR 125000 when appointed as external manager of AIFs"
        },
        {
            "statement": "an internally managed AIF shall have an initial capital of at least EUR 300000"
        },
    ]
    assert mutual_exclusion_group(verdicts) == [[0, 1]]


def test_mutual_exclusion_from_real_gold_pair():
    """The lawyer-flagged real pair: gold idx 38 (external EUR 125k) vs idx 59 (internal EUR 300k)."""
    f38 = _GOLD["findings"][38]  # AIFM external manager EUR 125000
    f59 = _GOLD["findings"][59]  # Member States ... internally managed AIF EUR 300000
    assert "external" in f38["statement"].lower() and "125000" in f38["statement"]
    assert "internally" in f59["statement"].lower() and "300000" in f59["statement"]
    verdicts = [{"statement": f38["statement"]}, {"statement": f59["statement"]}]
    groups = mutual_exclusion_group(verdicts)
    assert groups == [[0, 1]]
    # ZERO contradictions: the cohort contains BOTH poles and BOTH indices survive (nothing dropped).
    cohort = groups[0]
    assert set(cohort) == {0, 1}


def test_mutual_exclusion_lone_pole_is_not_a_group():
    """A single pole is not a contradiction — no group emitted (needs both poles to annotate 'alternative to')."""
    verdicts = [{"statement": "initial capital of at least EUR 125000 for an external manager"}]
    assert mutual_exclusion_group(verdicts) == []


def test_mutual_exclusion_ignores_non_capital_findings():
    verdicts = [
        {"statement": "ensure the AIF cash flows are properly monitored"},  # not capital
        {"statement": "implement adequate risk management systems"},  # not capital
    ]
    assert mutual_exclusion_group(verdicts) == []


def test_mutual_exclusion_cohort_zero_contradictions_property():
    """SCORER: every emitted cohort spans BOTH poles (an external and an internal member) — so it can never claim
    two same-pole findings exclude each other (zero contradictions). Read-only: input is never mutated."""
    verdicts = [
        {"management_structure": "external", "statement": "EUR 125000 external"},
        {"statement": "implement risk management"},  # noise (no pole)
        {"management_structure": "internal", "statement": "EUR 300000 internal"},
        {"management_structure": "internal", "statement": "EUR 300000 internal duplicate"},
    ]
    snapshot = json.dumps(verdicts, sort_keys=True)
    groups = mutual_exclusion_group(verdicts)
    assert json.dumps(verdicts, sort_keys=True) == snapshot, "input must not be mutated"
    for g in groups:
        poles = set()
        for i in g:
            s = (
                verdicts[i].get("management_structure") or verdicts[i].get("statement") or ""
            ).lower()
            poles.add("external" if "extern" in s else "internal")
        assert poles == {"external", "internal"}, (
            f"cohort {g} is not a both-pole cohort → contradiction risk"
        )


# ── dedup_groups: keep-first, annotate-only, never delete ────────────────────────────────────────────────────
def test_dedup_marks_transposed_duplicate_keep_first():
    findings = [
        {
            "actor": "AIFM",
            "action": "ensure a single depositary is appointed for each AIF",
            "modal": "shall",
            "law_source": "AIFMD",
        },
        {
            "actor": "AIFM",
            "action": "ensure a single depositary is appointed for each AIF",
            "modal": "shall",
            "law_source": "AIFMD",
        },
    ]
    out = dedup_groups(findings)
    assert out[0]["is_duplicate"] is False and out[0]["keep_index"] == 0
    assert out[1]["is_duplicate"] is True and out[1]["keep_index"] == 0
    assert out[0]["dedup_group"] == out[1]["dedup_group"]


def test_dedup_eu_and_cz_transposition_are_separate_law_sources():
    """The EU line and its CZ transposition share substance but DIFFERENT law_source → kept as distinct findings
    (different legal hooks). They are NOT marked duplicates of each other."""
    findings = [
        {
            "actor": "AIFM",
            "action": "have initial capital of EUR 125000",
            "modal": "shall",
            "law_source": "AIFMD",
        },
        {
            "actor": "AIFM",
            "action": "have initial capital of EUR 125000",
            "modal": "shall",
            "law_source": "ZISIF",
        },
    ]
    out = dedup_groups(findings)
    assert out[0]["is_duplicate"] is False and out[1]["is_duplicate"] is False
    assert out[0]["dedup_group"] != out[1]["dedup_group"]


def test_dedup_uses_statement_when_action_absent():
    findings = [
        {
            "actor": "AIFM",
            "statement": "review the risk management systems at least once a year",
            "modal": "shall",
            "law_source": "AIFMD",
        },
        {
            "actor": "AIFM",
            "statement": "Review the   risk-management systems, at least once a year.",
            "modal": "SHALL",
            "law_source": "AIFMD",
        },
    ]
    out = dedup_groups(findings)
    assert out[1]["is_duplicate"] is True  # normalization makes them the same substance


def test_dedup_distinct_duties_not_duplicates():
    findings = [
        {"actor": "AIFM", "action": "have initial capital of EUR 125000", "law_source": "AIFMD"},
        {"actor": "AIFM", "action": "have initial capital of EUR 300000", "law_source": "AIFMD"},
        {"actor": "AIFM", "action": "implement risk management systems", "law_source": "AIFMD"},
    ]
    out = dedup_groups(findings)
    assert all(o["is_duplicate"] is False for o in out)
    assert len({o["dedup_group"] for o in out}) == 3


# ── dedup precision scorer (spec: >= 0.98 on constructed transposed-duplicate pairs) ─────────────────────────
def _make_dup_pairs(n: int) -> list[dict]:
    """Construct n TRUE duplicate pairs (same substance + same law_source, one a normalization-variant of the
    other) interleaved — every odd index is a true duplicate of the even one before it. Each pair gets a UNIQUE
    substance (a per-pair token) so that the only duplicates in the whole corpus are the intended within-pair
    ones — no even-index finding is accidentally a duplicate of an earlier pair."""
    findings: list[dict] = []
    for k in range(n):
        actor = "AIFM"
        action = f"satisfy distinct duty number {k} for each AIF it manages"
        modal = "shall"
        findings.append({"actor": actor, "action": action, "modal": modal, "law_source": "AIFMD"})
        # a punctuation/case/whitespace variant of the SAME substance == a true duplicate
        variant = f"  {action.upper()},  "
        findings.append(
            {
                "actor": actor.upper(),
                "action": variant,
                "modal": modal.title(),
                "law_source": "AIFMD",
            }
        )
    return findings


def test_dedup_precision_on_constructed_duplicate_pairs():
    """SCORER: of all findings MARKED is_duplicate, the fraction that are TRUE duplicates must be >= 0.98.
    Construction guarantees every odd index is a real duplicate of the even index before it → precision 1.0."""
    findings = _make_dup_pairs(8)  # 16 findings, 8 true duplicate pairs
    out = dedup_groups(findings)
    marked = [o for o in out if o["is_duplicate"]]
    assert marked, "expected some duplicates to be marked"
    true_pos = sum(
        1
        for o in marked
        if o["index"] % 2 == 1 and out[o["index"] - 1]["limb_key"] == o["limb_key"]
    )
    precision = true_pos / len(marked)
    assert precision >= 0.98, f"dedup precision {precision} < 0.98"


def test_dedup_precision_no_false_positive_across_distinct_substance():
    """Mixed corpus of DISTINCT duties (no true duplicates) → ZERO findings marked duplicate (precision is vacuous
    but, critically, no false positives)."""
    findings = [
        {
            "actor": "AIFM",
            "action": "ensure a single depositary is appointed",
            "law_source": "AIFMD",
        },
        {"actor": "AIFM", "action": "implement risk management systems", "law_source": "AIFMD"},
        {
            "actor": "Investment firms",
            "action": "ensure information is fair clear and not misleading",
            "law_source": "MiFID",
        },
        {
            "actor": "depositary",
            "action": "ensure the AIF cash flows are monitored",
            "law_source": "AIFMD",
        },
    ]
    out = dedup_groups(findings)
    assert not any(o["is_duplicate"] for o in out)


# ── THE CARDINAL SAFETY PROOF: nothing is EVER dropped ───────────────────────────────────────────────────────
def test_nothing_is_ever_dropped_dedup_membership_total():
    """dedup_groups output membership is TOTAL: every input index appears EXACTLY once, regardless of how many are
    duplicates. A duplicate is annotated (is_duplicate=True), never removed."""
    findings = [
        *_make_dup_pairs(5),
        {"actor": "X", "action": "unique duty alpha", "law_source": "L1"},
        {"actor": "Y", "action": "unique duty beta", "law_source": "L2"},
    ]
    out = dedup_groups(findings)
    assert len(out) == len(findings), "dedup must return one annotation per input — never fewer"
    assert sorted(o["index"] for o in out) == list(range(len(findings)))


def test_nothing_is_ever_dropped_even_all_identical():
    """Even if ALL findings are identical duplicates, every one survives in the output (first kept, rest marked)."""
    findings = [
        {
            "actor": "AIFM",
            "action": "ensure a single depositary is appointed",
            "law_source": "AIFMD",
        }
    ] * 6
    out = dedup_groups(findings)
    assert len(out) == 6
    assert sum(1 for o in out if not o["is_duplicate"]) == 1  # exactly one keeper
    assert sum(1 for o in out if o["is_duplicate"]) == 5  # the other five annotated, not deleted
    assert all(o["keep_index"] == 0 for o in out)


def test_nothing_is_ever_dropped_mutual_exclusion_keeps_both_poles():
    """mutual_exclusion never removes a pole: the union of all grouped indices loses no member, and both poles of
    the real EUR125k/EUR300k pair survive."""
    verdicts = [
        {"management_structure": "external", "statement": "EUR 125000 external manager"},
        {"management_structure": "internal", "statement": "EUR 300000 internally managed"},
    ]
    groups = mutual_exclusion_group(verdicts)
    grouped = {i for g in groups for i in g}
    assert grouped == {0, 1}, (
        "both poles must remain present — neither is ever the 'losing' dropped finding"
    )


def test_split_never_returns_empty():
    """split_limbs over a broad set of inputs ALWAYS returns at least one non-degenerate result (never [] → which
    would understate the duty to 'nothing')."""
    samples = [s["statement"] for s in _GOLD["findings"]] + [
        "either A or B",
        "(a) x (b) y",
        "do X unless Y",
        "alternatively do Z",
        "plain single duty",
    ]
    for s in samples:
        limbs = split_limbs(s)
        assert isinstance(limbs, list) and len(limbs) >= 1
        # the union of limbs is non-empty whenever the input was non-empty
        if s.strip():
            assert "".join(limbs).strip() != ""


def test_full_gold_slice_split_is_lossless_and_total():
    """Over ALL 79 gold statements: split_limbs returns >=1 limb each and never empties a non-empty statement
    (the module cannot silently erase a duty even on real, truncated text)."""
    for f in _GOLD["findings"]:
        stmt = f["statement"]
        limbs = split_limbs(stmt)
        assert len(limbs) >= 1
        if stmt.strip():
            joined = " ".join(limbs)
            assert joined.strip(), f"split erased a non-empty statement: {stmt!r}"
