"""Per-answer ceiling on cheap-LLM calls made by the claim-verify and repair loop.

Every cheap-LLM call site in the loop (the batched entailment check, the optional claim-typing
assist and the redraft/repair lane) reserves calls with ``take()`` before issuing them. Once a
reservation would exceed the cap, ``take()`` returns False and latches ``exceeded`` (reported as the
``loop_budget_exceeded`` telemetry signal). The caller then falls back to deterministic checks only,
and any claim that needed an LLM is demoted (fail-closed), never silently passed.

The cap comes from ``claim_verify_max_llm_calls`` (default 13). The repair lane's ``RedraftBudget``
is an alias of this class (``charge()`` is ``take(1)``), so verification and repair share a single
ceiling. Deterministic: no clock, no database, never raises.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LoopBudget:
    """Per-turn cheap-LLM call budget.

    ``cap`` is the hard ceiling and ``used`` the running count. ``exceeded`` latches True the first
    time a reservation is refused; a refused ``take`` reserves nothing, so a batch is never silently
    split into a part that ran and a part that did not.
    """

    cap: int
    used: int = 0
    exceeded: bool = False

    def take(self, n: int = 1) -> bool:
        """Reserve ``n`` calls atomically.

        Returns True and adds ``n`` only if all ``n`` fit under the cap; otherwise returns False,
        reserves nothing and latches ``exceeded``. ``n <= 0`` is a no-op that returns True.
        """
        if n <= 0:
            return True
        if self.used + n > self.cap:
            self.exceeded = True
            return False
        self.used += n
        return True

    def charge(self) -> bool:
        """Reserve a single call; the interface the redraft lane uses (same as ``take(1)``)."""
        return self.take(1)

    def remaining(self) -> int:
        return max(0, self.cap - self.used)

    @classmethod
    def from_settings(cls, s: object) -> "LoopBudget":
        """Build from the ``claim_verify_max_llm_calls`` setting (default 13)."""
        return cls(cap=max(0, int(getattr(s, "claim_verify_max_llm_calls", 13))))
