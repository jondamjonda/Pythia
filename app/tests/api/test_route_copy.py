"""T-ANS-M5 — route-copy table + `false_premise` enum value.

Pure unit tests (no DB, no network, no LLM) over:
  1. `RefusalReason` back-compat — every pre-existing value is unchanged + present,
     and the new additive `FALSE_PREMISE = "false_premise"` value exists.
  2. The route_copy table — every honest-degradation route (+ the hedge banner +
     the disclaimer chain) carries a non-empty CZ AND EN {title, body, disclaimer}.
  3. `route_copy()` is TOTAL — it never raises KeyError for any route or language
     (unknown route → the safe `refuse` frame; unknown lang → the `cs` primary).

These are the M5 acceptance rows: enum back-compat, cs+en completeness, total lookup.
"""

from __future__ import annotations

import pytest

from regsuper.api import route_copy as rc
from regsuper.api.route_copy import all_copy_keys, route_copy
from regsuper.conduit.envelope import RefusalReason

# ---------------------------------------------------------------------------
# 1. RefusalReason enum — additive FALSE_PREMISE + back-compat.
# ---------------------------------------------------------------------------

# The pre-M5 value set (frozen here so a removal/rename of any existing value is
# caught — external integrators switch on these).
_PRE_M5_VALUES = {
    "reserved_advice",
    "low_confidence",
    "no_coverage",
    "out_of_scope",
    "as_of_out_of_range",
    "timeout",
    "guard_unavailable",
}


def test_false_premise_value_added():
    assert RefusalReason.FALSE_PREMISE == "false_premise"
    assert RefusalReason("false_premise") is RefusalReason.FALSE_PREMISE


def test_needs_clarification_value_added():
    # VL-A-09 (A3.3): the CLARIFY route's machine-contract reason (additive; a clarify is NOT a refusal).
    assert RefusalReason.NEEDS_CLARIFICATION == "needs_clarification"
    assert RefusalReason("needs_clarification") is RefusalReason.NEEDS_CLARIFICATION


def test_existing_refusal_reasons_unchanged():
    values = {r.value for r in RefusalReason}
    # every pre-M5 value still present + still maps to itself (no rename).
    assert _PRE_M5_VALUES <= values
    for v in _PRE_M5_VALUES:
        assert RefusalReason(v).value == v


def test_refusal_reason_is_exactly_pre_set_plus_false_premise():
    # The closed set = the pre-M5 values + the two later ADDITIVE members (false_premise, then VL-A-09's
    # needs_clarification). A new value must be an intentional, additive extension pinned right here.
    assert {r.value for r in RefusalReason} == _PRE_M5_VALUES | {
        "false_premise",
        "needs_clarification",
    }


def test_refusal_reason_str_round_trips():
    # StrEnum: a value is its own string (the contract integrators rely on).
    for r in RefusalReason:
        assert str(r) == r.value


# ---------------------------------------------------------------------------
# 2. Copy-table completeness — every key has non-empty cs + en {title,body,disclaimer}.
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = ("title", "body", "disclaimer")

# The four honest-degradation routes the table MUST cover.
_REQUIRED_ROUTES = {
    rc.ROUTE_NOT_A_LEGAL_QUESTION,
    rc.ROUTE_NOT_IN_CORPUS,
    rc.ROUTE_FALSE_PREMISE,
    rc.ROUTE_REFUSE,
}


def test_all_required_routes_present():
    keys = set(all_copy_keys())
    assert _REQUIRED_ROUTES <= keys
    # the hedge banner + the disclaimer chain are also supplied.
    assert rc.KEY_HEDGE_BANNER in keys
    assert rc.KEY_DISCLAIMER in keys
    assert rc.KEY_DISCLAIMER_AS_OF_UNKNOWN in keys
    assert rc.KEY_DISCLAIMER_UNVERIFIED_CITATIONS in keys


def test_non_sound_routes_match_required():
    assert set(rc.NON_SOUND_ROUTES) == _REQUIRED_ROUTES


@pytest.mark.parametrize(
    "key",
    _REQUIRED_ROUTES
    | {
        rc.KEY_HEDGE_BANNER,
        rc.KEY_DISCLAIMER,
        rc.KEY_DISCLAIMER_AS_OF_UNKNOWN,
        rc.KEY_DISCLAIMER_UNVERIFIED_CITATIONS,
    },
)
@pytest.mark.parametrize("lang", ("cs", "en"))
def test_every_key_has_nonempty_fields_in_both_langs(key, lang):
    block = route_copy(key, lang)
    assert set(_REQUIRED_FIELDS) <= set(block)
    for field in _REQUIRED_FIELDS:
        assert isinstance(block[field], str)
        assert block[field].strip(), f"{key}/{lang}/{field} is empty"


def test_cs_and_en_differ_per_route():
    # a real translation, not a copy-paste of the same string into both langs.
    for key in all_copy_keys():
        cs = route_copy(key, "cs")
        en = route_copy(key, "en")
        assert cs["title"] != en["title"], f"{key} title not translated"
        assert cs["body"] != en["body"], f"{key} body not translated"


def test_every_key_in_table_is_total_in_both_langs():
    # cover the WHOLE table, not just the required subset.
    for key in all_copy_keys():
        for lang in ("cs", "en"):
            block = route_copy(key, lang)
            for field in _REQUIRED_FIELDS:
                assert block[field].strip()


# ---------------------------------------------------------------------------
# 3. route_copy() is TOTAL — never KeyErrors.
# ---------------------------------------------------------------------------


def test_unknown_route_falls_back_to_refuse_frame():
    # an unrecognised gateway route must degrade to the safe honest 'refuse' frame.
    got = route_copy("totally_unknown_route_xyz")
    assert got == route_copy(rc.ROUTE_REFUSE)


def test_unknown_lang_falls_back_to_cs():
    got = route_copy(rc.ROUTE_NOT_IN_CORPUS, "de")
    assert got == route_copy(rc.ROUTE_NOT_IN_CORPUS, "cs")


def test_default_lang_is_cs():
    assert route_copy(rc.ROUTE_REFUSE) == route_copy(rc.ROUTE_REFUSE, "cs")


def test_lookup_never_raises_for_any_route_lang_combo():
    routes = [*all_copy_keys(), "", "sound", "nonsense", "FALSE_PREMISE"]
    langs = ["cs", "en", "de", "", "CS", "fr"]
    for r in routes:
        for lang in langs:
            block = route_copy(r, lang)  # must not raise
            assert set(_REQUIRED_FIELDS) <= set(block)


def test_returned_dict_is_a_copy_not_the_table():
    # mutating a returned block must not corrupt the table for the next caller.
    block = route_copy(rc.ROUTE_REFUSE, "cs")
    block["title"] = "MUTATED"
    fresh = route_copy(rc.ROUTE_REFUSE, "cs")
    assert fresh["title"] != "MUTATED"
