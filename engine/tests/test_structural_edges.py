"""NT4 — the structural-edge registry + extraction (pure, no DB)."""

from __future__ import annotations

from pythia.graph.relations import R0, BY_LABEL
from pythia.ingest.structural_edges import _ACT_REF

_NT4 = (
    "delegates_authority",
    "implements",
    "amends",
    "amended_by",
    "repeals",
    "repealed_by",
    "supersedes",
    "superseded_by",
    "consolidates_into",
    "consolidated_from",
)


def test_registry_grew_with_the_amendment_delegation_family():
    # registry integrity (robust to growth): rel_ids are contiguous 1..len(R0) — was 20 at NT4,
    # now 28 after N5 added the 4 case-law pairs (interprets/applies/unifies/annuls, rel_id 21-28).
    assert max(r.rel_id for r in R0) == len(R0)
    for label in _NT4:
        assert label in BY_LABEL, label


def test_inverses_are_consistent():
    # inverse-of-inverse is self, for every NT4 edge type
    for label in _NT4:
        r = BY_LABEL[label]
        assert BY_LABEL[r.inverse_of].inverse_of == label, label


def test_delegation_outweighs_amendment_lineage():
    # the delegation chain is a strong structural edge; amendment/repeal/supersession are weaker lineage
    assert BY_LABEL["delegates_authority"].default_weight > BY_LABEL["amends"].default_weight
    assert BY_LABEL["implements"].default_weight > BY_LABEL["repeals"].default_weight


def test_act_ref_extraction():
    refs = [m.group(1) for m in _ACT_REF.finditer("zrušuje se zákon č. 591/1992 Sb. a zákon č. 61/1996 Sb.")]
    assert refs == ["591/1992", "61/1996"]
