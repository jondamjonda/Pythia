"""VL-A-06 + VL-D-09 (WS3 step 24) — ledger provenance writers + header hardening.

Pins: the ledger rows carry non-empty sq_ids/intake_fact_ids derived from the intake artifact; the header
carries pre_assembly_draft_sha256 + the schema version_pins; the intake trace carries intake_schema:1; and
with no intake supplied the provenance is empty (byte-identical to the pre-step-24 ledger).
"""

from __future__ import annotations

import hashlib

from pythia.reasoning.claim_ledger import build_ledger, to_audit_dict
from pythia.reasoning.claim_ledger import Claim
from pythia.reasoning.intake import IntakeFact, IntakeRecord

_INTAKE = {
    "sub_questions": ["Jaké má zaměstnavatel oznamovací povinnosti při prvním zaměstnanci?"],
    "facts": [{"field": "legal_form", "value": "s.r.o.", "evidence_quote": "s.r.o."}],
}


def _claim(text, cid="c0"):
    return Claim(
        id=cid,
        span=(0, len(text)),
        text=text,
        slot="povinnosti",
        type="legal_rule",
        type_source="regex",
        chips=[{"provision_id": "P1"}],
        normative=True,
    )


def test_rows_carry_sq_ids_and_intake_fact_ids():
    claims = [_claim("Zaměstnavatel má oznamovací povinnost jako s.r.o.")]
    led = build_ledger(claims, {"c0": []}, "shadow", intake=_INTAKE)
    row = led["rows"][0]
    assert row.sq_ids == ["sq0"]  # ≥2 shared content tokens with sub-question 0
    assert row.intake_fact_ids == ["legal_form"]  # the fact value 's.r.o.' appears in the claim


def test_no_intake_leaves_provenance_empty():
    claims = [_claim("Nějaké tvrzení bez překryvu.")]
    led = build_ledger(claims, {"c0": []}, "shadow", intake=None)
    assert led["rows"][0].sq_ids == [] and led["rows"][0].intake_fact_ids == []


def test_header_hardening_sha256_and_version_pins():
    draft = "## Legislativní rámec\nDepozitář musí oznámit údaje [X § 1]."
    led = build_ledger([_claim("x")], {"c0": []}, "shadow", pre_assembly_draft=draft)
    summ = led["summary"]
    assert summ["pre_assembly_draft_sha256"] == hashlib.sha256(draft.encode("utf-8")).hexdigest()
    assert summ["version_pins"]["ledger_schema"] == "claim_ledger_v1"
    assert summ["version_pins"]["intake_schema"] == 1
    # round-trips through to_audit_dict (the serialized artifact the lawyer reviews)
    audit = to_audit_dict(led)
    assert audit["summary"]["pre_assembly_draft_sha256"] == summ["pre_assembly_draft_sha256"]
    assert audit["rows"][0]["sq_ids"] == [] and "intake_fact_ids" in audit["rows"][0]


def test_no_draft_leaves_sha_none():
    led = build_ledger([_claim("x")], {"c0": []}, "shadow")
    assert led["summary"]["pre_assembly_draft_sha256"] is None


def test_intake_trace_carries_schema_pin():
    rec = IntakeRecord(
        sub_questions=["q"],
        jurisdiction=["CZ"],
        matter_type="employment",
        facts=[IntakeFact(field="legal_form", value="s.r.o.", evidence_quote="s.r.o.")],
    )
    assert rec.as_trace()["intake_schema"] == 1
