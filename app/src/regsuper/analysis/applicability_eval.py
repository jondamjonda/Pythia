"""Offline evaluation of the entity-role applicability gate against 79 lawyer-confirmed findings.

Follows the same pattern as ``compliance_eval``: pure scorers plus a seed label set for the advocate
to validate. Read-only; not used at runtime. The acceptance gate mirrors ``compliance_eval``'s
``false_information == 0``::

    ac_pass = (binding_recall == 1.0) and (out_of_scope_precision >= 0.95)

``binding_recall == 1.0`` is the metric that matters most: a single genuinely binding obligation
marked ``out_of_scope`` fails the build, because a false negative on applicability is worse than a
false positive. The gate's over-suppression guard makes this true by construction (only
legislature-addressed transposition lines and a client-declared role exclusion are ever eligible for
``out_of_scope``); this harness demonstrates it on the 79 real findings.

``true_binds`` is a seed derived from the lawyer-confirmed ``actor`` and the scope caveats in the
reviewer's notes. Like compliance_eval's LABELLED_SEED it is a scaffold for the advocate to
validate, not yet evidential ground truth; the real signal comes when the advocate corrects a label
and the classifier must then match it. Both project-role configurations are scored on each run::

    python -m regsuper.analysis.applicability_eval [--gold <json>]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from regsuper.analysis.applicability import (
    addressee_class,
    decide_binding,
    obligation_type,
    project_entity_role,
)

# The committed gold set: 79 obligations a lawyer confirmed as faithful, reduced to the fields
# needed. Pass `--gold <path>` to score the full reviewed sample kept outside the repository.
_DEFAULT_GOLD = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "applicability_gold.json"
)

# An AIFM project, scored twice: "no-exclude" (MiFID stays conditional) and "pure-aifm" (the client
# disclaims the MiFID firm role and any top-up, so MiFID firm conduct can be out of scope).
_ROLE_NO_EXCLUDE = project_entity_role("aifm", "EU AIFM marketing its own AIF")
_ROLE_PURE_AIFM = project_entity_role(
    "aifm", "pure AIFM, not a MiFID investment firm, no MiFID top-up services"
)


def _true_binds(addressee: str, obl_type: str, *, pure_aifm: bool) -> str:
    """Return the seed expected verdict, derived from the lawyer-confirmed addressee and type."""
    if obl_type == "transposition_instruction":  # pure "adopt laws" only
        return "out_of_scope"
    if obl_type in ("member_state_requires_entity", "member_state_discretion"):
        # The substance binds the named entity, or it is decision-relevant context: keep visible.
        return "conditional"
    if addressee == "any_person":
        return "yes"
    if addressee in ("aifm", "ucits_manco", "credit_institution"):
        return "yes"
    if addressee == "mifid_investment_firm":
        return "out_of_scope" if pure_aifm else "conditional"
    if addressee in (
        "depositary",
        "external_valuer",
        "prime_broker",
        "investment_company_licence",
        "prospectus_responsible",
    ):
        return "conditional"
    return "yes"  # unknown or unparseable: keep (fail-safe), matching the guard


def _load_obligations(gold: Path) -> list[dict[str, Any]]:
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


def score(obligations: list[dict[str, Any]], *, pure_aifm: bool) -> dict[str, Any]:
    """Run the gate over the obligations and compute the metrics for one project-role config."""
    role = _ROLE_PURE_AIFM if pure_aifm else _ROLE_NO_EXCLUDE
    rows = []
    for o in obligations:
        addr = addressee_class(o["actor"])
        otype = obligation_type(addr, o["modal"], o["statement"])
        pred = decide_binding(role, addr, otype, binds_confidence=float(o.get("confidence") or 1.0))
        rows.append(
            {
                "addr": addr,
                "otype": otype,
                "pred": pred["binds"],
                "guarded": pred["guarded"],
                "true_binds": _true_binds(addr, otype, pure_aifm=pure_aifm),
            }
        )

    # Safety invariant: the only out_of_scope-eligible obligation type (transposition_instruction)
    # must never be assigned to an addressee other than member_state, or an entity duty could be
    # hidden. This is the structural guarantee behind binding_recall == 1.0; the eval checks it on
    # the 79 real findings.
    otype_dist = {}
    for r in rows:
        otype_dist[r["otype"]] = otype_dist.get(r["otype"], 0) + 1
    transposition_safe = all(
        not (r["otype"] == "transposition_instruction" and r["addr"] != "member_state")
        for r in rows
    )

    n = len(rows) or 1
    binding_truth = [r for r in rows if r["true_binds"] != "out_of_scope"]
    kept = [r for r in binding_truth if r["pred"] != "out_of_scope"]
    binding_recall = (len(kept) / len(binding_truth)) if binding_truth else 1.0

    predicted_oos = [r for r in rows if r["pred"] == "out_of_scope"]
    oos_correct = [r for r in predicted_oos if r["true_binds"] == "out_of_scope"]
    oos_precision = (len(oos_correct) / len(predicted_oos)) if predicted_oos else 1.0

    return {
        "config": "pure_aifm" if pure_aifm else "no_exclude",
        "n_obligations": len(rows),
        "binding_recall": round(binding_recall, 4),  # must be 1.0
        "out_of_scope_precision": round(oos_precision, 4),  # must be at least 0.95
        "scope_reduction": round(len(predicted_oos) / n, 4),  # fraction demoted to out_of_scope
        "n_out_of_scope": len(predicted_oos),
        "n_yes": sum(1 for r in rows if r["pred"] == "yes"),
        "n_conditional": sum(1 for r in rows if r["pred"] == "conditional"),
        "guarded_rate": round(sum(1 for r in rows if r["guarded"]) / n, 4),
        "obligation_type_dist": otype_dist,
        "transposition_safe": transposition_safe,  # no entity duty ever typed as transposition
        "ac_pass": binding_recall == 1.0 and oos_precision >= 0.95 and transposition_safe,
    }


def run(gold: Path | None = None) -> dict[str, Any]:
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
    ap = argparse.ArgumentParser(
        description="Score the entity/role applicability gate vs the 79 gold findings."
    )
    ap.add_argument(
        "--gold", default=None, help="path to the reviewed sample JSON (default: the B3-06a sample)"
    )
    args = ap.parse_args()
    report = run(Path(args.gold) if args.gold else None)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ac_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
