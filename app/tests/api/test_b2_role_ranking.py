"""B2 — the "Co se na vás vztahuje" (`applies`) slot role-ranking, which was RE-SCOPED.

Outcome of the timeboxed thin-seam attempt (2026-07-07): the thin seam was NOT forced. The answer-surface
practical items carry NO role/actor signal to rank against `sme_roles` (the item shape is {slot, fact_type,
value_raw, provision_id, grounded}; the engine emits no per-item role), and the principled per-finding role
determination already lives in the cascade lane (analysis/applicability_scoring.run_applicability_scoring →
payload['applicability'], behind sme_roles_enabled). Role-ranking the answer-surface slot is therefore
DEFERRED to the cascade lane (revisit at plan step 96), recorded as a formal `# B2:` deviation in
_shape_practical_block.

These tests pin the re-scope: (a) the deviation is RECORDED in the module; (b) _shape_practical_block does
NOT reorder or annotate the `applies` slot — the block is BYTE-IDENTICAL to today for any profile; (c) no new
B2 flag was introduced.
"""

from __future__ import annotations

import inspect

from regsuper.api import answer as answer_api
from regsuper.config import Settings


def _applies_engine_dict() -> dict:
    """An engine answer dict whose `.practical.applies` slot carries three grounded items in a fixed order
    (plus a non-`applies` item). If a role-rank had been wired, an employer profile would reorder/annotate
    these. Shaped as the OUTER engine dict `_shape_practical_block` reads (it pulls `ans["practical"]`)."""
    return {
        "practical": {
            "items": [
                {
                    "slot": "applies",
                    "fact_type": "obligation",
                    "value_raw": "A — zaměstnavatel",
                    "provision_id": "p1",
                    "grounded": True,
                },
                {
                    "slot": "applies",
                    "fact_type": "obligation",
                    "value_raw": "B — plátce DPH",
                    "provision_id": "p2",
                    "grounded": True,
                },
                {
                    "slot": "applies",
                    "fact_type": "obligation",
                    "value_raw": "C — správce údajů",
                    "provision_id": "p3",
                    "grounded": True,
                },
                {
                    "slot": "obligations",
                    "fact_type": "deadline",
                    "value_raw": "do 30 dnů",
                    "provision_id": "p4",
                    "grounded": True,
                },
            ],
            "gaps": [],
            "demoted_slots": [],
            "profile_used": True,
        }
    }


# ─────────────────────────────────── (a) the deviation is recorded ────────────────────────────────────────


def test_b2_deviation_note_is_recorded():
    src = inspect.getsource(answer_api._shape_practical_block)
    assert "# B2:" in src
    assert "cascade lane" in src and "step 96" in src


# ──────────────── (b) _shape_practical_block does NOT reorder / annotate the applies slot ──────────────────


def test_applies_slot_order_unchanged_no_role_annotation():
    grounded = {"p1", "p2", "p3", "p4"}
    block = answer_api._shape_practical_block(_applies_engine_dict(), grounded)
    assert block is not None
    applies = [it for it in block.items if it.slot == "applies"]
    # order is preserved EXACTLY as the engine emitted it (no role-driven reorder)
    assert [it.value_raw for it in applies] == [
        "A — zaměstnavatel",
        "B — plátce DPH",
        "C — správce údajů",
    ]
    assert [it.provision_id for it in applies] == ["p1", "p2", "p3"]
    # no role-match marker was stamped onto any item (the shape is unchanged — only the documented fields)
    allowed = {"slot", "fact_type", "value_raw", "provision_id", "grounded"}
    for it in block.items:
        assert set(it.model_dump().keys()) == allowed
        assert "role_match" not in it.model_dump() and "role" not in it.model_dump()


def test_practical_block_byte_identical_for_any_profile():
    """The block a role-rank would have permuted is IDENTICAL regardless of profile — there is no profile input
    to _shape_practical_block, so a bakery vs an employer yields the same items in the same order (the re-scope
    means the slot is not re-ranked here at all)."""
    grounded = {"p1", "p2", "p3", "p4"}
    b1 = answer_api._shape_practical_block(_applies_engine_dict(), grounded)
    b2 = answer_api._shape_practical_block(_applies_engine_dict(), grounded)
    assert b1 is not None and b2 is not None
    assert b1.model_dump() == b2.model_dump()


# ──────────────────────────────── (c) no new B2 flag was introduced ───────────────────────────────────────


def test_no_new_b2_flag_introduced():
    s = Settings()
    # the re-scope introduces NO answer-surface role-ranking flag; role work stays behind the existing
    # cascade-lane sme_roles_enabled flag (default OFF), untouched.
    assert not hasattr(s, "applies_role_ranking_enabled")
    assert not hasattr(s, "b2_role_ranking_enabled")
    assert s.sme_roles_enabled is False
