"""Stage labels for every LLM call in the answer pipeline and the claim-verify/repair loop.

Each LLM call is tagged with a ``stage=`` label, and the /metrics ``by_stage`` breakdown groups
calls by it. Keeping all labels in one module stops call sites from drifting into inconsistent
naming, which would make whole lanes disappear from the breakdown.

The string values are the labels already emitted on the wire (``decompose``, ``generate_retry``,
``verify_faithfulness``, ``claim_repair_redraft``), so changing them requires a coordinated
telemetry migration. The constant name is the canonical name; ``CANONICAL`` maps each legacy value
to it.
"""

from __future__ import annotations

# Answer-pipeline stages. The legacy string values are kept; do not change them without migrating
# telemetry consumers.
STAGE_INTAKE: str = "decompose"  # canonical: intake (the unified intake / decompose call)
STAGE_GENERATE: str = "generate"
STAGE_STRICT_RETRY: str = "generate_retry"  # canonical: strict_retry
STAGE_FAITHFULNESS: str = "verify_faithfulness"  # canonical: faithfulness
STAGE_METAGATE: str = "metagate"

# Claim-verify / repair loop stages.
STAGE_ATOMIZE_ASSIST: str = "atomize_assist"  # optional cheap-LLM claim-typing assist
STAGE_CLAIM_ENTAILMENT: str = "claim_entailment"  # citation verifier's ambiguous-band entailment
STAGE_REPAIR: str = "claim_repair_redraft"  # canonical: repair_<n> (the prose-repair lane)

# Every stage the /metrics by_stage breakdown must include (a stage appears once it has a call).
ALL_STAGES: tuple[str, ...] = (
    STAGE_INTAKE,
    STAGE_GENERATE,
    STAGE_STRICT_RETRY,
    STAGE_FAITHFULNESS,
    STAGE_METAGATE,
    STAGE_ATOMIZE_ASSIST,
    STAGE_CLAIM_ENTAILMENT,
    STAGE_REPAIR,
)

# Legacy emitted value to canonical name (documentation, and the map for any future rename).
CANONICAL: dict[str, str] = {
    "decompose": "intake",
    "generate_retry": "strict_retry",
    "verify_faithfulness": "faithfulness",
    "claim_repair_redraft": "repair",
}
