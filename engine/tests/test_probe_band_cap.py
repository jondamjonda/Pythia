"""WS5 step 48(e) — VL-E-08 BAND-CAP repo pin (promoted from the throwaway scratchpad probe).

The band cap (claim_repair.band_cap / any_core_demoted, applied in answer.py under metagate-ON + claim_verify
enforce) is DOWNGRADE-ONLY: when a CORE claim is demoted or safe_dropped but at least one core claim survives,
the presentation band is forced from 'full' to 'hedged' (+ the hedge banner) — it NEVER upgrades a band and
NEVER manufactures a refusal (the verify net / decide_refusal own those). This suite pins that branch, which
was UNTESTED at the answer() level (the enforce suite pins the loop pieces; this pins the band interaction).

Reuses the shared enforce `_harness`, extended here with a stubbed SOUND metagate verdict (band-cap fires only
under metagate-ON) — the band exists only when metagate is on, so this is the honest way to reach the branch.
"""

from __future__ import annotations

import pytest

import pythia.reasoning.answer as ans
from pythia.config import PythiaSettings
from pythia.contract import MetaVerdict

from tests.test_answer_enforce import _GOOD_FULL, _Q, _harness

pytestmark = pytest.mark.asyncio


def _harness_metagate(monkeypatch, settings, *, draft: str, full_text: str) -> None:
    """The enforce `_harness` + a stubbed SOUND metagate classification (band-cap fires only under metagate)."""
    _harness(monkeypatch, settings, draft=draft, full_text=full_text)

    async def _sound_meta(query, decomp, meta, ordered_ids, rrf, raw, cos=None):
        return MetaVerdict(
            route="sound",
            intent="legal",
            intent_confidence=0.99,
            coverage="covered",
            coverage_confidence=0.99,
            premise="sound",
            premise_confidence=0.99,
            reason="band-cap probe",
        )

    monkeypatch.setattr(ans, "classify_meta", _sound_meta, raising=True)


# A MIXED draft: sentence 1 (an obligation) is SUPPORTED by _GOOD_FULL and survives verified; sentence 2 (a
# sanction the source never states) is cited-but-unsupported ⇒ demoted. Not all-core-fail (one survives) ⇒ the
# loop does NOT refuse — exactly the state the band cap is for (a surviving-but-hedged answer).
_MIXED_DRAFT = (
    "## Legislativní rámec\n"
    "Depozitář musí oznámit údaje podle [240/2013 § 297].\n"
    "Podnikatel zaplatí pokutu milion korun okamžitě hejtmanovi kraje [240/2013 § 297].\n"
    "## Hodnocení\nOK."
)
# An all-supported draft: no core claim is demoted ⇒ the band cap does NOT fire (band stays whatever grade()
# assigned) ⇒ the metagate/band path is byte-identical to the no-cap case.
_GOOD_DRAFT = "## Legislativní rámec\nDepozitář musí oznámit údaje podle [240/2013 § 297].\n## Hodnocení\nOK."


async def test_band_cap_forces_hedged_on_a_demoted_core(monkeypatch):
    """VL-E-08: a demoted CORE claim (one core survives) ⇒ band forced to 'hedged' + hedge banner, and the
    answer is STILL SERVED (never refused, never upgraded). The withheld claim's text is excised."""
    s = PythiaSettings(metagate_enabled=True, claim_verify_enabled=True, claim_verify_mode="enforce")
    _harness_metagate(monkeypatch, s, draft=_MIXED_DRAFT, full_text=_GOOD_FULL)
    res = await ans.answer(_Q, method="cte")
    assert not res.refused  # downgrade-only — never refuses on its own
    assert res.band == "hedged" and res.render_mode == "hedged"
    assert res.hedge  # the hedge banner is stamped
    # the surviving core claim is kept verbatim; the demoted sanction claim is excised from the served answer.
    assert "Depozitář musí oznámit údaje podle" in res.drafted_answer
    assert "milion korun" not in res.drafted_answer
    led = res.audit_trace["claim_ledger"]
    assert led["summary"]["mode"] == "enforce"
    assert any(
        r["final"] in ("demoted_gap", "dropped_safety", "dropped_unverifiable") for r in led["rows"] if r["normative"]
    )


async def test_band_cap_untouched_when_all_core_pass(monkeypatch):
    """No core claim demoted ⇒ the band cap does NOT fire: the answer serves and its band is NOT forced to
    'hedged' by the cap (it keeps grade()'s band — 'full' for a well-grounded answer)."""
    s = PythiaSettings(metagate_enabled=True, claim_verify_enabled=True, claim_verify_mode="enforce")
    _harness_metagate(monkeypatch, s, draft=_GOOD_DRAFT, full_text=_GOOD_FULL)
    res = await ans.answer(_Q, method="cte")
    assert not res.refused
    # every core claim survived ⇒ the cap left the band alone (a well-grounded answer grades 'full').
    assert res.band == "full"
    led = res.audit_trace["claim_ledger"]
    assert not any(
        r["final"] in ("demoted_gap", "dropped_safety", "dropped_unverifiable") for r in led["rows"] if r["normative"]
    )


async def test_band_cap_never_upgrades(monkeypatch):
    """The cap is downgrade-only: it can force 'full' → 'hedged' but must NEVER lift a band. A demoted-core
    answer whose grade is already 'hedged' stays 'hedged' (never promoted to 'full'), and still serves."""
    # Same MIXED draft (one core demoted); band-cap can only hold or lower — assert it is not 'full'.
    s = PythiaSettings(metagate_enabled=True, claim_verify_enabled=True, claim_verify_mode="enforce")
    _harness_metagate(monkeypatch, s, draft=_MIXED_DRAFT, full_text=_GOOD_FULL)
    res = await ans.answer(_Q, method="cte")
    assert not res.refused
    assert res.band != "full"  # a lost core claim can never present as 'full'
