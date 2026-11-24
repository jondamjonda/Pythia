"""R — the relation registry (v2 §3.3): N5 (case law) Wave 0 additions + invariants (pure, no DB)."""

from __future__ import annotations

from pythia.graph import relations as rel


def test_by_label_and_by_id_auto_derive():
    # the two lookup maps must cover every R0 row and round-trip via the Relation fields
    assert len(rel.BY_LABEL) == len(rel.R0)
    assert len(rel.BY_ID) == len(rel.R0)
    for r in rel.R0:
        assert rel.BY_LABEL[r.label] is r
        assert rel.BY_ID[r.rel_id] is r


def test_n5_case_law_relations_registered_and_round_trip():
    # the 8 new case-law-to-statute labels (rel_id 21–28) are all present in BY_LABEL …
    new_labels = (
        "interprets",
        "interpreted_by",
        "applies",
        "applied_by",
        "unifies",
        "unified_by",
        "annuls",
        "annulled_by",
    )
    for label in new_labels:
        assert label in rel.BY_LABEL, label

    # … and each pair's inverse_of round-trips: the inverse of my inverse is me.
    for label in new_labels:
        r = rel.BY_LABEL[label]
        assert r.inverse_of in rel.BY_LABEL, r.inverse_of
        assert rel.BY_LABEL[r.inverse_of].inverse_of == r.label


def test_n5_weights_and_annuls_non_traversable():
    # forward weights per spec; the annuls pair mirrors the repeals temporal-lineage shape (weight 0,
    # NON-traversable) so it records erga-omnes derogation lineage without polluting GNN message-passing.
    assert rel.BY_LABEL["interprets"].default_weight == 0.80
    assert rel.BY_LABEL["interpreted_by"].default_weight == 0.70
    assert rel.BY_LABEL["applies"].default_weight == 0.55
    assert rel.BY_LABEL["applied_by"].default_weight == 0.45
    assert rel.BY_LABEL["unifies"].default_weight == 0.60
    assert rel.BY_LABEL["unified_by"].default_weight == 0.50
    assert rel.BY_LABEL["annuls"].default_weight == 0.0
    assert rel.BY_LABEL["annulled_by"].default_weight == 0.0


def test_n5_relations_have_embedding_glosses():
    # the GNN consumes SentenceEmb(description); every new row must carry a non-empty gloss.
    for rid in range(21, 29):
        r = rel.BY_ID[rid]
        assert r.description.strip(), r.label
