"""Authority-name patterns used to tag practical facts."""

from pythia.reasoning.practical_facts import _AUTHORITY_PATTERNS


def test_hygiene_station_pattern_matches_only_letters():
    # "hygienická stanice / stanici": the optional ending is a letter, never a stray "=".
    (pat,) = [p for p in _AUTHORITY_PATTERNS if p.pattern.startswith("hygienick")]
    assert pat.search("Hygienická stanice v Brně").group(0) == "Hygienická stanice"
    assert pat.search("viz hygienická stanic=x").group(0) == "hygienická stanic"


def test_no_authority_pattern_has_an_equals_sign_in_a_character_class():
    assert not [p.pattern for p in _AUTHORITY_PATTERNS if "=]" in p.pattern]
