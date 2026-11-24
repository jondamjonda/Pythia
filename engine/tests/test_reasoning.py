"""The answer pipeline's pure VERIFY helper — a bracket without BOTH an act id and a §/Article (or a
non-citation like [foo] / (see above)) is not counted as a citation. (The full grounding + ok/abstain/
refuse policy matrix lives in test_verify.py.)"""

from __future__ import annotations

from pythia.reasoning.verify import ground

_RET = [("p1", "240/2013", "§ 297"), ("p2", "32014L0065", "Article 27")]


def test_non_citations_are_not_counted():
    t = (
        "Per [240/2013 § 297] and [32014L0065 Article 27]. An act alone like [240/2013] must NOT "
        "count, nor a stray [foo] or (see above)."
    )
    grounded, ungrounded, n, _cmap = ground(t, _RET)
    assert n == 2  # only the two real (act + §/Article) citations
    assert set(grounded) == {"p1", "p2"} and not ungrounded
