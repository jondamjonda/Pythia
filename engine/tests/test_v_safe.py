"""Track V — V-SAFE verifier unit tests (fast, pure; no LLM / DB / clock / fetch).

Pins:
  • Tier-1 lawful-lane act-on-behalf regex ⇒ 'reserved_advice_claim' FAIL at claim grain (CZ + EN forms).
  • The G14 lawful lane (summaries / procedures / drafting / advice-on-own-matter) is NOT reserved ⇒ PASS.
  • Temporal honesty: asserts-current + a cited provision not-in-force at as_of ⇒ 'temporal_inconsistent' FAIL;
    no currency assertion, or no temporal signal, ⇒ no temporal fire.
  • Lawful-lane wins a tie over temporal (higher severity).
  • Non-normative / 'gap' claim ⇒ 'skip' (never a silent pass).
  • Every normative claim receives EXACTLY ONE V-SAFE verdict.
  • PARITY: the duplicated Tier-1 pattern hash is pinned (drift from the app's canonical list breaks loudly).
  • Determinism + orchestrator integration (V-SAFE fires through verify_claims on every normative claim).
"""

from __future__ import annotations

import hashlib

import pytest

from pythia.reasoning.claim_ledger import Claim, ClaimVerdict
from pythia.reasoning.claim_verify import verify_claims
from pythia.reasoning.v_safe import (
    TIER1_PATTERN_VERSION,
    _RESERVED_ADVICE_RE,
    _is_not_in_force,
    verify,
)

_SETTINGS = object()  # V-SAFE ignores settings (pure/deterministic; no sub-flag LLM lane in V-SAFE)


def _claim(text: str, *, chips=None, normative: bool = True, typ: str = "legal_rule") -> Claim:
    return Claim(
        id="c0",
        span=(0, len(text)),
        text=text,
        slot="legislative",
        type=typ,
        type_source="slot_default",
        chips=list(chips or []),
        normative=normative,
    )


def _only(verdicts: list[ClaimVerdict]) -> ClaimVerdict:
    assert isinstance(verdicts, list) and len(verdicts) == 1, verdicts
    v = verdicts[0]
    assert isinstance(v, ClaimVerdict) and v.check == "v_safe"
    return v


# ───────────────────────────────── lawful-lane: reserved-advice FAIL cases ──────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "Zastupte mě u soudu v tomto řízení.",  # CZ represent me
        "Podejte tu žalobu za mě do konce lhůty.",  # CZ file for me (intervening object, M13)
        "Podejte žalobu mým jménem k soudu.",  # CZ in my name (M13)
        "Zastupte mne před soudem.",  # CZ formal 'mne' (M13)
        "Please represent me before the court.",  # EN represent me
        "File the appeal on my behalf.",  # EN on my behalf
        "You should act for me in this matter.",  # EN act for me
        "Submit this application for me by Friday.",  # EN submit ... for me (intervening object)
    ],
)
def test_reserved_advice_claim_fails(text):
    v = _only(verify(_claim(text), {}, _SETTINGS))
    assert v.status == "fail"
    assert v.reason == "reserved_advice_claim"
    assert v.evidence["matched_signal"]  # the offending span is recorded
    assert v.evidence["pattern_version"] == TIER1_PATTERN_VERSION


# ─────────────────────────────── lawful-lane: G14-ALLOWED information PASS cases ─────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "Podle § 6 živnostenského zákona musíte ohlásit živnost do 15 dnů.",  # abstract rule
        "Postup je následující: vyplňte formulář a podejte jej na úřad.",  # procedure (own action)
        "Můžete si připravit návrh smlouvy podle těchto náležitostí.",  # drafting for the user
        "Ve vašem případě se na vaši s.r.o. vztahuje ohlašovací povinnost.",  # advice on own matter
        "The deadline to register is 15 days from starting the trade.",  # EN abstract
    ],
)
def test_lawful_lane_information_passes(text):
    v = _only(verify(_claim(text), {}, _SETTINGS))
    assert v.status == "pass"
    assert v.reason == "lawful_lane_ok"
    assert v.evidence["pattern_version"] == TIER1_PATTERN_VERSION


# ─────────────────────────────────────── temporal honesty FAIL cases ────────────────────────────────────────
def test_temporal_inconsistent_superseded_pit():
    claim = _claim(
        "Tato úprava aktuálně platí a je účinná dodnes.",
        chips=[{"provision_id": "P1", "start": 0, "end": 5}],
    )
    ev = {
        "provision_temporal": {"P1": {"pit_status": "SUPERSEDED", "temporal_status": "in_force"}},
        "as_of": "2026-07-01",
    }
    v = _only(verify(claim, ev, _SETTINGS))
    assert v.status == "fail"
    assert v.reason == "temporal_inconsistent"
    assert v.evidence["provision_id"] == "P1"
    assert v.evidence["pit_status"] == "SUPERSEDED"
    assert v.evidence["asserted_current"] is True


def test_temporal_inconsistent_repealed_doc():
    claim = _claim(
        "Dle současné právní úpravy je tento zákon v platnosti.",
        chips=[{"provision_id": "P9"}],
    )
    ev = {"provision_temporal": {"P9": {"pit_status": None, "temporal_status": "REPEALED"}}}
    v = _only(verify(claim, ev, _SETTINGS))
    assert v.status == "fail"
    assert v.reason == "temporal_inconsistent"
    assert v.evidence["temporal_status"] == "repealed"


def test_temporal_not_yet_in_force():
    claim = _claim("Tento předpis je účinný.", chips=[{"provision_id": "P3"}])
    ev = {"provision_temporal": {"P3": {"pit_status": "NOT_YET_IN_FORCE"}}}
    v = _only(verify(claim, ev, _SETTINGS))
    assert v.status == "fail"
    assert v.reason == "temporal_inconsistent"


# ─────────────────────────────────────── temporal: does-NOT-fire cases ──────────────────────────────────────
def test_temporal_no_currency_assertion_passes():
    # Provision is superseded BUT the claim makes no current-law assertion (neutral historical statement).
    claim = _claim("Do roku 2023 platila jiná sazba.", chips=[{"provision_id": "P1"}])
    ev = {"provision_temporal": {"P1": {"pit_status": "SUPERSEDED"}}}
    v = _only(verify(claim, ev, _SETTINGS))
    assert v.status == "pass"
    assert v.reason == "lawful_lane_ok"


def test_temporal_no_signal_passes():
    # Asserts currency, but the evidence carries NO temporal signal ⇒ nothing to contradict ⇒ PASS.
    claim = _claim("Tato úprava aktuálně platí.", chips=[{"provision_id": "P1"}])
    v = _only(verify(claim, {"cited_texts": {}, "chips": []}, _SETTINGS))
    assert v.status == "pass"


def test_temporal_in_force_provision_passes():
    claim = _claim("Tento zákon je stále v platnosti.", chips=[{"provision_id": "P1"}])
    ev = {"provision_temporal": {"P1": {"pit_status": "IN_FORCE", "temporal_status": "in_force"}}}
    v = _only(verify(claim, ev, _SETTINGS))
    assert v.status == "pass"


# ─────────────────────────────────── tie-break: lawful-lane beats temporal ───────────────────────────────────
def test_lawful_lane_wins_tie_over_temporal():
    # A claim that BOTH acts-on-behalf AND asserts currency over a superseded provision ⇒ reserved wins.
    claim = _claim(
        "Zastupte mě u soudu; tato úprava aktuálně platí.",
        chips=[{"provision_id": "P1"}],
    )
    ev = {"provision_temporal": {"P1": {"pit_status": "SUPERSEDED"}}}
    v = _only(verify(claim, ev, _SETTINGS))
    assert v.status == "fail"
    assert v.reason == "reserved_advice_claim"


# ───────────────────────────────────────────── skip cases ───────────────────────────────────────────────────
def test_non_normative_claim_skips():
    v = _only(verify(_claim("## Nadpis", normative=False), {}, _SETTINGS))
    assert v.status == "skip"
    assert v.reason == "non_normative"


def test_gap_claim_skips():
    # A 'gap' claim reaching V-SAFE (defensively) ⇒ skip, never a silent pass.
    v = _only(verify(_claim("K tomu nemáme podklad v korpusu.", typ="gap"), {}, _SETTINGS))
    assert v.status == "skip"
    assert v.reason == "non_normative"


# ───────────────────────────────────── determinism + robustness ─────────────────────────────────────────────
def test_deterministic_repeat():
    claim = _claim("Podejte žalobu za mě.", chips=[{"provision_id": "P1"}])
    a = verify(claim, {}, _SETTINGS)
    b = verify(claim, {}, _SETTINGS)
    assert [(v.status, v.reason) for v in a] == [(v.status, v.reason) for v in b]


def test_bad_evidence_shape_does_not_crash():
    # A non-dict evidence must not crash V-SAFE (defensive coercion) — still yields one verdict.
    v = _only(verify(_claim("Tento zákon je účinný."), None, _SETTINGS))  # type: ignore[arg-type]
    assert v.check == "v_safe"


def test_malformed_chip_ignored():
    # A chip without provision_id / a non-dict chip must be skipped safely.
    claim = _claim("Tato úprava aktuálně platí.", chips=["not-a-dict", {"start": 0}])
    ev = {"provision_temporal": {"P1": {"pit_status": "SUPERSEDED"}}}
    v = _only(verify(claim, ev, _SETTINGS))
    assert v.status == "pass"  # no resolvable pid ⇒ no temporal match


# ───────────────────────────────── _is_not_in_force vocab (mirrors _temporal_marker) ────────────────────────
@pytest.mark.parametrize(
    "pit,doc,expected",
    [
        ("SUPERSEDED", None, True),
        ("superseded", None, True),  # case-normalised
        (None, "repealed", True),
        (None, "ANNULLED_BY_CC", True),
        ("NOT_YET_IN_FORCE", None, True),
        (None, "future-effective", True),  # hyphen-normalised
        ("IN_FORCE", "in_force", False),
        (None, None, False),  # no signal ⇒ in force (honest)
        ("", "  ", False),  # blank ⇒ in force
    ],
)
def test_is_not_in_force_vocab(pit, doc, expected):
    assert _is_not_in_force(pit, doc) is expected


# ───────────────────────────────────────────── PARITY pin ───────────────────────────────────────────────────
def test_tier1_pattern_parity_hash_pinned():
    """The duplicated Tier-1 pattern is DUPLICATED VERBATIM from the app's
    `regsuper.analysis.compliance_guard._RESERVED_ADVICE_RE` (engine⊥app ⇒ we cannot import it). The version
    hash is PINNED here: if this copy is edited (drifting from the app's canonical list), the hash changes and
    THIS test fails, forcing a deliberate re-sync + review. If the app's list is intentionally revised, update
    the copy AND this golden hash together."""
    assert TIER1_PATTERN_VERSION == "a4fb7a4a9b57a08a"
    # And the exposed constant is genuinely the sha256[:16] of the compiled pattern's source.
    assert TIER1_PATTERN_VERSION == hashlib.sha256(_RESERVED_ADVICE_RE.pattern.encode("utf-8")).hexdigest()[:16]


# ─────────────────────────────────── orchestrator integration ───────────────────────────────────────────────
def test_v_safe_present_on_each_normative_claim_distinct_ids():
    claims = [
        Claim(
            id="a",
            span=(0, 3),
            text="Podle § 6 musíte ohlásit živnost.",
            slot="legislative",
            type="legal_rule",
            type_source="slot_default",
            chips=[],
            normative=True,
        ),
        Claim(
            id="b",
            span=(0, 3),
            text="Zastupte mě u soudu.",
            slot="legislative",
            type="legal_rule",
            type_source="slot_default",
            chips=[],
            normative=True,
        ),
        Claim(
            id="c",
            span=(0, 3),
            text="## Nadpis",
            slot="legislative",
            type="gap",
            type_source="slot_default",
            chips=[],
            normative=False,
        ),
    ]
    out = verify_claims(claims, {"cited_texts": {}, "chips": []}, _SETTINGS)
    assert any(v.check == "v_safe" and v.reason == "lawful_lane_ok" for v in out["a"])
    assert any(v.check == "v_safe" and v.reason == "reserved_advice_claim" for v in out["b"])
    # non-normative 'gap' routes to NO check in the orchestrator ⇒ no v_safe verdict.
    assert not out["c"]
