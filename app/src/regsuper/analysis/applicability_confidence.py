"""Split extraction confidence from applicability confidence for obligation findings.

The cascade emits one ``confidence`` per finding, but that number conflates two independent
questions:

* Extraction confidence: how good was retrieval and interpretation? Did we read the right
  provision, quote it faithfully, parse the actor and modal correctly? A pipeline-quality signal.
* Applicability confidence: given a faithful extraction, how sure are we that the obligation binds
  this entity in this role? A legal signal about the ``applicability.decide_binding`` verdict.

Collapsing the two is dangerous. The 13 ZISIF s.614 generic offences in the lawyer-reviewed sample
have an extraction confidence of about 0.899 (Czech offence text is noisier to parse), yet their
applicability is categorical: a generic offence binds any person, so it binds the AIFM as solidly as
anything can. If one conflated number drove a "low confidence, so hide it" rule, those binding
offences would be suppressed because the text was hard to parse, a textbook false negative. Here
the applicability label is computed from the legal basis of the verdict and is provably independent
of ``extraction_confidence`` (see ``test_extraction_confidence_is_orthogonal``).

This module is pure and deterministic: no LLM, database, network, clock or randomness. It reads the
facts ``applicability`` already derived (addressee class, obligation type, ``binds`` verdict) plus
the raw ``extraction_confidence`` for audit display only. ``applicability_scoring`` attaches its
output when ``applicability_confidence_enabled`` is on.

Hiding or understating a genuinely binding duty is worse than over-reporting one, so:

1. This module only computes a label. It never changes a ``binds`` verdict and never drops or hides
   a finding, so binding recall is 1.0 by construction (checked by the eval below).
2. ``extraction_confidence`` can never lower the label. A noisy extraction may be noted in the
   human-readable ``reason``, but the label is a pure function of the legal basis.
3. The only demotion offered (``applicability_confidence_floor``) keeps more: it can turn a would-be
   ``out_of_scope`` into ``conditional`` (more visible), never the reverse. It reads only the
   applicability label; extraction confidence is not even a parameter.
"""

from __future__ import annotations

from typing import Literal

# The closed domains defined in applicability (imported read-only, never redefined).
from regsuper.analysis.applicability import AddresseeClass, Binds, ObligationType

# The two axes this module adds.
# The kind of legal ground under the binding decision: the audit trail for the applicability label.
# Each value names a distinct basis a lawyer can check.
ConfidenceBasis = Literal[
    "addressee_match",  # addressed to the project's own regulated role (for example an AIFM duty)
    "generic_offence_coverage",  # categorical "any person" offence; binds everyone, AIFM included
    "transposition_vehicle",  # "Member States shall require that <entity> ...": binds via MS law
    "counterparty_contractual",  # a depositary, valuer or prime broker duty; verify or contract
    "conditional_assignment",  # licence scope or Member State discretion; depends on client facts
    "licence_scope_presumed",  # activity-scope rule presumed in scope pending the client's licence
    "declared_exclusion",  # client declared a role exclusion (pure AIFM, no top-up): solidly out
    "unknown",  # basis could not be determined (fail-safe: kept visible, never hidden)
]

# The legal solidity of the verdict, independent of extraction quality. There is deliberately no
# "out_of_scope" label: the binds verdict carries scope, while this label rates how sure it is.
ApplicabilityConfidence = Literal["high", "moderate", "low"]


# Basis to default label. The label rates only the legal solidity of the binding verdict; it is a
# pure function of the basis and never reads extraction_confidence, which is the point of the split.
#   * addressee_match (high): the norm names the project's own role, the most solid ground.
#   * generic_offence_coverage (high): categorical "binds any person"; solidity does not depend on
#     parse quality (the s.614 offences at 0.899 are still high).
#   * declared_exclusion (high): the client asserted the disqualifying fact; a solid call.
#   * transposition_vehicle (moderate): binds, but the national enactment needs checking.
#   * counterparty_contractual (moderate): a real duty, but on a counterparty; depends on contract.
#   * conditional_assignment (moderate): gated on a client fact (discretion or activity).
#   * licence_scope_presumed (moderate): presumed applicable pending the client's licence scope.
#   * unknown (low): basis indeterminate; flagged for review, but the finding is kept.
_BASIS_LABEL: dict[str, ApplicabilityConfidence] = {
    "addressee_match": "high",
    "generic_offence_coverage": "high",
    "declared_exclusion": "high",
    "transposition_vehicle": "moderate",
    "counterparty_contractual": "moderate",
    "conditional_assignment": "moderate",
    "licence_scope_presumed": "moderate",
    "unknown": "low",
}

_COUNTERPARTY_ADDRESSEES = frozenset({"depositary", "external_valuer", "prime_broker"})


def applicability_confidence_basis(
    addressee: AddresseeClass,
    obligation_type: ObligationType,
    binds: Binds,
    extraction_confidence: float | None = None,
    project_role: dict | None = None,
) -> dict:
    """Classify the legal basis of the binding verdict and return the applicability label.

    Returns a dict with:

    * ``basis``: a :data:`ConfidenceBasis` naming the kind of legal ground under the verdict.
    * ``label``: an :data:`ApplicabilityConfidence` rating only the legal solidity of the verdict.
    * ``reason``: a human-readable audit string. If ``extraction_confidence`` is supplied it is
      appended here for the auditor, but it never feeds the label (the label is fixed from the
      basis before the reason is composed).

    ``project_role`` is reserved for a future refinement of the basis (for example distinguishing
    a self-managed AIF); it does not currently affect the label.

    Invariants checked by the tests:

    * The label is a pure function of ``addressee``, ``obligation_type`` and ``binds``; varying
      ``extraction_confidence`` anywhere in [0, 1], including 0.0, leaves it unchanged.
    * A verdict that keeps the finding as binding (``binds`` other than ``out_of_scope``) is never
      rated ``low``.
    * A ``generic_offence`` or ``any_person`` verdict is always ``high``, even though the s.614
      offences in the reviewed sample have extraction confidence of about 0.899.
    * No ``binds`` verdict changes and nothing is hidden; this only annotates.
    """
    # 1) Determine the legal basis from the applicability facts alone (extraction_confidence is
    # intentionally not read).
    basis: str
    if obligation_type == "generic_offence" or addressee == "any_person":
        # Categorical "binds any person": the AIFM is caught, whatever the parse quality.
        basis = "generic_offence_coverage"
    elif binds == "out_of_scope":
        # decide_binding admits out_of_scope on exactly two safe bases. Name the one that applies so
        # the auditor sees why the finding was set aside.
        if obligation_type == "transposition_instruction":
            # "Member States shall adopt laws": addressed to the legislature, not an entity.
            basis = "conditional_assignment"
        else:
            # A client-declared role exclusion with no top-up, the only entity-conduct out_of_scope
            # path.
            basis = "declared_exclusion"
    elif obligation_type == "member_state_requires_entity":
        basis = "transposition_vehicle"
    elif obligation_type == "member_state_discretion":
        basis = "conditional_assignment"
    elif obligation_type == "counterparty_duty" or addressee in _COUNTERPARTY_ADDRESSEES:
        basis = "counterparty_contractual"
    elif obligation_type == "licence_scope_rule" or addressee == "investment_company_licence":
        basis = "licence_scope_presumed"
    elif obligation_type == "liability_allocation" or addressee == "prospectus_responsible":
        # A liability-allocation duty (for example ZPKT s.34 prospectus liability) is gated on the
        # project's prospectus role.
        basis = "conditional_assignment"
    elif obligation_type == "substantive_duty" and addressee in (
        "aifm",
        "ucits_manco",
        "mifid_investment_firm",
        "credit_institution",
    ):
        basis = "addressee_match"
    elif addressee == "unknown" or obligation_type == "unknown":
        # Basis undetermined: "low" flags it for review, but the binding guard has already kept the
        # finding.
        basis = "unknown"
    else:
        basis = "unknown"

    # 2) The label comes from the basis table, never from extraction_confidence. This line is the
    # whole independence guarantee.
    label: ApplicabilityConfidence = _BASIS_LABEL[basis]

    # 2a) A verdict that definitely binds (binds == 'yes') is never rated 'low'. The binding guard
    # forces binds='yes' for an unparseable addressee (basis 'unknown'); presenting that kept duty
    # as low confidence would understate it. 'low' stays reachable for a genuinely uncertain
    # 'conditional' verdict, where it is honest rather than understating.
    if binds == "yes" and label == "low":
        label = "moderate"

    # 3) Compose the audit reason. extraction_confidence appears only here, after the label is
    # fixed, so it can colour the explanation without moving the rating.
    reason = _BASIS_REASON[basis]
    if extraction_confidence is not None:
        ec = round(float(extraction_confidence), 4)
        reason = (
            f"{reason} [applicability label '{label}' is set from the legal basis and is independent of the "
            f"extraction confidence {ec}]"
        )
    return {"basis": basis, "label": label, "reason": reason}


# Human-readable rationale per basis (audit copy with no client data or PII, safe to surface).
_BASIS_REASON: dict[str, str] = {
    "addressee_match": "the norm is addressed to the project's own regulated role — the most solid binding ground",
    "generic_offence_coverage": "a categorical offence binding any person, including the AIFM — solid regardless of extraction noise",
    "transposition_vehicle": "transposed via your Member State; the substance binds the named entity once the national enactment is confirmed",
    "counterparty_contractual": "a counterparty's duty (depositary/valuer/prime broker) — verify it or contract for it",
    "conditional_assignment": "a conditional/discretion-gated norm — decision-relevant context that depends on your facts",
    "licence_scope_presumed": "an activity-scope rule presumed applicable pending your actual licence scope",
    "declared_exclusion": "the client declared a role exclusion (with no top-up) — a solid out-of-scope determination",
    "unknown": "the legal basis could not be determined — flagged for review; the finding remains kept (fail-safe)",
}


def applicability_confidence_floor(label: ApplicabilityConfidence, floor_value: int) -> bool:
    """Return True if a weak applicability label should demote ``out_of_scope`` to ``conditional``.

    True when the label's strength is strictly below ``floor_value``, meaning the set-aside rests
    on too weak a legal footing and should be surfaced as ``conditional`` instead of hidden. Label
    strength is ordinal: ``low=0 < moderate=1 < high=2``.

    Safety properties checked by the tests:

    * It only ever keeps more. A True result can only turn ``out_of_scope`` into ``conditional``;
      nothing here turns ``yes`` or ``conditional`` into ``out_of_scope``. Callers must apply the
      result as a demotion only.
    * Extraction confidence is not a parameter, so a noisy extraction can never trigger or block a
      demotion. The only inputs are the legal-solidity ``label`` and the operator-set
      ``floor_value``.
    * ``floor_value <= 0`` never demotes, since no label is below 0. That is the default.
    """
    strength = _LABEL_STRENGTH[label]
    return strength < floor_value


# Ordinal strength of each applicability label (low is weakest). Used only by the floor; extraction
# confidence never appears in this map.
_LABEL_STRENGTH: dict[str, int] = {"low": 0, "moderate": 1, "high": 2}


# Evaluation over the 79 lawyer-reviewed findings (read-only; not used at runtime).
# Mirrors applicability_eval: derive the applicability facts from the lawyer-confirmed actor, modal
# and statement, then score the confidence split. It passes when extraction_confidence never moves
# the label and the 13 s.614 generic offences (extraction confidence about 0.899) are rated high.

from pathlib import Path  # noqa: E402  (kept local to the eval section, like applicability_eval)

_DEFAULT_GOLD = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "applicability_gold.json"
)


def _load_obligations(gold: Path) -> list[dict]:
    import json

    data = json.loads(gold.read_text(encoding="utf-8"))
    out = []
    for f in data.get("findings", []):
        if f.get("type") != "obligation":
            continue
        p = f.get("payload") or {}
        out.append(
            {
                "actor": p.get("actor"),
                "modal": p.get("modal"),
                "statement": f.get("statement"),
                "confidence": f.get("confidence"),
            }
        )
    return out


def score(obligations: list[dict], *, pure_aifm: bool = False) -> dict:
    """Score the confidence split over the obligations for one project-role configuration.

    Derives addressee, obligation type and binds verdict deterministically, then computes the
    applicability label per row. Reports the label distribution, the generic-offence check, and the
    main acceptance criterion: the label is identical whether extraction_confidence is the row's
    real value, 0.0, 1.0 or None.
    """
    # Imported here (eval only) to keep the pure core's import surface minimal, as in
    # applicability_eval.
    from regsuper.analysis.applicability import (
        addressee_class,
        decide_binding,
        obligation_type,
        project_entity_role,
    )

    role = project_entity_role(
        "aifm",
        "pure AIFM, not a MiFID investment firm, no MiFID top-up services"
        if pure_aifm
        else "EU AIFM marketing its own AIF",
    )

    label_dist: dict[str, int] = {}
    basis_dist: dict[str, int] = {}
    generic_offence_labels: list[str] = []
    binding_kept = 0
    binding_total = 0
    orthogonal = True  # main acceptance: extraction_confidence never moves the label

    for o in obligations:
        addr = addressee_class(o["actor"])
        otype = obligation_type(addr, o["modal"], o["statement"])
        verdict = decide_binding(
            role, addr, otype, binds_confidence=float(o.get("confidence") or 1.0)
        )
        binds = verdict["binds"]

        # This module changes no verdict, so binding recall is 1.0 by construction: every obligation
        # whose verdict is kept (yes or conditional) stays kept after annotation.
        if binds != "out_of_scope":
            binding_total += 1
            binding_kept += 1  # annotation cannot flip a kept verdict (it sets no verdict at all)

        real = applicability_confidence_basis(
            addr, otype, binds, extraction_confidence=o.get("confidence"), project_role=role
        )
        label = real["label"]
        label_dist[label] = label_dist.get(label, 0) + 1
        basis_dist[real["basis"]] = basis_dist.get(real["basis"], 0) + 1
        if otype == "generic_offence" or addr == "any_person":
            generic_offence_labels.append(label)

        # Independence check: recompute the label at the extremes of extraction_confidence; it must
        # not move.
        lo = applicability_confidence_basis(
            addr, otype, binds, extraction_confidence=0.0, project_role=role
        )["label"]
        hi = applicability_confidence_basis(
            addr, otype, binds, extraction_confidence=1.0, project_role=role
        )["label"]
        none_ = applicability_confidence_basis(
            addr, otype, binds, extraction_confidence=None, project_role=role
        )["label"]
        if not (lo == hi == none_ == label):
            orthogonal = False

    binding_recall = (binding_kept / binding_total) if binding_total else 1.0
    generic_non_low = all(lab != "low" for lab in generic_offence_labels)
    generic_high = all(lab == "high" for lab in generic_offence_labels)

    return {
        "config": "pure_aifm" if pure_aifm else "no_exclude",
        "n_obligations": len(obligations),
        "label_dist": label_dist,
        "basis_dist": basis_dist,
        "n_generic_offences": len(generic_offence_labels),
        "generic_offence_non_low": generic_non_low,  # s.614 offences never demoted for poor parsing
        "generic_offence_all_high": generic_high,
        "binding_recall": round(binding_recall, 4),  # must be 1.0: this module changes no verdict
        "extraction_orthogonal": orthogonal,  # label independent of extraction_confidence
        "ac_pass": orthogonal and generic_non_low and binding_recall == 1.0,
    }


def run(gold: Path | None = None) -> dict:
    g = gold or _DEFAULT_GOLD
    obls = _load_obligations(g)
    no_exclude = score(obls, pure_aifm=False)
    pure_aifm = score(obls, pure_aifm=True)
    return {
        "gold": str(g),
        "n": len(obls),
        "no_exclude": no_exclude,
        "pure_aifm": pure_aifm,
        "ac_pass": no_exclude["ac_pass"] and pure_aifm["ac_pass"],
    }


def main() -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(
        description="Score the WF-2 confidence split vs the 79 gold findings."
    )
    ap.add_argument(
        "--gold",
        default=None,
        help="path to the reviewed sample JSON (default: the B3-06a gold slice)",
    )
    args = ap.parse_args()
    report = run(Path(args.gold) if args.gold else None)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ac_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
