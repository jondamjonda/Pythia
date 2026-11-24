"""The regulatory analysis cascade.

A run flows through L0 intake, L1 candidate retrieval, L2 applicability classification, L3
obligation extraction and L4 gap analysis. Each layer lives in its own module so it can be tested
in isolation. This package also holds the law-source gate that the persisting layers call.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from regsuper.config import get_settings

__all__ = ["LawSourceNotSupported", "require_cascade_law_source"]
# cutover_eval_mode and cutover_eval_active are deliberately left out of __all__: they are a private
# hook for the offline evaluation harness, not part of the analysis API. Never import them on a
# request or serving path.


class LawSourceNotSupported(RuntimeError):
    """The L2-L4 cascade was requested against a law source not approved for live serving."""


# Context-scoped permission for the cascade to run under law_source='nexus' (the engine's corpus) so
# the evaluation harness can generate a sample for lawyer review without relaxing the gate for live
# traffic. Defaults to False (fail closed): outside cutover_eval_mode() nexus is still refused. As a
# ContextVar it is per execution context, so a concurrent live request never sees it.
_CUTOVER_EVAL: ContextVar[bool] = ContextVar("cutover_eval_active", default=False)

# Second lock: cutover_eval_mode() also requires this process-level env flag, which only the harness
# sets. Importing and entering the context manager from a server process is therefore not enough to
# bypass the gate; a request handler that wrapped a block in cutover_eval_mode() by mistake would
# raise instead of serving.
_EVAL_PROCESS_FLAG = "REGSUPER_CUTOVER_EVAL"


@contextmanager
def cutover_eval_mode() -> Iterator[None]:
    """Allow the cascade to run on the nexus corpus for an offline evaluation.

    Inside this context ``require_cascade_law_source`` permits ``law_source='nexus'``; on exit the
    gate fails closed again. No global flag changes, and because the permission is a ContextVar it
    has no effect on concurrent requests.

    Refuses to activate unless the process-level ``REGSUPER_CUTOVER_EVAL=1`` flag is set (only
    scripts/cutover_eval_harness.py sets it). A serving process never sets it, so accidental use of
    this context manager on a request path raises instead of silently serving nexus.
    """
    if os.environ.get(_EVAL_PROCESS_FLAG) != "1":
        raise RuntimeError(
            "cutover_eval_mode() refused: the process-level REGSUPER_CUTOVER_EVAL=1 flag is not set. This hook "
            "is the off-flip eval harness ONLY — it must never run on a serving path."
        )
    token = _CUTOVER_EVAL.set(True)
    try:
        yield
    finally:
        _CUTOVER_EVAL.reset(token)


def cutover_eval_active() -> bool:
    """Return True if the current execution context is inside ``cutover_eval_mode()``."""
    return _CUTOVER_EVAL.get()


def require_cascade_law_source(layer: str) -> None:
    """Refuse to run a persisting layer (L2, L3, L4) on a law source not approved for live serving.

    Live traffic runs on ``law_source='regsuper'`` (the app's own provision tables). A run on
    ``'nexus'`` (the engine's canonical corpus) does resolve end to end: L2 and L3 read the engine,
    cited documents resolve app-side through the regsuper-to-nexus crosswalk, and the audit,
    evidence and digest read layers are corpus-aware. It is still not approved for serving until a
    lawyer has reviewed a generated sample, so this gate fails loudly on any other source.

    Two sanctioned exceptions admit nexus:

    * an offline evaluation inside :func:`cutover_eval_mode`, which generates the lawyer-review
      sample against an isolated database (never the live regsuper one);
    * the live cutover, admitted only when the operator has set ``cutover_approved=True``: their
      explicit attestation that the lawyer sample was reviewed as faithful and the refusal-floor
      fallback was accepted. The flag defaults to off, which keeps refusing.

    L0 and L1 do not call this gate.
    """
    src = get_settings().law_source
    if src == "regsuper":
        return
    if src == "nexus" and _CUTOVER_EVAL.get():
        return  # sanctioned offline eval against an isolated database; see cutover_eval_mode()
    if src == "nexus" and getattr(get_settings(), "cutover_approved", False):
        return  # operator-approved live cutover (lawyer sample and fallback signed off)
    raise LawSourceNotSupported(
        f"{layer}: law_source={src!r} is refused for the live cascade — the B3 cutover is not approved for "
        f"serving (set cutover_approved=True only after B3-06a + B3-06b). It resolves end-to-end now, but live "
        f"traffic stays on 'regsuper' until then. The off-flip eval harness (cutover_eval_mode) may also run nexus."
    )
