"""VL-A-02 (WS4 step 33, engine half) — cross-repo intake-contract parity.

The engine's intake trace is consumed by the app's understanding-panel shaper. The keys/enum the app depends
on are MIRRORED here as a canonical contract (the SAME literals the app pins in
Polarius/tests/api/test_iv_enum_parity.py). If the engine renames a status, a verdict key, or the conflict
value keys, THIS test fails — catching the drift before it becomes a silent app-side empty panel (VL-A-10).
Also re-asserts the engine's own IV-ENUM vocab ≡ intake._FACT_FIELDS parity guard.
"""

from __future__ import annotations

# ── The canonical intake-trace contract (KEEP IN LOCKSTEP with Polarius/tests/api/test_iv_enum_parity.py) ──
CONFLICT_VALUE_KEYS = ("profile_value", "query_value")
VERDICT_KEYS = {"item_id", "verifier", "status", "reason"}
IV_STATUSES = ("pass", "drop", "conflict", "advisory")


def test_iv_status_matches_contract():
    from pythia.reasoning.intake_verify import IV_STATUS

    assert tuple(IV_STATUS) == IV_STATUSES


def test_ivverdict_as_dict_has_contract_keys():
    from pythia.reasoning.intake_verify import IVVerdict

    d = IVVerdict("fact:0", "IV-ENUM", "drop", "reason_code", {"field": "legal_form"}).as_dict()
    assert VERDICT_KEYS <= set(d)  # (+ optional evidence)
    assert d["status"] in IV_STATUSES


def test_profile_conflict_shape_matches_contract():
    # the engine records a profile conflict as {field, profile_value, query_value} (intake.py).
    from pythia.reasoning.intake import IntakeRecord

    rec = IntakeRecord(
        sub_questions=["q"],
        jurisdiction=["CZ"],
        matter_type="employment",
        profile_conflicts=[{"field": "legal_form", "profile_value": "sro", "query_value": "as"}],
    )
    conflict = rec.as_trace()["profile_conflicts"][0]
    assert set(CONFLICT_VALUE_KEYS) <= set(conflict) and "field" in conflict


def test_iv_enum_vocab_equals_fact_fields():
    from pythia.reasoning.intake import _FACT_FIELDS
    from pythia.reasoning.intake_verify import _ENUM_VOCAB

    assert _ENUM_VOCAB == _FACT_FIELDS  # the engine's own IV-ENUM parity guard
