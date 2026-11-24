"""Phase-C M3 — the CONFIDENCE-BAND presentation grade tests.

Every test is PURE (no LLM / DB / model / server). They prove the cardinal M3 invariants:
  • the golden band table at every edge — `full` requires BOTH dials ≥ T_full; otherwise `hedged`;
  • grade() NEVER returns "refuse"/"closest"/"notlegal" on its own — those arrive ONLY via the meta route
    (mapped through) or, for "refuse", the post-generate verify net at the answer.py call site;
  • a non-sound meta route is MAPPED to its render-mode with a None hedge (bands do not grade a non-answer);
  • the dials are tolerant — None / negative / >1 / NaN clamp to the cautious side, never a false `full`;
  • the band only decides presentation — it returns no verdict / cited-set / refuse decision;
  • the FLAT (metagate-OFF) answer.py path leaves band/render_mode/hedge = None → byte-identical for a
    consumer that ignores the new fields.
"""

from __future__ import annotations

import math

import pytest

from pythia.config import PythiaSettings
from pythia.contract import MetaVerdict, PythiaAnswer
from pythia.reasoning.bands import grade

# Conservative fallbacks (JUDGMENT §3.4) — the defaults grade() uses unless overridden.
_T_FULL, _T_HEDGE = 0.60, 0.40
_SOUND = MetaVerdict(route="sound", intent="legal", coverage="covered", premise="sound")


# ════════════════════════════════════════════════════════════════════════════════════════════════════════
# 1) the golden band-edge table — full requires BOTH dials ≥ T_full; otherwise hedged.
# ════════════════════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "c, r, expected",
    [
        # both at/above the full floor → full (the boundary 0.60 is INCLUSIVE).
        (0.60, 0.60, "full"),
        (0.90, 0.90, "full"),
        (0.60, 1.00, "full"),
        (1.00, 0.60, "full"),
        # exactly one dial below T_full → hedged (the AND requirement).
        (0.59, 0.90, "hedged"),  # C just under the edge
        (0.90, 0.59, "hedged"),  # R just under the edge (grounded-but-thin)
        (0.59, 0.59, "hedged"),  # both just under
        # the hedge floor band and below — still hedged (a cleared answer is always presentable).
        (0.40, 0.40, "hedged"),
        (0.00, 0.00, "hedged"),
        (0.50, 0.50, "hedged"),
    ],
)
def test_golden_band_table(c, r, expected) -> None:
    g = grade(c, r, _SOUND)
    assert g["band"] == expected and g["render_mode"] == expected
    # the amber banner is present iff hedged.
    if expected == "hedged":
        assert g["hedge"] is not None and g["hedge"]["level"] == "amber" and g["hedge"]["copy"].strip()
    else:
        assert g["hedge"] is None


def test_full_needs_both_dials_strictly() -> None:
    """The AND is genuine: dropping EITHER dial below the floor demotes a full to hedged."""
    assert grade(0.61, 0.61, _SOUND)["band"] == "full"
    assert grade(0.39, 0.99, _SOUND)["band"] == "hedged"
    assert grade(0.99, 0.39, _SOUND)["band"] == "hedged"


def test_custom_thresholds_move_the_edge() -> None:
    """The thresholds are parameters (config-fed at the call site); raising them demotes a previously-full
    grade — proving the edges are presentation knobs, not hard-coded."""
    assert grade(0.62, 0.62, _SOUND, t_full=0.60)["band"] == "full"
    assert grade(0.62, 0.62, _SOUND, t_full=0.70)["band"] == "hedged"  # same answer, stricter edge → hedged


# ════════════════════════════════════════════════════════════════════════════════════════════════════════
# 2) grade() NEVER invents refuse/closest/notlegal — those come only via the meta route (mapped through).
# ════════════════════════════════════════════════════════════════════════════════════════════════════════
def test_grade_only_emits_full_or_hedged_for_sound() -> None:
    """For a SOUND meta (or meta=None), grade() emits EXACTLY one of full/hedged — never refuse/closest/
    notlegal, no matter the dial values."""
    for c in (0.0, 0.39, 0.40, 0.59, 0.60, 0.99, 1.0):
        for r in (0.0, 0.40, 0.60, 1.0):
            for m in (_SOUND, None):
                band = grade(c, r, m)["band"]
                assert band in ("full", "hedged")


@pytest.mark.parametrize(
    "route, expected_rm",
    [
        ("not_a_legal_question", "notlegal"),
        ("not_in_corpus", "closest"),
    ],
)
def test_nonsound_route_maps_through(route, expected_rm) -> None:
    """A non-sound gateway route is MAPPED to its render-mode (never re-decided), with a None hedge — bands
    do not grade a non-answer. The dial values are ignored on this branch."""
    m = MetaVerdict(route=route)
    g = grade(0.99, 0.99, m)  # high dials must NOT turn a non-answer into `full`
    assert g["band"] == expected_rm and g["render_mode"] == expected_rm
    assert g["hedge"] is None


def test_false_premise_route_is_graded_not_framed() -> None:
    """false_premise is a GENERATED, grounded, verified answer (the correction-led GENERATE) — it is a
    cleared answer and is graded full/hedged like sound, NOT mapped to a static frame (the app keys the
    correction banner off meta.route separately)."""
    m = MetaVerdict(route="false_premise", intent="legal", coverage="covered", premise="false_premise")
    assert grade(0.90, 0.90, m)["band"] == "full"
    assert grade(0.30, 0.90, m)["band"] == "hedged"


# ════════════════════════════════════════════════════════════════════════════════════════════════════════
# 3) the dials are tolerant — None / negative / >1 / NaN clamp to the cautious side (never a false full).
# ════════════════════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("c, r", [(None, 0.99), (0.99, None), (None, None)])
def test_none_dial_is_cautious(c, r) -> None:
    """A None dial clamps to 0.0 → pushes toward hedged, never a falsely-confident full."""
    assert grade(c, r, _SOUND)["band"] == "hedged"


def test_negative_and_overrange_and_nan_clamp() -> None:
    assert grade(-1.0, 0.99, _SOUND)["band"] == "hedged"  # negative C → 0.0
    assert grade(0.99, -0.5, _SOUND)["band"] == "hedged"  # negative R → 0.0
    assert grade(5.0, 5.0, _SOUND)["band"] == "full"  # >1 clamps to 1.0 (both ≥ floor → full)
    assert grade(math.nan, 0.99, _SOUND)["band"] == "hedged"  # NaN → cautious 0.0
    assert grade(0.99, math.nan, _SOUND)["band"] == "hedged"
    assert grade("oops", 0.99, _SOUND)["band"] == "hedged"  # unparseable → cautious 0.0


def test_hedge_banner_is_a_fresh_dict() -> None:
    """Each hedged grade returns its OWN banner dict so a caller cannot mutate a shared object."""
    a = grade(0.0, 0.0, _SOUND)["hedge"]
    b = grade(0.0, 0.0, _SOUND)["hedge"]
    assert a == b and a is not b


# ════════════════════════════════════════════════════════════════════════════════════════════════════════
# 4) config — conservative fallbacks present and default-safe.
# ════════════════════════════════════════════════════════════════════════════════════════════════════════
def test_band_thresholds_default_to_conservative_fallbacks() -> None:
    s = PythiaSettings()
    assert s.band_full_floor == 0.60 and s.band_hedge_floor == 0.40
    # the bands are gated behind metagate, which stays OFF by default → byte-identical.
    assert s.metagate_enabled is False


# ════════════════════════════════════════════════════════════════════════════════════════════════════════
# 5) FLAT path byte-identical — metagate OFF leaves band/render_mode/hedge None; a consumer ignoring them
#    sees today's PythiaAnswer exactly.
# ════════════════════════════════════════════════════════════════════════════════════════════════════════
def test_default_answer_has_none_band_fields() -> None:
    """A PythiaAnswer built without the M3 fields (the metagate-OFF path) carries None band/render_mode/hedge
    — so a consumer that ignores them is byte-identical to pre-M3."""
    a = PythiaAnswer(verdict="ok", drafted_answer="x", confidence=0.9)
    assert a.band is None and a.render_mode is None and a.hedge is None


async def test_answer_off_path_leaves_band_none(monkeypatch) -> None:
    """End-to-end through answer.py with metagate_enabled=False: the band fields stay None (the M3 stamp is
    skipped) → byte-identical for a consumer that ignores them. Pipeline fully stubbed (no LLM/DB/model)."""
    import pythia.reasoning.answer as answer_mod

    s = PythiaSettings()
    assert s.metagate_enabled is False
    monkeypatch.setattr(answer_mod, "get_settings", lambda: s, raising=True)

    def _grade_spy(*a, **k):  # bands.grade must NOT be called on the OFF path
        raise AssertionError("bands.grade must not be called when metagate_enabled is False")

    monkeypatch.setattr(answer_mod, "grade_band", _grade_spy, raising=True)

    class _LLM:
        async def available(self):
            return True

        async def json(self, prompt, schema, *, tier="cheap", stage=None):
            if "planning step" in prompt:
                return {"sub_queries": ["q"], "jurisdiction": ["CZ"], "matter_type": "x"}
            return {"unsupported": []}

        async def text(self, prompt, *, system=None, tier="generate", stage=None):
            return "## Legislativní rámec\nPodle [240/2013 § 297] platí povinnost.\n## Hodnocení\nOK."

    monkeypatch.setattr(answer_mod, "get_llm", lambda tier: _LLM(), raising=True)

    class _Eng:
        async def retrieve(self, q, *, method="cte"):
            from pythia.contract import CitedProvision as _CP

            return type("R", (), {"cited_provisions": [_CP(provision_id="p1", label="§ 297", score=0.82)]})()

    monkeypatch.setattr(answer_mod, "PythiaEngine", lambda: _Eng(), raising=True)

    async def _fetch_stub(ids, *, as_of=None, render_asof=False, ctx_window=False, terms=None):
        return {
            "p1": {
                "id": "p1",
                "label": "§ 297",
                "ext": "240/2013",
                "title": "ZISIF",
                "jurisdiction": "CZ",
                "txt": "Depozitář …",
                "bindingness": "hard-law",
                "is_authoritative": True,
                "temporal_status": "in-force",
                "legal_hierarchy": "STATUTE",
                "pit_status": None,
                "vector_stale": None,
            }
        }

    monkeypatch.setattr(answer_mod, "_fetch", _fetch_stub, raising=True)

    res = await answer_mod.answer("Jaké povinnosti má depozitář podle § 297?", method="cte")
    assert res.band is None and res.render_mode is None and res.hedge is None
