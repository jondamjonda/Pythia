"""B1a-HARDEN — EU Article section-kind classifier (pure, DB-free → in the `-m "not live"` gate).

Locks the per-family adapter: the title-finder reads the `*italic*` heading off a real EU Article body, and
the UNIVERSAL marker table maps it to the right kind. Examples are REAL GDPR (32016R0679) article titles."""

from __future__ import annotations

from pythia.graph.section_kind import (
    KIND_DEFINITIONS,
    KIND_OPERATIVE,
    KIND_PENALTIES,
    KIND_PREAMBLE,
    KIND_TRANSITIONAL,
)
from pythia.graph.section_kind_eu import classify_eu_title, eu_title, plan_eu


def test_eu_title_reads_the_italic_heading():
    body = "## Article 4\n\n*Definitions*\n\nFor the purposes of this Regulation:\n\n- (1) 'personal data'…"
    assert eu_title(body) == "Definitions"


def test_eu_title_empty_when_absent():
    assert eu_title("## Article 7\n\nNo italic heading here, just body text.") == ""
    assert eu_title(None) == ""
    assert eu_title("") == ""


def test_real_gdpr_titles_map_to_the_right_kind():
    # the exact titles in the live corpus (verified 2026-06-24)
    assert classify_eu_title("Definitions") == KIND_DEFINITIONS
    assert classify_eu_title("Penalties") == KIND_PENALTIES
    assert classify_eu_title("General conditions for imposing administrative fines") == KIND_PENALTIES
    assert classify_eu_title("Entry into force and application") == KIND_TRANSITIONAL
    # hyphen normalisation makes the 'subject matter' phrase marker fire on GDPR's 'Subject-matter'
    assert classify_eu_title("Subject-matter and objectives") == KIND_PREAMBLE
    # a substantive operative article matches no marker → the binding default, never a downgrade
    assert classify_eu_title("Principles relating to processing of personal data") == KIND_OPERATIVE
    assert classify_eu_title("Lawfulness of processing") == KIND_OPERATIVE


def test_no_title_defaults_operative():
    # the SAFE default: no evidence ⇒ operative (binding), never a silent downgrade to interpretive.
    assert classify_eu_title("") == KIND_OPERATIVE


def test_plan_eu_carries_an_auditable_source():
    rows = [{"id": "a1", "text": "## Article 4\n\n*Definitions*\n\nFor the purposes of…"}]
    [plan] = plan_eu(rows)
    assert plan["id"] == "a1"
    assert plan["section_kind"] == KIND_DEFINITIONS
    assert "Definitions" in plan["kind_source"] and plan["kind_source"].startswith("b1a08eu")
