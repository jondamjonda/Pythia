"""VL-A-12 (WS4 step 34) — app-side intake.* telemetry: prefix registration + friendly templates.

The four intake events route to the polarius control-room module and render human copy (not the raw code).
Emitted from the answer handler (clarify returned / context attached / conflict / dropped) — counts only.
"""

from __future__ import annotations

from regsuper.observability import events

_CODES = (
    "intake.clarify.returned",
    "intake.context.attached",
    "intake.conflict",
    "intake.facts.dropped",
)


def test_intake_prefix_routes_to_polarius():
    for code in _CODES:
        assert events._module_for_code(code) == "polarius"


def test_all_intake_templates_registered():
    for code in _CODES:
        assert code in events.FRIENDLY_TEMPLATES


def test_intake_templates_render_counts_not_raw_code():
    assert (
        events._render("intake.conflict", {"n_conflicts": 2})
        == "Intake conflict on 2 field(s) (profile vs query)."
    )
    assert (
        events._render("intake.facts.dropped", {"n_dropped": 3})
        == "Intake verifier dropped 3 proposed fact(s)."
    )
    r = events._render("intake.context.attached", {"n_facts": 5})
    assert r != "intake.context.attached" and "5" in r
    r2 = events._render("intake.clarify.returned", {"matter_type": "employment", "n_missing": 2})
    assert "employment" in r2 and "2" in r2
