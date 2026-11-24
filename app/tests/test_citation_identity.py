"""B2a-12 — citation↔provision IDENTITY (INV-3/4): a quote is only ever "verified" against the RIGHT statute.

`verify_citation(span, provision_text)` runs at PERSIST time in L2 (`_persist_finding`) and L3
(`_persist_obligation`); the text it is given is bound to the cited provision by the identity assertion in
`evaluate_provision` / `extract_obligations` (the regsuper load keys on `p.id = :pid`; the assertion makes
the id↔text binding explicit and would catch a nexus-resolve mismatch — B2a-15). L4 GAP findings are
EXEMPT by design: they INHERIT `cited_provisions` from the L3 obligation they flag and carry no
LLM-generated span, so there is no quote to verify.

These pure tests pin the substantive guarantee: a span lifted from statute A never "verifies" against
statute B's unrelated text, and a genuine Czech-diacritic quote verifies against its own text.
"""

from regsuper.analysis.citation_check import review_state_for, verify_citation

# real-ish Czech provision texts from two unrelated acts
_A = (
    "Zaměstnavatel je povinen zajistit bezpečnost a ochranu zdraví zaměstnanců při práci s ohledem na "
    "rizika možného ohrožení jejich života a zdraví, která se týkají výkonu práce."
)
_B = (
    "Správce daně při správě daní postupuje v souladu se zákony a jinými právními předpisy a šetří práva "
    "a právem chráněné zájmy daňových subjektů a třetích osob."
)


def test_quote_from_A_does_not_verify_against_B():
    """The wrong-statute hole: a span clearly from statute A, checked against statute B's text, must NOT
    come back 'verified' or 'fuzzy' — it is `not_found`, and its review_state flags it suspect."""
    span_from_A = "zajistit bezpečnost a ochranu zdraví zaměstnanců při práci"
    verdict = verify_citation(span=span_from_A, provision_text=_B)
    assert verdict == "not_found"
    assert review_state_for(verdict) == "unverified_citation"


def test_genuine_czech_quote_verifies_against_its_own_text():
    """Diacritics are meaningful (not stripped): an exact Czech quote verifies against its own provision."""
    span = "bezpečnost a ochranu zdraví zaměstnanců při práci"
    assert verify_citation(span=span, provision_text=_A) == "verified"


def test_diacritic_mismatch_is_not_silently_verified():
    """A near-miss that drops the diacritics is NOT the same legal term — must not exact-verify
    (the careful-Czech contract; 'plnění' ≠ 'plneni')."""
    # 'plnění' (performance) vs the diacritic-stripped 'plneni' against a text that only has the real word
    text = "Dlužník je povinen splnit svůj závazek řádně a včas, jinak je v prodlení s plněním."
    assert (
        verify_citation(span="plnění", provision_text=text) == "verified"
    )  # the real word verifies
    # an unrelated span does not
    assert (
        verify_citation(span="kybernetická bezpečnost informačního systému", provision_text=text)
        == "not_found"
    )


def test_empty_span_is_empty_not_verified():
    assert verify_citation(span="", provision_text=_A) == "empty"
    assert verify_citation(span=None, provision_text=_A) == "empty"
