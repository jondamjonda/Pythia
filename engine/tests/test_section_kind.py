"""B1a-08 — section-KIND classifier (PURE planner, no DB) — REWORK 2026-06-24.

Covers the TITLE-LINE word-boundary marker table, the per-child vote, the DOMINANCE anchor resolver (one
outlier child can NEVER flip a section; operative is the neutral default), the finest-grain plan_kinds, and
the EXTRA-conservative preamble (a 'general provisions' chapter that also holds operative rules ≠ preamble).
All DB-free.
"""

from __future__ import annotations

from pythia.graph.section_kind import (
    KIND_DEFINITIONS,
    KIND_OPERATIVE,
    KIND_PENALTIES,
    KIND_PREAMBLE,
    KIND_TRANSITIONAL,
    SECTION_KINDS,
    _child_kind,
    _match_marker,
    _resolve_anchor,
    _title_line,
    plan_kinds,
)


# --------------------------------------------------------------------------- the title-line slice
def test_title_line_trims_at_first_clause_so_body_does_not_vote():
    # the heading is everything BEFORE the first numbered clause / obligation verb, with the "§ N"/"##### § N"
    # ordinal echoes peeled — so a body 'pokuta' is dropped and only the real heading word survives.
    assert _title_line("§ 5 Náležitosti dluhopisu\n(1) Emitent ... uloží pokutu ...") == "Náležitosti dluhopisu"
    assert _title_line("§ 12 Právo zvolit zmocněnce (1) Vlastník je povinen…") == "Právo zvolit zmocněnce"
    # the REAL corpus markdown shape: "{label} {text}" with text = "##### § N\n\n{Heading}\n{body}".
    assert _title_line("§ 1 ##### § 1\n\nPředmět úpravy\nTento zákon zapracovává…") == "Předmět úpravy"
    assert (
        _title_line("§ 183 ##### § 183\n\nPokuty vybírá a vymáhá správní orgán.")
        == "Pokuty vybírá a vymáhá správní orgán."
    )
    # a heading-less § whose body is the účinnost sentence still yields that sentence as the title line.
    assert _title_line("§ 22 ##### § 22\n\nZákon nabývá účinnosti dnem 1. ledna.").startswith("Zákon nabývá účinnosti")
    assert _title_line(None) == ""
    assert _title_line("") == ""


# --------------------------------------------------------------------------- the CZ+EN marker table
def test_marker_penalties_cz_and_en():
    assert _match_marker("Přestupky a sankce") == KIND_PENALTIES
    assert _match_marker("Správní delikty právnických osob") == KIND_PENALTIES
    assert _match_marker("Pokuty") == KIND_PENALTIES
    assert _match_marker("Penalties and sanctions") == KIND_PENALTIES
    assert _match_marker("Administrative offences") == KIND_PENALTIES


def test_marker_transitional_final_cz_and_en():
    assert _match_marker("Přechodná ustanovení") == KIND_TRANSITIONAL
    assert _match_marker("Závěrečná ustanovení") == KIND_TRANSITIONAL
    assert _match_marker("Zrušovací ustanovení") == KIND_TRANSITIONAL
    assert _match_marker("Účinnost") == KIND_TRANSITIONAL
    assert _match_marker("Transitional provisions") == KIND_TRANSITIONAL
    assert _match_marker("Entry into force") == KIND_TRANSITIONAL
    assert _match_marker("Repeal of earlier acts") == KIND_TRANSITIONAL


def test_marker_definitions_cz_and_en():
    assert _match_marker("Vymezení pojmů") == KIND_DEFINITIONS
    assert _match_marker("Vymezení základních pojmů") == KIND_DEFINITIONS
    assert _match_marker("Definice") == KIND_DEFINITIONS
    assert _match_marker("Definitions") == KIND_DEFINITIONS
    assert _match_marker("For the purposes of this Regulation") == KIND_DEFINITIONS


def test_marker_preamble_recital_cz_and_en():
    assert _match_marker("Úvodní ustanovení") == KIND_PREAMBLE
    assert _match_marker("Preambule") == KIND_PREAMBLE
    assert _match_marker("Předmět úpravy") == KIND_PREAMBLE
    assert _match_marker("Whereas the following…") == KIND_PREAMBLE
    assert _match_marker("Recital 14") == KIND_PREAMBLE


def test_marker_none_is_operative_default():
    assert _match_marker("§ 118 Vlastnické právo") is None
    assert _match_marker(None) is None
    assert _match_marker("") is None


def test_marker_word_boundary_body_mention_does_not_fire():
    # the REWORK bug-fix: a § whose HEADING is operative but whose BODY mentions 'pokuta' / 'pro účely' must
    # NOT vote penalty / preamble — the title-line slice drops the body so the marker never sees it. Uses the
    # real corpus markdown shape ("{label} ##### § N\n\n{Heading}\n**(1)** {body}").
    assert _match_marker("§ 5 ##### § 5\n\nNáležitosti dluhopisu\n**(1)** ... uloží pokutu") is None
    assert _match_marker("§ 12 ##### § 12\n\nPovinnosti emitenta\n**(1)** Pro účely splnění ...") is None


def test_marker_priority_penalties_beats_transitional_in_one_head():
    # a head naming BOTH sanctions and účinnost resolves to the higher-priority penalties marker.
    assert _match_marker("Sankce a účinnost") == KIND_PENALTIES


# --------------------------------------------------------------------------- the per-child vote
def test_child_marker_wins_over_columns():
    # an explicit penalties heading wins even if the row carries a definition flag.
    assert _child_kind("Přestupky", is_definition=True, roles=[1]) == KIND_PENALTIES


def test_child_role7_votes_penalties_without_marker():
    assert _child_kind("§ 200", is_definition=False, roles=[7]) == KIND_PENALTIES


def test_child_is_definition_or_role1_votes_definitions():
    assert _child_kind("§ 2", is_definition=True, roles=None) == KIND_DEFINITIONS
    assert _child_kind("§ 2", is_definition=False, roles=[1]) == KIND_DEFINITIONS


def test_child_no_evidence_votes_operative():
    assert _child_kind("§ 118", is_definition=False, roles=[2]) == KIND_OPERATIVE  # obligation ≠ a kind marker
    assert _child_kind(None, is_definition=None, roles=None) == KIND_OPERATIVE


def test_child_real_mislabels_from_the_bug_now_correct():
    # the four verified prior-version mislabels — each must now vote its TRUE kind.
    assert _child_kind("§ Náležitosti dluhopisu\n(1) ... uloží pokutu", False, None) == KIND_OPERATIVE  # was preamble
    assert _child_kind("§ Právo zvolit zmocněnce\n(1) je povinen", False, [2]) == KIND_OPERATIVE  # was penalties
    assert _child_kind("§ Závěrečná ustanovení", False, None) == KIND_TRANSITIONAL  # was penalties
    assert _child_kind("§ 2 Pro účely tohoto zákona se rozumí", True, [1]) == KIND_DEFINITIONS  # was preamble


# --------------------------------------------------------------------------- the DOMINANCE anchor resolver
def test_resolve_empty_is_operative_default():
    assert _resolve_anchor([]) == KIND_OPERATIVE


def test_resolve_one_outlier_penalty_never_flips_a_big_section():
    # THE core bug: a 20-child section with ONE penalty § stays operative (1 < max(2, 40%·20=8)).
    votes = [KIND_PENALTIES] + [KIND_OPERATIVE] * 19
    assert _resolve_anchor(votes) == KIND_OPERATIVE


def test_resolve_dominant_penalties_section():
    # a section of 5 penalty children is dominantly penalties.
    assert _resolve_anchor([KIND_PENALTIES] * 5) == KIND_PENALTIES
    # the boundary: 2 of 5 = 40% (≥ max(2, 2.0)) → penalties; 2 of 6 ≈ 33% (< 2.4) → operative.
    assert _resolve_anchor([KIND_PENALTIES] * 2 + [KIND_OPERATIVE] * 3) == KIND_PENALTIES
    assert _resolve_anchor([KIND_PENALTIES] * 2 + [KIND_OPERATIVE] * 4) == KIND_OPERATIVE


def test_resolve_role7_density_40pct_triggers_penalties_20pct_does_not():
    votes = [KIND_OPERATIVE] * 5
    assert _resolve_anchor(votes, [[7], [7], [2], [], []]) == KIND_PENALTIES  # 2/5 = 40%
    assert _resolve_anchor(votes, [[7], [2], [], [], []]) == KIND_OPERATIVE  # 1/5 = 20%


def test_resolve_definitions_needs_60pct():
    assert _resolve_anchor([KIND_DEFINITIONS] * 3 + [KIND_OPERATIVE] * 2) == KIND_DEFINITIONS  # 3/5 = 60%
    # a single defined term inside a 5-child operative section (20%) must NOT flip the section.
    assert _resolve_anchor([KIND_DEFINITIONS] + [KIND_OPERATIVE] * 4) == KIND_OPERATIVE


def test_resolve_transitional_needs_trailing_or_40pct():
    # a single trailing-block trans child (1/4) wins because it is a final block.
    assert _resolve_anchor([KIND_TRANSITIONAL] + [KIND_OPERATIVE] * 3, trailing=True) == KIND_TRANSITIONAL
    # the SAME shape NOT trailing and < 40% → operative (one outlier never flips a mid-act block).
    assert _resolve_anchor([KIND_TRANSITIONAL] + [KIND_OPERATIVE] * 3, trailing=False) == KIND_OPERATIVE
    # ≥40% wins even when not flagged trailing.
    assert _resolve_anchor([KIND_TRANSITIONAL] * 2 + [KIND_OPERATIVE] * 3, trailing=False) == KIND_TRANSITIONAL


def test_resolve_preamble_is_extra_conservative():
    # a true short intro-only block (all preamble votes, ZERO operative/penalty children) → preamble.
    assert _resolve_anchor([KIND_PREAMBLE, KIND_PREAMBLE]) == KIND_PREAMBLE
    # a 'general provisions' chapter: a scope clause (preamble vote) alongside operative rules → operative,
    # NOT preamble (the extra-conservative veto: any substantive-operative child blocks the downgrade).
    assert _resolve_anchor([KIND_PREAMBLE] + [KIND_OPERATIVE] * 3) == KIND_OPERATIVE
    # even with NO bare-operative vote, an obligation (role-2) child vetoes the preamble downgrade.
    assert _resolve_anchor([KIND_PREAMBLE, KIND_PREAMBLE], [[], [2]]) == KIND_OPERATIVE
    # a single preamble child among operative ones (an outlier) → operative.
    assert _resolve_anchor([KIND_PREAMBLE] + [KIND_OPERATIVE] * 4) == KIND_OPERATIVE


def test_resolve_downgrade_only_on_dominance():
    # NO non-operative dominance ⇒ operative (the neutral default; a downgrade never fires off one outlier).
    assert _resolve_anchor([KIND_OPERATIVE, KIND_OPERATIVE]) == KIND_OPERATIVE


# --------------------------------------------------------------------------- the pure FINEST-grain planner
def test_plan_kinds_aggregates_and_audits():
    anchors = [
        {
            "id": "a1",
            "children": [  # dominantly penalties (both children sanction)
                {"head": "Přestupky", "is_definition": False, "roles": [7]},
                {"head": "§ 200", "is_definition": False, "roles": [7]},
            ],
        },
        {
            "id": "a2",
            "children": [  # 100% definitions
                {"head": "Vymezení pojmů", "is_definition": True, "roles": [1]},
                {"head": "§ 3", "is_definition": True, "roles": [1]},
            ],
        },
        {"id": "a3", "children": []},  # no children → operative (neutral default)
    ]
    out = {p["id"]: p for p in plan_kinds(anchors)}
    assert out["a1"]["section_kind"] == KIND_PENALTIES
    assert out["a2"]["section_kind"] == KIND_DEFINITIONS
    assert out["a3"]["section_kind"] == KIND_OPERATIVE
    # kind_source is the auditable derivation trail
    assert "b1a08_vote" in out["a1"]["kind_source"]
    assert out["a3"]["kind_source"].startswith("b1a08_vote n=0")


def test_plan_kinds_one_outlier_child_never_flips_a_section():
    # a 10-child finest section: 9 operative §§ + 1 penalty § → operative (the resolver's dominance guard).
    children = [{"head": "§ %d" % i, "is_definition": False, "roles": []} for i in range(9)]
    children.append({"head": "§ 99 Pokuty", "is_definition": False, "roles": [7]})
    out = plan_kinds([{"id": "big", "children": children}])[0]
    assert out["section_kind"] == KIND_OPERATIVE


def test_plan_kinds_honours_trailing_flag_for_transitional():
    # the same lone-transitional child resolves differently by the trailing flag the DB fetch supplies.
    children = [{"head": "§ %d" % i, "is_definition": False, "roles": []} for i in range(3)] + [
        {"head": "§ 9 Účinnost", "is_definition": False, "roles": []}
    ]
    assert plan_kinds([{"id": "t", "trailing": True, "children": children}])[0]["section_kind"] == KIND_TRANSITIONAL
    assert plan_kinds([{"id": "t", "trailing": False, "children": children}])[0]["section_kind"] == KIND_OPERATIVE


def test_plan_kinds_outputs_only_vocab_values():
    anchors = [
        {"id": "x", "children": [{"head": h, "is_definition": False, "roles": []}]}
        for h in ("Přestupky", "Účinnost", "Definice", "Předmět úpravy", "§ 9")
    ]
    for p in plan_kinds(anchors):
        assert p["section_kind"] in SECTION_KINDS
