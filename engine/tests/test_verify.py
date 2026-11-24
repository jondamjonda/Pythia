"""P5 — the VERIFY layer: format-robust citation grounding + ok/abstain/refuse policy (pure, no LLM).

These pin the fix for the blocker that previously refused good answers: a single strict regex matched
none of the local LLM's real bracket-format variations. The new grounding tolerates that variation yet
cannot fabricate (only RETRIEVED provisions ground).
"""

from __future__ import annotations

from pythia.reasoning.verify import Verdict, apply_faithfulness, ground, verify

# the retrieved set (ground truth): [(provision_id, act/ext, label), …]
_RET = [("p1", "240/2013", "§ 297"), ("p2", "32014L0065", "Article 27"), ("p3", "240/2013", "§ 318")]


def test_grounds_across_bracket_format_variations():
    for draft in [
        "The manager must notify [240/2013 § 297].",
        "Per [240/2013, § 297] the duty applies.",  # comma separator
        "See (240/2013 §297).",  # parens, no space after §
        "Under [§ 297 of Act 240/2013] …",  # reversed order, extra words
        "Viz [240/2013 § 297 odst. 1].",  # paragraph suffix tolerated
    ]:
        v = verify(draft, _RET)
        assert v.verdict == "ok", draft
        assert "p1" in v.grounded_provision_ids
        assert v.confidence == 1.0


def test_eu_celex_and_czech_article_markers():
    v = verify("Applies under [32014L0065 Article 27] and again [32014L0065 čl. 27].", _RET)
    assert v.verdict == "ok" and "p2" in v.grounded_provision_ids
    # the two references are the same citation (32014L0065 §27) → counted once, grounded
    assert v.n_citations == 1 and v.n_grounded == 1 and v.confidence == 1.0


def test_paragraph_grain_grounds_to_section_provision():
    v = verify("Obligation in [240/2013 § 318(1)].", _RET)  # § 318(1) → grounds to p3 (§ 318)
    assert v.verdict == "ok" and "p3" in v.grounded_provision_ids


def test_hallucinated_citation_is_refused():
    v = verify("The rule is in [999/1999 § 1].", _RET)  # act/§ never retrieved → no fabrication
    assert v.verdict == "refuse" and v.n_grounded == 0 and v.ungrounded


def test_no_citation_and_no_abstain_phrase_is_refused():
    v = verify("Managers must generally act prudently and in good faith.", _RET)
    assert v.verdict == "refuse" and v.n_citations == 0


def test_honest_no_answer_is_abstain_not_refuse():
    for draft in [
        "None of the provisions are on point here.",
        "Žádné z uvedených ustanovení se na tento případ nevztahuje.",
    ]:
        assert verify(draft, _RET).verdict == "abstain", draft


def test_mixed_grounding_below_floor_refuses():
    draft = "[240/2013 § 297] but also [999/1999 § 1], [998/1998 § 2], [997/1997 § 3]."  # 1/4 = 25% < 34%
    v = verify(draft, _RET)
    assert v.n_grounded == 1 and v.n_citations == 4 and v.verdict == "refuse"


def test_mixed_grounding_above_floor_is_ok():
    draft = "[240/2013 § 297] and [32014L0065 Article 27], plus a stray [999/1999 § 1]."  # 2 of 3 = 67%
    v = verify(draft, _RET)
    assert v.n_grounded == 2 and v.verdict == "ok" and v.confidence == round(2 / 3, 2)
    assert set(v.grounded_provision_ids) == {"p1", "p2"}


def test_bare_prose_section_grounds_by_number():
    # the local LLM's REAL style observed live: no brackets, act implicit ("podle § 297 zákona")
    draft = "Podle § 297 zákona musí obhospodařovatel provést notifikaci; viz též Article 27."
    v = verify(draft, _RET)
    assert v.verdict == "ok"
    assert set(v.grounded_provision_ids) == {"p1", "p2"}  # § 297 → p1, Article 27 → p2
    assert v.n_citations == 2 and v.n_grounded == 2


def test_bare_prose_section_not_retrieved_is_ungrounded():
    v = verify("Podle § 999 zákona platí zvláštní výjimka.", _RET)  # § 999 not in the retrieved set
    assert v.verdict == "refuse" and v.n_grounded == 0 and "§999" in v.ungrounded


def test_phantom_part_word_does_not_falsely_ground():
    # _SEC_RE word-boundary fix: 'part 27' must NOT phantom-match as 'art 27' and ground to the retrieved
    # Article 27 (p2). A zero-real-citation answer must still REFUSE, not slip through at confidence 1.0.
    v = verify("Managers must act prudently; see part 27 of the internal policy.", _RET)
    assert v.verdict == "refuse" and v.n_citations == 0


def test_phantom_part_words_do_not_inflate_ungrounded_count():
    # the other face of the same bug: a well-cited answer must stay OK — phantom 'part 5'/'part 6' must
    # NOT count as ungrounded citations that push the single real, grounded citation below the floor.
    v = verify("Obligation in [240/2013 § 297]; cf. part 5 and part 6 of the contract.", _RET)
    assert v.verdict == "ok" and v.n_citations == 1 and v.n_grounded == 1
    assert v.grounded_provision_ids == ["p1"]


# ── edge-assertion (faithfulness) policy — apply_faithfulness ────────────────────────────────────────
def _ok(conf: float = 0.9) -> Verdict:
    return Verdict("ok", conf, ["p1"], 3, 3, [])


def test_faithfulness_advisory_trims_confidence_but_keeps_ok():
    v = apply_faithfulness(_ok(0.9), ["claim X overstates § 297"], strict=False)
    assert v.verdict == "ok" and v.confidence <= 0.6 and v.reason


def test_faithfulness_strict_refuses_on_unsupported_claim():
    v = apply_faithfulness(_ok(0.9), ["claim X overstates § 297"], strict=True)
    assert v.verdict == "refuse" and "strict verify" in (v.reason or "")


def test_faithfulness_is_noop_without_unsupported_or_when_not_ok():
    base = _ok(0.9)
    assert apply_faithfulness(base, [], strict=True) is base  # no unsupported → unchanged
    refused = Verdict("refuse", 0.0, [], 1, 0, ["§999"], "withheld")
    assert apply_faithfulness(refused, ["x"], strict=True) is refused  # not ok → unchanged


# ── B2b-16 — span→id citation MAP (ADDITIVE; consistency with the flat grounded-id path) ─────────────
def _ids(cmap: list[dict]) -> set[str]:
    return {c["provision_id"] for c in cmap}


def test_ground_returns_four_tuple_with_map():
    grounded, ungrounded, n, cmap = ground("Per [240/2013 § 297].", _RET)
    assert grounded == ["p1"] and n == 1 and not ungrounded
    assert isinstance(cmap, list) and _ids(cmap) == {"p1"}


def test_citations_map_ids_equal_flat_grounded_ids_mixed_qualified_and_bare():
    # CONSISTENCY (the cardinal B2b-16 assertion): an act-qualified citation + a bare § + an EU article →
    # every grounded inline span carries a chip, and the chip ids are EXACTLY the flat grounded id set.
    draft = (
        "The manager must notify [240/2013 § 297]. Obligation in § 318. Investor protection applies under Article 27."
    )
    v = verify(draft, _RET)
    assert v.verdict == "ok"
    assert set(v.grounded_provision_ids) == {"p1", "p2", "p3"}
    assert _ids(v.citations_map) == set(v.grounded_provision_ids)  # map ids == flat grounded ids
    # every chip is a SUBSET member of the grounded ids (no chip without a grounded id)
    assert _ids(v.citations_map) <= set(v.grounded_provision_ids)


def test_citations_map_offsets_and_corpus_anchor_the_real_span():
    draft = "The duty is in [240/2013 § 297]."
    v = verify(draft, _RET)
    assert len(v.citations_map) == 1
    chip = v.citations_map[0]
    # offsets slice the verbatim bracket span out of the draft; corpus = the resolved act id
    assert draft[chip["start"] : chip["end"]] == "[240/2013 § 297]"
    assert chip["provision_id"] == "p1" and chip["corpus"] == "240/2013"


def test_citations_map_offsets_anchor_the_bare_section_span():
    # B2b-16 on the BARE-§ path (the bracketed test's counterpart): the chip offsets must slice the
    # verbatim bare "§ N" out of the draft, and corpus = the resolved act. (Closes the review's one gap.)
    draft = "Obligation in § 318 applies."
    v = verify(draft, _RET)
    assert _ids(v.citations_map) == {"p3"}
    chip = next(c for c in v.citations_map if c["provision_id"] == "p3")
    assert draft[chip["start"] : chip["end"]] == "§ 318"
    assert chip["corpus"] == "240/2013"


def test_citations_map_empty_on_abstain_and_on_ungrounded_refuse():
    # an honest abstain and a wholly-ungrounded refuse ground NOTHING → no chips (never a chip w/o a gid)
    assert verify("None of the provisions are on point here.", _RET).citations_map == []
    assert verify("The rule is in [999/1999 § 1].", _RET).citations_map == []


def test_citations_map_carried_through_faithfulness_passes():
    base = verify("The duty is in [240/2013 § 297].", _RET)
    assert _ids(base.citations_map) == {"p1"}
    # advisory: stays ok, chips preserved
    adv = apply_faithfulness(base, ["claim overstates § 297"], strict=False)
    assert adv.verdict == "ok" and _ids(adv.citations_map) == {"p1"}
    # strict: flips to refuse but keeps the grounded ids → keeps the chips too (consistency)
    strict = apply_faithfulness(base, ["claim overstates § 297"], strict=True)
    assert strict.verdict == "refuse" and _ids(strict.citations_map) == {"p1"}


# ── B2b-17 — bare-§ ACT-DISAMBIGUATION ("resolve-or-drop, never mis-link") ────────────────────────────
# Two retrieved acts that BOTH carry § 297 (the audit bug: a bare "§ 297" silently bound to the FIRST).
_RET_DUP = [("a297", "240/2013", "§ 297"), ("b297", "256/2004", "§ 297"), ("c", "256/2004", "§ 10")]


def test_bare_section_with_two_acts_sharing_it_is_dropped_not_cross_linked():
    # ADVERSARIAL: a bare "§ 297" is genuinely ambiguous (two retrieved acts have it) → it must NOT bind to
    # EITHER act (and certainly not silently to the first). Resolve-or-drop: no grounded id, no chip.
    grounded, ungrounded, n, cmap = ground("Obhospodařovatel musí podle § 297 provést notifikaci.", _RET_DUP)
    assert "a297" not in grounded and "b297" not in grounded  # never cross-linked to the wrong statute
    assert not grounded  # dropped → grounds nothing
    assert _ids(cmap) == set()  # and emits no chip
    assert any("297" in u for u in ungrounded)  # recorded as ungrounded (ambiguous act)


def test_ambiguous_bare_section_refuses_where_old_code_would_have_grounded():
    # The whole point of the fix: the old `setdefault` would have grounded this bare § to the FIRST act at
    # confidence 1.0 (verdict ok) while silently MIS-LINKING the statute. Now it grounds nothing → refuse.
    v = verify("Obhospodařovatel musí podle § 297 provést notifikaci.", _RET_DUP)
    assert v.verdict == "refuse" and v.n_grounded == 0
    assert "a297" not in v.grounded_provision_ids and "b297" not in v.grounded_provision_ids


def test_single_act_bare_section_still_grounds():
    # CONTROL: when only ONE retrieved act carries § 297 there is no ambiguity → it still grounds (the
    # single-act fast path is unchanged), and the chip resolves to that one act.
    v = verify("Obhospodařovatel musí podle § 297 provést notifikaci.", _RET)
    assert v.verdict == "ok" and v.grounded_provision_ids == ["p1"]
    assert _ids(v.citations_map) == {"p1"}
    assert v.citations_map[0]["corpus"] == "240/2013"


def test_act_qualified_marker_disambiguates_even_when_section_is_shared():
    # An ACT-QUALIFIED "[256/2004 § 297]" stays on its precise (act,§) path — the shared § 297 does NOT
    # make it ambiguous, because the act is explicit. It grounds to the RIGHT act (b297), not a297.
    v = verify("The rule is in [256/2004 § 297].", _RET_DUP)
    assert v.verdict == "ok" and v.grounded_provision_ids == ["b297"]
    assert _ids(v.citations_map) == {"b297"}
    chip = v.citations_map[0]
    assert chip["corpus"] == "256/2004" and chip["provision_id"] == "b297"


def test_act_qualified_path_grounds_exactly_as_before_regression():
    # REGRESSION: a known-good act-qualified answer's verdict + grounded set is byte-for-byte unchanged.
    draft = "[240/2013 § 297] and [32014L0065 Article 27], plus a stray [999/1999 § 1]."  # 2 of 3 = 67%
    v = verify(draft, _RET)
    assert v.n_grounded == 2 and v.verdict == "ok" and v.confidence == round(2 / 3, 2)
    assert set(v.grounded_provision_ids) == {"p1", "p2"}
    assert _ids(v.citations_map) == {"p1", "p2"}  # and the chips agree with the flat set
