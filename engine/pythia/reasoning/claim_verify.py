"""Route each atomic claim to the typed verifiers that apply to it and collect their verdicts.

``verify_claims(claims, evidence, settings)`` runs every claim through the ordered checks its type
routes to (``CHECK_ROUTING``), calling each verifier module's ``verify(claim, evidence, settings)``.
Checks run synchronously and deterministically. A checker that raises is recorded as a ``skip``
verdict (``reason='checker_exception'``): never a crash and never a silent pass.

Routing:
  * The safety check (V-SAFE) is appended to every normative claim.
  * deadline, amount: citation, fact and arithmetic checks, then safety.
  * legal_rule, procedure_step, sanction, authority: citation check, then safety.
  * situation_application (restating the asker's facts): profile check, then safety.
  * gap (an honest "not covered" line): nothing to verify.

Non-normative claims (headings, blank lines, disclaimers) assert nothing and get no checks. This
module only routes: it passes ``settings`` through, does not touch the database, and deliberately
never reads the metagate verdict, so claim verification stays independent of the question gate.
"""

from __future__ import annotations

from typing import Any

from pythia.reasoning import v_calc, v_cit, v_fact, v_prof, v_safe
from pythia.reasoning.claim_ledger import Claim, ClaimVerdict

# Check name to the verifier module's verify() callable. Every verifier implements (claim, evidence,
# settings) -> list[ClaimVerdict], so a verifier can change without touching the router.
_REGISTRY: dict[str, Any] = {
    "v_cit": v_cit.verify,
    "v_fact": v_fact.verify,
    "v_calc": v_calc.verify,
    "v_safe": v_safe.verify,
    "v_prof": v_prof.verify,
}

# Claim type to its ordered checks. V-SAFE is not listed because _checks_for appends it to every
# normative claim. 'gap' routes to nothing.
CHECK_ROUTING: dict[str, list[str]] = {
    "deadline": ["v_cit", "v_fact", "v_calc"],
    "amount": ["v_cit", "v_fact", "v_calc"],
    "legal_rule": ["v_cit"],
    "procedure_step": ["v_cit"],
    "sanction": ["v_cit"],
    "authority": ["v_cit"],
    "situation_application": ["v_prof"],
    "gap": [],
}


def _checks_for(claim: "Claim") -> list[str]:
    """Return the ordered checks for a claim.

    Non-normative claims get none. Normative claims get their type's checks with V-SAFE appended
    last, de-duplicated. An unknown type falls back to ``[v_cit]``, so every normative claim
    receives at least one verdict.
    """
    if not claim.normative:
        return []
    if claim.type == "gap":
        return []  # an honest no-coverage line; nothing to verify
    routed = CHECK_ROUTING.get(claim.type)
    if routed is None:
        routed = ["v_cit"]  # conservative default so at least one verdict is guaranteed
    ordered = list(routed) + ["v_safe"]
    seen: set[str] = set()
    out: list[str] = []
    for ck in ordered:
        if ck not in seen:
            seen.add(ck)
            out.append(ck)
    return out


def verify_claims(
    claims: list["Claim"],
    evidence: dict[str, Any],
    settings: Any,
) -> dict[str, list["ClaimVerdict"]]:
    """Run the routed checks for every claim.

    Args:
        claims: Atomic claims from the claim atomiser.
        evidence: Shared evidence bundle (``cited_texts`` mapping provision id to full text, the
            citations, intake and profile facts, ``fact_lookup``).
        settings: Passed through to each verifier.

    Returns:
        ``{claim_id: [ClaimVerdict, ...]}``; the list is empty for a claim that routes to no check
        (non-normative or 'gap'). A checker that raises yields a skip verdict. Deterministic and
        synchronous; no LLM calls are made here.
    """
    out: dict[str, list[ClaimVerdict]] = {}
    for claim in claims:
        verdicts: list[ClaimVerdict] = []
        for check in _checks_for(claim):
            fn = _REGISTRY.get(check)
            if fn is None:  # defensive: an unregistered check name must not crash
                verdicts.append(ClaimVerdict(check=check, status="skip", reason="unregistered_check"))
                continue
            try:
                result = fn(claim, evidence, settings)
            except Exception as exc:  # noqa: BLE001 - a failing checker becomes a skip, never a crash or pass
                verdicts.append(
                    ClaimVerdict(
                        check=check, status="skip", reason="checker_exception", evidence={"error": type(exc).__name__}
                    )
                )
                continue
            if isinstance(result, list):
                verdicts.extend(v for v in result if isinstance(v, ClaimVerdict))
            elif isinstance(result, ClaimVerdict):
                verdicts.append(result)
            else:  # a checker that returned the wrong shape is a skip, never a silent pass
                verdicts.append(ClaimVerdict(check=check, status="skip", reason="bad_verifier_return"))
        out[claim.id] = verdicts
    return out
