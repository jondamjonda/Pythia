"""VL-A-02 (WS4 step 33, app half) — cross-repo intake-contract parity.

The app's understanding-panel shaper consumes the engine's intake trace. The keys/enum it depends on are
MIRRORED here as a canonical contract; the engine repo pins the SAME literals in tests/test_iv_enum_parity.py.
Drift on either side fails that side's CI — the absence of this pin is exactly what let the VL-A-10 fixture
theater (a `conflicts` key the engine never emitted, an iv_verdicts shape the engine never had) go unnoticed.
"""

from __future__ import annotations

from regsuper.api import answer as answer_api

# ── The canonical intake-trace contract (KEEP IN LOCKSTEP with PythiaRAG/Build/tests/test_iv_enum_parity.py) ──
CONFLICTS_KEY = "profile_conflicts"
CONFLICT_VALUE_KEYS = ("profile_value", "query_value")
VERDICTS_KEY = "iv_verdicts"
IV_STATUSES = ("pass", "drop", "conflict", "advisory")
DROP_STATUS = "drop"


def test_app_conflict_value_keys_match_contract():
    # the shaper lifts a conflict's candidate values from EXACTLY the engine's two keys
    assert answer_api._CONFLICT_SCALAR_KEYS == CONFLICT_VALUE_KEYS


def test_app_shaper_reads_profile_conflicts_and_drop_status():
    # behavioural pin: a trace using the CONTRACT keys populates conflicts[] + dropped[]; the wrong keys don't.
    real = {
        "audit_trace": {
            "intake": {
                CONFLICTS_KEY: [
                    {"field": "legal_form", "profile_value": "sro", "query_value": "as"}
                ],
                VERDICTS_KEY: [
                    {
                        "item_id": "fact:0",
                        "verifier": "IV-ENUM",
                        "status": DROP_STATUS,
                        "reason": "x",
                        "evidence": {"field": "legal_form"},
                    }
                ],
            }
        }
    }
    from types import SimpleNamespace
    from unittest.mock import patch

    settings = answer_api.get_settings().model_copy(update={"answer_intake_panel_enabled": True})
    with patch.object(answer_api, "get_settings", lambda: settings):
        out = answer_api._shape_intake(real)
    assert out is not None and len(out.conflicts) == 1 and len(out.dropped) == 1
    _ = SimpleNamespace  # (kept import-light)

    # the OLD (wrong) keys the engine never emits ⇒ nothing surfaces (proves the contract keys are load-bearing)
    wrong = {
        "audit_trace": {
            "intake": {
                "conflicts": [{"field": "legal_form", "values": ["a", "b"]}],
                VERDICTS_KEY: [{"field": "x", "verdict": "drop"}],
            }
        }
    }
    with patch.object(answer_api, "get_settings", lambda: settings):
        out2 = answer_api._shape_intake(wrong)
    assert out2 is not None and not out2.conflicts and not out2.dropped
