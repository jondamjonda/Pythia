"""B2a-03 / B2a-04 — L2/L3 model-name rewire to the resolver, byte-stability anchor.

The L2 and L3 cascade layers no longer hold their own hardcoded OpenAI model literals
(`L2_MODEL = "gpt-4o-mini"` / `L3_MODEL = "gpt-4o"`); they resolve the string ONCE per run
via `resolve_model(ModelRole.APPLICABILITY)` / `resolve_model(ModelRole.OBLIGATIONS)` and
thread it through the per-provision eval fn + the audit/persist helpers.

These tests pin the two invariants that make the rewire safe:

  (a) BYTE-STABILITY ANCHOR — with no env override, the resolver returns the EXACT same
      strings the retired constants held. This is what guarantees existing L2/L3 eval
      output, audit actor_id/model_version, and finding generated_by are unchanged.

  (b) THREADING — the per-provision eval fn and the audit/persist helpers now accept a
      `model` parameter (so the resolved-once string flows to every write-site, keeping
      actor_id/model_version stable within a run). A light signature/inspect check.

The retired constants must also no longer exist as module attributes.
"""

from __future__ import annotations

import inspect

from regsuper.analysis import l2_applicability as l2
from regsuper.analysis import l3_obligations as l3
from regsuper.analysis.model_resolver import ModelRole, resolve_model


# --------------------------------------------------------------------------- (a) byte anchor
def test_resolver_returns_the_exact_retired_literals():
    """The default resolve MUST equal the old hardcoded constants, byte-for-byte. If either
    of these drifts, every L2/L3 audit actor_id / finding generated_by changes — the whole
    point of B2a-03/04 was that the swap is byte-identical by default."""
    assert resolve_model(ModelRole.APPLICABILITY) == "gpt-4o-mini"
    assert resolve_model(ModelRole.OBLIGATIONS) == "gpt-4o"


def test_retired_constants_are_gone():
    """L2_MODEL / L3_MODEL were retired — the model now flows from the resolver, not a
    module-level literal. Their continued absence pins that nothing re-introduced a
    second source of truth."""
    assert not hasattr(l2, "L2_MODEL")
    assert not hasattr(l3, "L3_MODEL")


# --------------------------------------------------------------------------- (b) threading
def test_l2_helpers_accept_a_model_param():
    for fn in (l2.evaluate_provision, l2._audit_l2_call, l2._persist_finding):
        params = inspect.signature(fn).parameters
        assert "model" in params, f"{fn.__name__} must accept a `model` param"


def test_l3_helpers_accept_a_model_param():
    for fn in (l3.extract_obligations, l3._persist_obligation, l3._audit_l3_call):
        params = inspect.signature(fn).parameters
        assert "model" in params, f"{fn.__name__} must accept a `model` param"
