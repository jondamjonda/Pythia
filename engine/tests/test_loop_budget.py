"""VL-G-05 (WS3 step 20) — LoopBudget, the C9 single-source cheap-LLM call bound.

Pins: total reservations never exceed the cap under ANY pattern; a batch that would overflow reserves nothing
and latches the breach flag (never raises); the redraft lane's RedraftBudget is an ALIAS of LoopBudget so
verify + repair draw from ONE ceiling; from_settings reads claim_verify_max_llm_calls (13, the single source).
"""

from __future__ import annotations

from types import SimpleNamespace

from pythia.reasoning.claim_repair import RedraftBudget
from pythia.reasoning.loop_budget import LoopBudget


def test_never_exceeds_cap_under_any_pattern():
    b = LoopBudget(cap=13)
    granted = 0
    # a pathological interleaving of single + batched reservations
    for n in (1, 5, 1, 3, 8, 1, 2, 4, 1):
        if b.take(n):
            granted += n
    assert granted == b.used <= 13
    assert b.remaining() == 13 - b.used


def test_batch_reservation_is_atomic():
    b = LoopBudget(cap=10)
    assert b.take(8) is True and b.used == 8
    assert b.take(4) is False  # 8+4 > 10 ⇒ refused, reserves NOTHING
    assert b.used == 8 and b.exceeded is True
    assert b.take(2) is True and b.used == 10  # the exact remainder still fits
    assert b.take(1) is False and b.used == 10


def test_exhaustion_latches_flag_never_raises():
    b = LoopBudget(cap=2)
    assert b.take() and b.take()
    assert b.exceeded is False
    assert b.take() is False and b.exceeded is True  # third refused, flag latches
    # once exceeded, even a later fitting-size take stays refused only if it overflows; a 0-take is a no-op
    assert b.take(0) is True


def test_charge_is_take_one_and_redraft_alias():
    # RedraftBudget must BE LoopBudget (the single source) so verify+repair share one ceiling.
    assert RedraftBudget is LoopBudget
    b = RedraftBudget(cap=1)
    assert b.charge() is True and b.used == 1
    assert b.charge() is False


def test_shared_ceiling_across_verify_and_repair():
    # ONE budget threaded through a verify lane (entailment batches) AND the repair lane (redraft charges):
    # their combined draws can never exceed the C9 cap.
    b = LoopBudget(cap=5)
    b.take(3)  # entailment batch
    assert b.charge() and b.charge()  # two redraft charges → used 5
    assert b.charge() is False  # exhausted for the whole turn
    assert b.used == 5 and b.exceeded


def test_from_settings_reads_c9_single_source():
    b = LoopBudget.from_settings(SimpleNamespace(claim_verify_max_llm_calls=13))
    assert b.cap == 13
    # missing knob ⇒ the documented default
    assert LoopBudget.from_settings(SimpleNamespace()).cap == 13
