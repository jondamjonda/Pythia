"""TC-3 — the temporal PROSE GUARD proof (LLM-prose-only, gated, additive, conservative).

Closes the free-text gap left by TC-2: TC-2 keeps a per-provision-SUPERSEDED rule out of the STRUCTURED
obligation_register but still RETRIEVES it as historical context, and reasoning/answer.py's PROSE context
gets ZERO temporal signal — so the model could present a repealed/superseded/future provision as CURRENT
law with no caveat. TC-3, behind a NEW default-OFF flag, annotates the LLM answer-context with a per-
provision temporal marker and splices a temporal-validity instruction into the gen prompt.

Proves the TC-3 invariants WITHOUT any live LLM (no Ollama needed — these are pure unit tests):

  (a) flag defaults False.
  (b) _temporal_marker matrix — pit/doc signals map to the right marker; in-force/None ⇒ None; whitespace /
      non-str / list pit cannot crash or produce a spurious marker; both-signals ⇒ superseded (wins the tie).
  (c) OFF byte-identity — _build_ctx(..., guard=False) over a set INCLUDING a superseded provision == today's
      exact '[ext label] txt' format (no tag); the guard-OFF prompt selector returns _GEN_PROMPT /
      _GEN_PROMPT_STRICT UNCHANGED (string identity).
  (d) ON behaviour — _build_ctx(..., guard=True) tags the superseded line and leaves an in-force line
      untagged; the guard-ON gen prompts CONTAIN the temporal-validity instruction, the OFF ones do not.
  (e) cited-set independence — the marker logic lives ONLY in ctx/prompt; the cited-provision list is built
      from `meta`, never from the marked ctx (structural assertion).
"""

from __future__ import annotations

import inspect

import pytest

from pythia.config import PythiaSettings
from pythia.reasoning.answer import (
    _GEN_PROMPT,
    _GEN_PROMPT_STRICT,
    _GEN_PROMPT_STRICT_TEMPORAL,
    _GEN_PROMPT_TEMPORAL,
    _TEMPORAL_VALIDITY_INSTRUCTION,
    _build_ctx,
    _gen_prompts,
    _norm,
    _temporal_marker,
)

_SUPERSEDED_MARKER = "SUPERSEDED - historical, not current law"
_NOTYET_MARKER = "NOT YET IN FORCE - future law, not yet binding"


# ----- (a) flag defaults OFF -------------------------------------------------------------------------------


def test_flag_defaults_false():
    assert PythiaSettings().temporal_prose_guard_enabled is False


# ----- (b) _temporal_marker matrix -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pit, doc, expected",
    [
        # pit-driven
        ("SUPERSEDED", None, _SUPERSEDED_MARKER),
        ("NOT_YET_IN_FORCE", None, _NOTYET_MARKER),
        ("IN_FORCE", None, None),
        # doc-driven (superseded family)
        (None, "repealed", _SUPERSEDED_MARKER),
        (None, "superseded", _SUPERSEDED_MARKER),
        (None, "annulled", _SUPERSEDED_MARKER),
        (None, "abolished", _SUPERSEDED_MARKER),
        # doc-driven (not-yet family)
        (None, "future-effective", _NOTYET_MARKER),
        (None, "not-yet-in-force", _NOTYET_MARKER),
        (None, "not_yet_in_force", _NOTYET_MARKER),
        # ACTUAL stored source_document.temporal_status taxonomy (uppercase) — the pit-absent FALLBACK must
        # cover these (review MEDIUM): REPEALED / NOT_YET_EFFECTIVE (taxonomy.py) + ANNULLED_BY_CC (n5_check).
        (None, "REPEALED", _SUPERSEDED_MARKER),
        (None, "NOT_YET_EFFECTIVE", _NOTYET_MARKER),
        (None, "not_yet_effective", _NOTYET_MARKER),
        (None, "ANNULLED_BY_CC", _SUPERSEDED_MARKER),
        (None, "annulled_by_cc", _SUPERSEDED_MARKER),
        (None, "annulled-by-cc", _SUPERSEDED_MARKER),  # hyphen variant normalises to the same token
        (None, "IN_FORCE", None),  # the dominant stored value stays un-marked
        # in-force / unknown / empty ⇒ None
        (None, "in-force", None),
        ("IN_FORCE", "in-force", None),
        (None, None, None),
        ("", "", None),
        ("WHATEVER", "unknown", None),
        # case / whitespace insensitivity (normalisation works)
        ("  superseded  ", None, _SUPERSEDED_MARKER),
        (None, "  REPEALED  ", _SUPERSEDED_MARKER),
        # both signals co-occur ⇒ SUPERSEDED wins (more restrictive for a current-law claim)
        ("NOT_YET_IN_FORCE", "repealed", _SUPERSEDED_MARKER),
        ("SUPERSEDED", "future-effective", _SUPERSEDED_MARKER),
    ],
)
def test_temporal_marker_matrix(pit, doc, expected):
    assert _temporal_marker(pit, doc) == expected


@pytest.mark.parametrize("bad", [["SUPERSEDED"], {"x": 1}, 42, object(), b"SUPERSEDED"])
def test_temporal_marker_non_str_robustness(bad):
    # A whitespace / non-str / list value must neither crash nor produce a spurious marker. (A list/bytes/int
    # stringifies to something that is NOT one of the exact SUPERSEDED/NOT_YET tokens ⇒ None.)
    assert _temporal_marker(bad, None) is None
    assert _temporal_marker(None, bad) is None


def test_temporal_marker_is_pure_no_db_no_llm():
    # Pure signature: two positional args, no awaits, no DB/LLM. Cheap structural guard against drift.
    sig = inspect.signature(_temporal_marker)
    assert list(sig.parameters) == ["pit_status", "temporal_status"]
    assert not inspect.iscoroutinefunction(_temporal_marker)


# ----- (c) OFF byte-identity -------------------------------------------------------------------------------


def _meta_fixture():
    """A 3-provision meta dict mirroring answer._fetch's shape: one in-force, one per-provision SUPERSEDED,
    one doc-level future-effective."""
    return {
        "p1": {
            "ext": "240/2013",
            "label": "§ 297",
            "txt": "  In-force\ttext   here.  ",
            "pit_status": "IN_FORCE",
            "temporal_status": "in-force",
        },
        "p2": {
            "ext": "32014L0065",
            "label": "Article 27",
            "txt": "Superseded text.",
            "pit_status": "SUPERSEDED",
            "temporal_status": "in-force",
        },
        "p3": {
            "ext": "99/1963",
            "label": "§ 1",
            "txt": "Future text.",
            "pit_status": "IN_FORCE",
            "temporal_status": "future-effective",
        },
    }


def _today_ctx(meta, ordered):
    """The EXACT pre-TC-3 inline build (the literal that lived at answer.py line ~161)."""
    return "\n".join(f"[{meta[i]['ext']} {meta[i]['label']}] {_norm(meta[i]['txt'])}" for i in ordered)


def test_build_ctx_off_byte_identical_with_superseded_present():
    meta = _meta_fixture()
    ordered = ["p1", "p2", "p3"]  # set INCLUDES a superseded provision (p2)
    off = _build_ctx(meta, ordered, guard=False)
    assert off == _today_ctx(meta, ordered)
    # and crucially: NO tag leaks into the OFF string even though p2 is superseded
    assert "[WARN" not in off
    assert off == (
        "[240/2013 § 297] In-force text here.\n[32014L0065 Article 27] Superseded text.\n[99/1963 § 1] Future text."
    )


def test_gen_prompts_off_returns_unchanged_templates():
    g, gs = _gen_prompts(guard=False)
    # string IDENTITY — the OFF path returns the very same objects, byte-for-byte today's prompts
    assert g is _GEN_PROMPT
    assert gs is _GEN_PROMPT_STRICT
    assert _TEMPORAL_VALIDITY_INSTRUCTION not in g
    assert _TEMPORAL_VALIDITY_INSTRUCTION not in gs


# ----- (d) ON behaviour ------------------------------------------------------------------------------------


def test_build_ctx_on_tags_marked_lines_only():
    meta = _meta_fixture()
    ordered = ["p1", "p2", "p3"]
    on = _build_ctx(meta, ordered, guard=True)
    lines = on.split("\n")
    # p1 in-force ⇒ untagged, byte-identical to its OFF line
    assert lines[0] == "[240/2013 § 297] In-force text here."
    assert "[WARN" not in lines[0]
    # p2 pit=SUPERSEDED ⇒ superseded WARN tag, right after the bracket, before the text
    assert lines[1] == f"[32014L0065 Article 27] [WARN {_SUPERSEDED_MARKER}] Superseded text."
    # p3 doc=future-effective ⇒ not-yet WARN tag
    assert lines[2] == f"[99/1963 § 1] [WARN {_NOTYET_MARKER}] Future text."


def test_build_ctx_on_preserves_ext_label_text_and_order():
    meta = _meta_fixture()
    ordered = ["p3", "p1", "p2"]  # arbitrary order must be preserved exactly
    on = _build_ctx(meta, ordered, guard=True)
    lines = on.split("\n")
    assert lines[0].startswith("[99/1963 § 1] ")
    assert lines[1] == "[240/2013 § 297] In-force text here."  # the untagged in-force line is unchanged
    assert lines[2].startswith("[32014L0065 Article 27] ")


def test_gen_prompts_on_contains_temporal_instruction():
    g, gs = _gen_prompts(guard=True)
    assert g is _GEN_PROMPT_TEMPORAL
    assert gs is _GEN_PROMPT_STRICT_TEMPORAL
    # the ON variants CONTAIN the instruction; the OFF ones do NOT
    assert _TEMPORAL_VALIDITY_INSTRUCTION in g
    assert _TEMPORAL_VALIDITY_INSTRUCTION in gs
    assert _TEMPORAL_VALIDITY_INSTRUCTION not in _GEN_PROMPT
    assert _TEMPORAL_VALIDITY_INSTRUCTION not in _GEN_PROMPT_STRICT


def test_gen_prompts_on_differ_from_off_only_by_instruction():
    # the spliced variant equals the OFF template with the instruction inserted before 'STRUCTURE the answer'
    assert _GEN_PROMPT_TEMPORAL == _GEN_PROMPT.replace(
        "STRUCTURE the answer", _TEMPORAL_VALIDITY_INSTRUCTION + "STRUCTURE the answer", 1
    )
    assert _GEN_PROMPT_STRICT_TEMPORAL == _GEN_PROMPT_STRICT.replace(
        "STRUCTURE the answer", _TEMPORAL_VALIDITY_INSTRUCTION + "STRUCTURE the answer", 1
    )
    # the {q} / {ctx} / {tags} format slots survive the splice (the templates still .format cleanly)
    assert "{q}" in _GEN_PROMPT_TEMPORAL and "{ctx}" in _GEN_PROMPT_TEMPORAL
    assert "{tags}" in _GEN_PROMPT_STRICT_TEMPORAL and "{ctx}" in _GEN_PROMPT_STRICT_TEMPORAL


def test_warn_tokens_are_ascii_only():
    # the WARN tags + marker strings injected into ctx are ASCII (the spec required the warning TOKEN be ASCII;
    # the instruction PROSE itself may carry the em-dash that matches the surrounding gen-prompt house style).
    ("[WARN " + _SUPERSEDED_MARKER + "]").encode("ascii")
    ("[WARN " + _NOTYET_MARKER + "]").encode("ascii")
    _SUPERSEDED_MARKER.encode("ascii")
    _NOTYET_MARKER.encode("ascii")


# ----- (e) cited-set independence (structural) -------------------------------------------------------------


def test_cited_provisions_built_from_meta_not_marked_ctx():
    # STRUCTURAL proof that the cited list is independent of the marker logic: answer() builds `cps` from
    # `meta[i][...]` only, and the verify input `prov_tuples` from `meta` only — neither reads `ctx`. We assert
    # the answer() source threads the marked ctx ONLY into the .format(...) prompt calls, while cps/prov_tuples
    # are constructed from meta. This avoids any live LLM / Ollama dependency.
    src = inspect.getsource(__import__("pythia.reasoning.answer", fromlist=["answer"]).answer)
    # cps is built from meta, never from ctx
    assert "cps = [CitedProvision(" in src
    assert "prov_tuples = [(i, meta[i]" in src
    # ctx is only ever consumed via _build_ctx and the .format(... ctx=ctx ...) prompt calls. (B1a added the
    # additive use_ctx_txt kwarg — the ctx is still built solely via _build_ctx over meta, never marker logic.)
    assert "ctx = _build_ctx(meta, ordered, guard, use_ctx_txt=ctx_window)" in src
    # the guard is read exactly once
    assert src.count("get_settings().temporal_prose_guard_enabled") == 1


def test_marker_does_not_depend_on_label_or_text():
    # the marker is a function ONLY of (pit_status, temporal_status) — same answer regardless of ext/label/txt
    assert _temporal_marker("SUPERSEDED", "in-force") == _SUPERSEDED_MARKER
    assert _temporal_marker("IN_FORCE", "in-force") is None
