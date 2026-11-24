"""WS3 step 21 — THE evidence-dict wire (Blocker B3). Pins that answer._claim_evidence_extra builds the
fact_rows / intake_facts / profile_context / provision_temporal evidence the claim verifiers were written to
read but were never fed — and that feeding it makes "verified" mean more than "cited-only":

  • V-FACT verifies a deadline whose value lives ONLY in a fact row (beyond the 700-char cited slice).
  • V-PROF can act on the profile the wire delivers (an asker-fact misstatement is demoted vf_profile).
  • V-SAFE's temporal lane receives per-provision pit/temporal status.
  • Substrate-absence is safe (a fact_lookup failure yields no rows, never a crash).
"""

from __future__ import annotations

import pytest

from pythia.reasoning import answer as A
from pythia.reasoning import fact_lookup, v_fact, v_prof
from pythia.reasoning.claim_ledger import Claim
from pythia.reasoning.fact_lookup import FactRow


class _S:
    claim_verify_enabled = True
    claim_verify_mode = "shadow"


def _fact(value_raw: str, fact_type: str = "deadline") -> FactRow:
    return FactRow(
        fact_type=fact_type,
        value_raw=value_raw,
        char_start=0,
        char_end=len(value_raw),
        value_norm=None,
        extraction_method="regex",
        source="regex",
    )


class _Rec:
    """A minimal IntakeRecord stand-in carrying .facts (dict facts are accepted by V-PROF)."""

    def __init__(self, facts):
        self.facts = facts


# ─────────────────────────────── the builder ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extra_builds_all_four_keys(monkeypatch):
    async def _fake_lookup(pid, fact_type=None):
        return [_fact("8 dnů")] if pid == "P1" else []

    monkeypatch.setattr(fact_lookup, "lookup", _fake_lookup)

    meta = {
        "P1": {"pit_status": "IN_FORCE", "temporal_status": None},
        "P2": {"pit_status": "SUPERSEDED", "temporal_status": "repealed"},
    }
    rec = _Rec([{"field": "legal_form", "value": "sro", "source": "profile"}])
    profile = {"legal_form": "sro", "headcount_band": "1"}

    extra = await A._claim_evidence_extra(["P1", "P2"], meta, rec, profile)

    assert extra["fact_rows"] == {"P1": [_fact("8 dnů")]}  # only P1 had rows
    assert extra["intake_facts"] == rec.facts
    assert extra["profile_context"] == profile
    assert extra["provision_temporal"]["P1"] == {"pit_status": "IN_FORCE", "temporal_status": None}
    assert extra["provision_temporal"]["P2"] == {"pit_status": "SUPERSEDED", "temporal_status": "repealed"}


@pytest.mark.asyncio
async def test_extra_is_substrate_absence_safe(monkeypatch):
    async def _boom(pid, fact_type=None):
        raise RuntimeError("practical_fact table absent")

    monkeypatch.setattr(fact_lookup, "lookup", _boom)
    extra = await A._claim_evidence_extra(["P1"], {"P1": {"pit_status": None, "temporal_status": None}}, None, None)
    assert extra["fact_rows"] == {}  # no crash, empty
    assert extra["intake_facts"] == [] and extra["profile_context"] is None


@pytest.mark.asyncio
async def test_extra_handles_no_intake_no_profile(monkeypatch):
    monkeypatch.setattr(fact_lookup, "lookup", lambda pid, fact_type=None: _noop())
    extra = await A._claim_evidence_extra([], {}, None, "not-a-dict")
    assert extra == {"fact_rows": {}, "intake_facts": [], "profile_context": None, "provision_temporal": {}}


async def _noop():
    return []


# ─────────────────────────────── the wire lights the verifiers ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fact_row_only_deadline_verifies_via_the_wire(monkeypatch):
    # THE canary: the deadline value 'do 8 dnů' is NOT in the 700-char cited slice, only in a fact row.
    # Before the wire, evidence['fact_rows'] was never populated → V-FACT could only see cited_texts →
    # a beyond-700 deadline was unverifiable. Now the wire feeds fact_rows → V-FACT verifies it.
    async def _fake_lookup(pid, fact_type=None):
        return [_fact("do 8 dnů")]

    monkeypatch.setattr(fact_lookup, "lookup", _fake_lookup)

    extra = await A._claim_evidence_extra(
        ["P1"], {"P1": {"pit_status": "IN_FORCE", "temporal_status": None}}, None, None
    )
    evidence = {
        "cited_texts": {"P1": "Toto ustanovení mluví o oznamovací povinnosti bez konkrétní lhůty."},
        "chips": [],
        **extra,
    }
    claim = Claim(
        id="c0",
        span=(0, 26),
        text="Oznámení podejte do 8 dnů.",
        slot="lhuty",
        type="deadline",
        type_source="regex",
        chips=[{"provision_id": "P1", "start": 0, "end": 4}],
        normative=True,
    )

    verdicts = v_fact.verify(claim, evidence, _S())
    v = next(x for x in verdicts if x.check == "v_fact")
    assert v.status == "pass"  # verified via the fact row, not cited_texts


@pytest.mark.asyncio
async def test_profile_wire_lets_v_prof_demote_misstatement(monkeypatch):
    # The wire delivers profile_context/intake_facts → V-PROF can now act (it SKIPs when neither is present).
    async def _fake_lookup(pid, fact_type=None):
        return []

    monkeypatch.setattr(fact_lookup, "lookup", _fake_lookup)
    extra = await A._claim_evidence_extra(
        [], {}, _Rec([{"field": "legal_form", "value": "sro", "source": "profile"}]), {"legal_form": "sro"}
    )
    evidence = {"cited_texts": {}, "chips": [], **extra}
    # a claim asserting the WRONG legal form for an s.r.o. record
    claim = Claim(
        id="c0",
        span=(0, 40),
        text="Jako akciová společnost máte tuto povinnost.",
        slot="situace",
        type="situation_application",
        type_source="regex",
        chips=[],
        normative=True,
    )
    verdicts = v_prof.verify(claim, evidence, _S())
    v = next(x for x in verdicts if x.check == "v_prof")
    # the wire delivered facts, so V-PROF did NOT skip-for-no-facts; the misstatement is a fail (vf_profile)
    assert v.status == "fail"
    assert v.evidence.get("gap_source") == "vf_profile"
