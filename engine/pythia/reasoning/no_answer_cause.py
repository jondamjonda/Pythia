"""Typed reason an answer ended up refused, abstained or degraded (observe-only).

An answer can be refused or abstained, or served in a degraded render mode (closest, notlegal,
refuse), for several different reasons that look the same from outside: ``refused=True`` or
``render_mode=refuse``. For example, intake and retrieval succeed and a draft is produced, but it
carries no citation tags, so the grounding floor refuses it; the surface signal then looks like a
coverage or false-premise problem.

``classify_no_answer_cause`` names the cause. It reads only decision signals ``answer()`` has
already computed (the metagate verdict, the grounding verdict, the enforcement refusal flag) and
returns the first matching decision point, in pipeline order, as a ``NoAnswerCause``. It never
changes a decision; the result is attached as ``audit_trace["no_answer_cause"]`` and the optional
``PythiaAnswer.no_answer_cause`` field. A normally served, grounded answer is ``NONE``.

Rules:
  * The string values are the wire and telemetry form. Never rename a value; add a member instead.
  * Pure and total. Any unexpected state falls back to ``NONE``; nothing raises.
  * First match wins, in the order ``answer()`` decides: no retrieval, then the metagate route
    (intent, coverage, premise), then claim-verify enforcement, then the grounding floor. The
    grounding-floor cause (drafted but under-grounded) stays distinct from the metagate routes and
    from a compliance block.
"""

from __future__ import annotations

from enum import Enum


class NoAnswerCause(str, Enum):
    """Reason an answer is a non-answer or a degraded render.

    The ``str`` mixin makes each member equal to its wire value, so ``NoAnswerCause.GROUNDING_FLOOR
    == "grounding_floor"`` and ``json.dumps`` writes the bare string.
    """

    # A normally served, grounded answer; no non-answer decision fired.
    NONE = "none"

    # Metagate routes (only when the metagate is on), checked as intent, coverage, then premise.
    INTENT_NOT_LEGAL = "intent_not_legal"  # intent: not a legal or regulatory question
    COVERAGE_NO_COVERAGE = "coverage_no_coverage"  # coverage: nothing in the corpus bears on it
    COVERAGE_WEAK_COVERAGE = "coverage_weak_coverage"  # coverage: only a partial or weak basis
    OUT_OF_SCOPE = "out_of_scope"  # coverage: outside the domains the corpus covers
    PREMISE_FALSE = "premise_false"  # premise: the question rests on a false premise

    # Refusals after generation: a draft was produced, then withheld.
    GROUNDING_FLOOR = "grounding_floor"  # verification: drafted, but grounded below the floor
    COMPLIANCE_BLOCK = "compliance_block"  # a compliance or lawful-use guard withheld the answer
    VERIFY_ENFORCE = "verify_enforce"  # claim enforcement: no core claim survived, so refuse


# Metagate coverage verdict to cause, used only for the `not_in_corpus` route, so the coverage
# sub-reason (no_coverage, weak_coverage, out_of_scope) is preserved.
_COVERAGE_CAUSE: dict[str, NoAnswerCause] = {
    "no_coverage": NoAnswerCause.COVERAGE_NO_COVERAGE,
    "weak_coverage": NoAnswerCause.COVERAGE_WEAK_COVERAGE,
    "out_of_scope": NoAnswerCause.OUT_OF_SCOPE,
}


def classify_no_answer_cause(
    *,
    meta_verdict=None,
    verdict=None,
    enforce_refused: bool = False,
    no_retrieval: bool = False,
    compliance_blocked: bool = False,
) -> NoAnswerCause:
    """Derive why this answer is a non-answer or was degraded.

    Reads only decision signals that were already computed and decides nothing. The first match
    wins, in the order ``answer()`` reaches each decision point. Every argument is optional, so a
    partial call is safe.

    Args:
        meta_verdict: The metagate ``MetaVerdict``, or None when the metagate is off. Its ``route``
            and ``coverage`` name the intent, coverage or premise cause.
        verdict: The post-generation ``verify.Verdict``, or None on a retrieve-only path.
            ``verdict.verdict == "refuse"`` is the grounding floor, distinct from metagate routes.
        enforce_refused: Claim enforcement downgraded an ok verdict to refuse.
        no_retrieval: Nothing was retrieved (empty RRF), giving the honest no-provisions abstain.
        compliance_blocked: A compliance or lawful-use guard withheld the answer. The engine has
            no such guard yet, so this is reserved for a future one.

    Returns:
        The cause, or ``NoAnswerCause.NONE`` for a normally served answer or any state that
        cannot be attributed. Never raises.
    """
    try:
        # 1. No retrieval: the earliest non-answer. With nothing to ground against, the answer is an
        #    honest no-provisions abstain, attributed to coverage (no basis in the corpus).
        if no_retrieval:
            return NoAnswerCause.COVERAGE_NO_COVERAGE

        # 2. Metagate route: intent, then coverage, then premise, as route() decides. A non-sound
        #    route names the cause whether it stopped the answer (enforce mode) or only set a shadow
        #    render mode.
        if meta_verdict is not None:
            route = getattr(meta_verdict, "route", "sound")
            if route == "not_a_legal_question":
                return NoAnswerCause.INTENT_NOT_LEGAL
            if route == "not_in_corpus":
                coverage = str(getattr(meta_verdict, "coverage", "") or "")
                return _COVERAGE_CAUSE.get(coverage, NoAnswerCause.COVERAGE_WEAK_COVERAGE)
            if route == "false_premise":
                return NoAnswerCause.PREMISE_FALSE
            # route == "sound": fall through to the post-generation checks below.

        # 3. Compliance block: a guard that withholds a drafted answer (defensive; the engine has no
        #    such guard yet). Checked before the grounding floor so a block is reported as a block.
        if compliance_blocked:
            return NoAnswerCause.COMPLIANCE_BLOCK

        # 4. Claim enforcement: the per-claim refusal that downgraded an ok verdict to refuse,
        #    reported separately from the whole-answer grounding floor below.
        if enforce_refused:
            return NoAnswerCause.VERIFY_ENFORCE

        # 5. Grounding floor: a draft was produced but post-generation verification refused it (no
        #    grounded citations, all ungrounded, or grounding below the floor). An `abstain` is an
        #    honest no-answer (the draft said no provision is on point), not a floor failure, so it
        #    stays NONE.
        if verdict is not None and getattr(verdict, "verdict", None) == "refuse":
            return NoAnswerCause.GROUNDING_FLOOR

        return NoAnswerCause.NONE
    except Exception:  # an audit-only derivation must never break the answer path
        return NoAnswerCause.NONE
