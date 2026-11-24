"""B2b-18 (#66/WP-OUT-3) — the PURE section splitter: section-headed draft → PythiaAnswer.sections[].

These pin the additive sectioned contract: a section-headed draft parses into the three legal sections that
CONTIGUOUSLY TILE the whole draft, so the union of section chips is a genuine PARTITION of the flat
citations_map (no grounded chip lost/duplicated/invented — the partition now holds STRUCTURALLY, not by luck:
a preamble chip lands in the first section, and a chip-less / no-case-law section is MERGED into a neighbour
rather than dropped). Judikatura is ABSENT-not-empty (no standalone case_law section unless a grounded
CASE_LAW chip is present, but its statute chips are merged forward, never discarded), chip offsets still
slice their verbatim span out of the WHOLE draft (draft-relative, never re-based), text == draft[range]
exactly (incl. the section's own '##' heading line), a marker-less draft degrades to [] without losing
content, and the flat drafted_answer / citations_map / verdict are byte-stable.
"""

from __future__ import annotations

from pythia.contract import CitedProvision, Section
from pythia.reasoning.sections import split_into_sections
from pythia.reasoning.verify import verify

# A retrieved set: two statute provisions (p1, p3) + one CASE_LAW provision (pc, ext "5/2019").
_STATUTE = [
    CitedProvision(provision_id="p1", label="§ 297", legal_hierarchy="STATUTE"),
    CitedProvision(provision_id="p3", label="§ 318", legal_hierarchy="STATUTE"),
]
_CASE_LAW = CitedProvision(provision_id="pc", label="§ 12", legal_hierarchy="CASE_LAW")

# ground-truth tuples for verify(): [(provision_id, ext, label), …]
_RET_STATUTE = [("p1", "240/2013", "§ 297"), ("p3", "240/2013", "§ 318")]
_RET_MIXED = [("p1", "240/2013", "§ 297"), ("pc", "5/2019", "§ 12")]

# A section-headed STATUTE-ONLY draft (no case law). Built so verify() grounds both bracketed cites.
_DRAFT_STATUTE = (
    "## Legislativní rámec\n"
    "The manager must notify [240/2013 § 297] and keep records [240/2013 § 318].\n"
    "## Hodnocení\n"
    "Both duties are mandatory and continuous."
)
# A section-headed MIXED draft — a Judikatura section that actually cites a court ruling (5/2019 → pc).
_DRAFT_MIXED = (
    "## Legislativní rámec\n"
    "The manager must notify [240/2013 § 297].\n"
    "## Judikatura\n"
    "The court confirmed this duty in [5/2019 § 12].\n"
    "## Hodnocení\n"
    "The obligation is strict."
)


def _verdict(draft, ret):
    v = verify(draft, ret)
    assert v.verdict == "ok", (draft, v)
    return v


# (a) section-headed draft + citations_map → correct sections; Judikatura present WITH a case-law chip.
def test_sections_parse_with_case_law_present():
    v = _verdict(_DRAFT_MIXED, _RET_MIXED)
    secs = split_into_sections(_DRAFT_MIXED, v.citations_map, [_STATUTE[0], _CASE_LAW])
    kinds = [s.kind for s in secs]
    assert kinds == ["legislative", "case_law", "assessment"], kinds
    assert all(isinstance(s, Section) for s in secs)
    leg, case, assess = secs
    assert "Legislativní rámec" == leg.heading and "Judikatura" == case.heading
    assert "Hodnocení" == assess.heading
    # the legislative chip grounds p1, the case_law chip grounds pc (a CASE_LAW provision)
    assert [c["provision_id"] for c in leg.citations_map] == ["p1"]
    assert [c["provision_id"] for c in case.citations_map] == ["pc"]
    assert assess.citations_map == []  # the assessment prose carries no inline citation here


# (b) STATUTE-ONLY section-headed draft (no case-law chip) → NO case_law section (absent-not-empty).
def test_statute_only_omits_judikatura_section():
    v = _verdict(_DRAFT_STATUTE, _RET_STATUTE)
    secs = split_into_sections(_DRAFT_STATUTE, v.citations_map, _STATUTE)
    kinds = [s.kind for s in secs]
    assert kinds == ["legislative", "assessment"], kinds  # case_law OMITTED, not empty
    assert all(s.kind != "case_law" for s in secs)


# absent-not-empty edge: a Judikatura HEADING is present but cites only statute → still OMITTED (no case-law
# chip inside it), never emitted empty.
def test_judikatura_heading_without_case_law_chip_is_omitted():
    draft = (
        "## Legislativní rámec\nDuty in [240/2013 § 297].\n"
        "## Judikatura\nNothing on point here in [240/2013 § 318].\n"
        "## Hodnocení\nClear."
    )
    v = _verdict(draft, _RET_STATUTE)
    # both p1 (§297) and p3 (§318) are STATUTE; the Judikatura body cites only a statute → no case-law chip
    secs = split_into_sections(draft, v.citations_map, _STATUTE)
    assert [s.kind for s in secs] == ["legislative", "assessment"]


# a heading with an EMPTY body is dropped, not emitted empty.
def test_empty_bodied_section_is_dropped():
    draft = "## Legislativní rámec\nDuty in [240/2013 § 297].\n## Hodnocení\n   \n"
    v = _verdict(draft, _RET_STATUTE)
    secs = split_into_sections(draft, v.citations_map, _STATUTE)
    assert [s.kind for s in secs] == ["legislative"]  # blank Hodnocení body dropped


# (c) UNION-CONSISTENCY: union of section chip provision_ids == flat citations_map provision_ids.
def test_union_of_section_chips_equals_flat_map():
    for draft, ret, cps in [
        (_DRAFT_MIXED, _RET_MIXED, [_STATUTE[0], _CASE_LAW]),
        (_DRAFT_STATUTE, _RET_STATUTE, _STATUTE),
    ]:
        v = _verdict(draft, ret)
        secs = split_into_sections(draft, v.citations_map, cps)
        flat_ids = [c["provision_id"] for c in v.citations_map]
        union_ids = [c["provision_id"] for s in secs for c in s.citations_map]
        # no chip lost, duplicated, or invented across sections (a partition of the flat map)
        assert sorted(union_ids) == sorted(flat_ids), (draft, union_ids, flat_ids)
        assert len(union_ids) == len(set(id(c) for s in secs for c in s.citations_map))


# (d) chip offsets still slice the chip's VERBATIM span out of the WHOLE draft (draft-relative, both a
# legislative chip AND a non-first-section case_law chip), and section.range slices the section prose.
def test_chip_offsets_slice_verbatim_span_in_whole_draft():
    v = _verdict(_DRAFT_MIXED, _RET_MIXED)
    secs = split_into_sections(_DRAFT_MIXED, v.citations_map, [_STATUTE[0], _CASE_LAW])
    for s in secs:
        # the section range re-slices the section prose out of the whole draft
        assert _DRAFT_MIXED[s.range[0] : s.range[1]] == s.text
        for c in s.citations_map:
            # each chip's draft-relative offsets slice its verbatim label out of the WHOLE draft …
            assert _DRAFT_MIXED[c["start"] : c["end"]] == c["label"]
            # … and the chip span lies inside its section range (the containment that assigned it)
            assert s.range[0] <= c["start"] and c["end"] <= s.range[1]
    # explicitly assert we proved BOTH a legislative chip and a NON-first-section (case_law) chip
    assert any(s.kind == "legislative" and s.citations_map for s in secs)
    assert any(s.kind == "case_law" and s.citations_map for s in secs)


# (e) GRACEFUL DEGRADATION: a draft with NO markers → no crash, sections == [], drafted_answer complete.
def test_no_markers_degrades_to_empty_without_losing_content():
    blob = "The manager must notify [240/2013 § 297] and keep records [240/2013 § 318]."
    v = _verdict(blob, _RET_STATUTE)
    secs = split_into_sections(blob, v.citations_map, _STATUTE)
    assert secs == []  # rely on drafted_answer (the complete answer); nothing lost
    # an empty / None draft is also safe
    assert split_into_sections("", [], []) == []
    assert split_into_sections(None, [], None) == []


# (f) REGRESSION: the splitter NEVER mutates the flat citations_map / verdict — they stay byte-stable, and
# every section chip is byte-equal to its flat-map source (Pydantic copies the dict into the Section, so it
# is equal-by-VALUE, not by identity; what matters is the flat path a non-sections consumer reads is
# unchanged and no chip's offsets/ids were re-based).
def test_flat_path_byte_stable_and_chips_match_flat_map():
    v = _verdict(_DRAFT_MIXED, _RET_MIXED)
    flat_before = [dict(c) for c in v.citations_map]  # snapshot of the flat map
    flat_ids_before = list(v.grounded_provision_ids)
    conf_before, verdict_before = v.confidence, v.verdict
    secs = split_into_sections(_DRAFT_MIXED, v.citations_map, [_STATUTE[0], _CASE_LAW])
    # the flat map is untouched (same length, same dict contents, same order)
    assert [dict(c) for c in v.citations_map] == flat_before
    assert list(v.grounded_provision_ids) == flat_ids_before
    assert (v.confidence, v.verdict) == (conf_before, verdict_before)
    # every section chip is byte-equal to a flat chip (no offset re-base, no invented field)
    flat_set = [dict(c) for c in v.citations_map]
    for s in secs:
        for c in s.citations_map:
            assert dict(c) in flat_set


def _flat_ids(v):
    return sorted(c["provision_id"] for c in v.citations_map)


def _union_ids(secs):
    return sorted(c["provision_id"] for s in secs for c in s.citations_map)


# T1 PREAMBLE: a grounded chip BEFORE the first heading must NOT be lost — it now belongs to the first
# section (the first tile is back-extended to offset 0 to absorb the preamble), so union == flat.
def test_preamble_chip_is_kept_in_first_section():
    draft = (
        "Note the duty up front [240/2013 § 297].\n"
        "## Legislativní rámec\nAnd keep records [240/2013 § 318].\n"
        "## Hodnocení\nClear."
    )
    v = _verdict(draft, _RET_STATUTE)
    secs = split_into_sections(draft, v.citations_map, _STATUTE)
    # the preamble chip (p1) is NOT dropped: union of section chips == flat map
    assert _union_ids(secs) == _flat_ids(v) == ["p1", "p3"], (_union_ids(secs), _flat_ids(v))
    # it lives in the FIRST section, whose range begins at offset 0 (it absorbed the preamble)
    assert secs[0].range[0] == 0
    assert "p1" in [c["provision_id"] for c in secs[0].citations_map]


# T2 DROPPED-JUDIKATURA-WITH-STATUTE-CHIP (the HIGH defect): the LLM wrongly places a grounded STATUTE chip
# under '## Judikatura' with no ruling cited. The case_law section is NOT emitted standalone, but its statute
# chip is MERGED forward (not dropped) → union == flat AND no emitted section is kind=='case_law'.
def test_dropped_judikatura_with_statute_chip_is_merged_not_lost():
    draft = (
        "## Legislativní rámec\nThe manager must notify [240/2013 § 297].\n"
        "## Judikatura\ncf. the statute itself [240/2013 § 318]. (no ruling on point)\n"
        "## Hodnocení\nBoth duties are mandatory."
    )
    v = _verdict(draft, _RET_STATUTE)  # BOTH p1 (§297) and p3 (§318) are STATUTE
    secs = split_into_sections(draft, v.citations_map, _STATUTE)
    # the misplaced statute chip p3 is preserved (merged into the legislative section), not silently lost
    assert _union_ids(secs) == _flat_ids(v) == ["p1", "p3"], (_union_ids(secs), _flat_ids(v))
    # and no standalone case_law widget is emitted (absent-not-empty)
    assert all(s.kind != "case_law" for s in secs)


# T3 CONTIGUITY: the emitted sections tile [0, len(draft)] EXACTLY — first.start==0, last.end==len, adjacent
# end==next start (no gap/overlap), and text == draft[range] for each (text now includes the heading line).
def test_sections_contiguously_tile_the_whole_draft():
    v = _verdict(_DRAFT_MIXED, _RET_MIXED)
    secs = split_into_sections(_DRAFT_MIXED, v.citations_map, [_STATUTE[0], _CASE_LAW])
    assert secs[0].range[0] == 0
    assert secs[-1].range[1] == len(_DRAFT_MIXED)
    for a, b in zip(secs, secs[1:]):
        assert a.range[1] == b.range[0], (a.range, b.range)  # no gap, no overlap
    for s in secs:
        assert _DRAFT_MIXED[s.range[0] : s.range[1]] == s.text  # text == draft[range], incl. heading line


# T4 PARTITION-GENERAL: for a normal 3-section draft, every flat chip is in EXACTLY ONE section and the union
# of section chip ids equals the flat map ids — the partition holds structurally (no chip lost/duplicated).
def test_partition_every_flat_chip_in_exactly_one_section():
    v = _verdict(_DRAFT_MIXED, _RET_MIXED)
    secs = split_into_sections(_DRAFT_MIXED, v.citations_map, [_STATUTE[0], _CASE_LAW])
    assert _union_ids(secs) == _flat_ids(v), (_union_ids(secs), _flat_ids(v))
    # exactly-once: number of section chips == number of flat chips, each by distinct object identity
    section_chip_ids = [id(c) for s in secs for c in s.citations_map]
    assert len(section_chip_ids) == len(set(section_chip_ids)) == len(v.citations_map)


# PATHOLOGICAL RECLASSIFY: the LLM emits ONLY '## Judikatura' with statute content (no other section). The
# lone chip-less-of-case-law section is reclassified to 'legislative' over the whole draft so the grounded
# statute chip is preserved and no empty Judikatura widget is emitted.
def test_only_judikatura_heading_with_statute_is_reclassified_legislative():
    draft = "## Judikatura\nStatute only here [240/2013 § 297]."
    v = _verdict(draft, [("p1", "240/2013", "§ 297")])
    secs = split_into_sections(draft, v.citations_map, [_STATUTE[0]])
    assert len(secs) == 1
    assert secs[0].kind == "legislative"  # reclassified, never a chip-less case_law
    assert secs[0].range == [0, len(draft)]
    assert _union_ids(secs) == _flat_ids(v) == ["p1"]  # the grounded statute chip survives


# ─────────────────────── VL-C-05 (WS5 step 42) — schema='legacy'|'structured' ───────────────────────────────
def test_legacy_schema_equals_structured_for_an_essay_draft():
    """A pure ESSAY draft splits IDENTICALLY under 'legacy' and 'structured' — the essay draft carries no
    structured heading, so the two vocabularies agree byte-for-byte (the legacy path is byte-identical to today
    BY CONSTRUCTION, not because the essay prompt happens not to emit structured headings)."""
    v = _verdict(_DRAFT_MIXED, _RET_MIXED)
    legacy = split_into_sections(_DRAFT_MIXED, v.citations_map, [_STATUTE[0], _CASE_LAW], schema="legacy")
    structured = split_into_sections(_DRAFT_MIXED, v.citations_map, [_STATUTE[0], _CASE_LAW], schema="structured")
    assert [s.model_dump() for s in legacy] == [s.model_dump() for s in structured]
    assert [s.kind for s in legacy] == ["legislative", "case_law", "assessment"]


def test_legacy_schema_never_splits_on_a_structured_heading():
    """A draft mixing an essay heading with a structured 7-slot heading: 'legacy' recognises ONLY the essay
    heading (the '## Vaše situace' line is folded into the essay section as prose), while 'structured' splits it
    out. This is the BY-CONSTRUCTION guarantee — a legacy draft can never phantom-split on a structured slot."""
    draft = (
        "## Legislativní rámec\n"
        "Statute prose here.\n"
        "## Vaše situace\n"
        "A structured heading that legacy must treat as ordinary prose.\n"
    )
    legacy = split_into_sections(draft, [], [], schema="legacy")
    assert {s.kind for s in legacy} == {"legislative"}  # ONLY the essay kind; 'situation' absorbed
    structured = split_into_sections(draft, [], [], schema="structured")
    assert {s.kind for s in structured} == {"legislative", "situation"}  # structured DOES recognise it
    # contiguity + total-tiling still hold in legacy mode (the folded heading kept the tiling intact)
    assert legacy[0].range == [0, len(draft)]


def test_structured_seven_slot_draft_tiles_under_structured_schema():
    """A structured 7-slot draft tiles into its structured kinds under schema='structured' (contiguous, total)."""
    draft = (
        "## Vaše situace\nJste zaměstnavatel.\n"
        "## Konkrétní povinnosti a lhůty\nOznámení do 8 dnů [240/2013 § 297].\n"
        "## Kam se obrátit\nFinanční úřad.\n"
    )
    v = _verdict(draft, [("p1", "240/2013", "§ 297")])
    secs = split_into_sections(draft, v.citations_map, [_STATUTE[0]], schema="structured")
    kinds = [s.kind for s in secs]
    assert kinds == ["situation", "obligations", "where"]
    assert secs[0].range[0] == 0 and secs[-1].range[1] == len(draft)  # total tiling
    for a, b in zip(secs, secs[1:]):
        assert a.range[1] == b.range[0]  # contiguous, no gap/overlap
