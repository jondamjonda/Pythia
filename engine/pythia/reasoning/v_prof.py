"""Profile verifier: checks what a claim says about the asker against the asker's verified profile.

The other verifiers check claims against the law; none checks them against the asker. A
`situation_application` line that restates the asker's situation ("ve vašem případě jako s.r.o. …")
could silently misstate a fact, such as calling an s.r.o. an a.s. or saying the business processes
personal data when the profile says it does not. This verifier closes that gap. Pure and
deterministic: no LLM, database or clock.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from pythia.reasoning.claim_ledger import Claim, ClaimVerdict

_CHECK = "v_prof"

# Stable reason codes. The repair step and gap bucketing key off the fail code; the skip codes
# record why the check could not reach a verdict.
_R_FAIL = "asker_fact_unverified"  # an asserted asker fact contradicts or is absent from the record
_R_PASS = "asker_fact_consistent"  # every asserted asker fact matches the verified record
_R_SKIP_NO_FACTS = "no_asker_facts"  # no intake or profile facts to verify against
_R_SKIP_NO_ASSERT = "no_asker_assertion"  # the claim asserts nothing on a recognised profile axis
_R_SKIP_NOT_SITUATION = "not_situation_claim"  # defensive: a non-situation claim was routed here

# Gap-source tag on a fail verdict's evidence, so gap bucketing files it under profile gaps.
_GAP_SOURCE = "vf_profile"


# Normalization
def _norm(s: Any) -> str:
    """Fold diacritics and case and collapse whitespace runs, for token comparison.

    Total: a non-string value is coerced with str().
    """
    text = unicodedata.normalize("NFKD", str(s))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _norm_legal_form(s: Any) -> str:
    """Normalize a legal-form value to a canonical token.

    Dotted and spaced abbreviation variants (s.r.o., s. r. o., sro) and the spelled-out names map to
    the same token, so the comparison does not depend on how the asker or the draft wrote the form.
    """
    base = _norm(s)
    compact = base.replace(".", "").replace(" ", "")
    _CANON = {
        "sro": "sro",
        "spolsro": "sro",
        "spolecnostsrucenimomezenym": "sro",
        "as": "as",
        "akciovaspolecnost": "as",
        "osvc": "osvc",
        "zivnostnik": "osvc",
        "podnikatel": "osvc",
        "vos": "vos",
        "ks": "ks",
        "spolek": "spolek",
        "sdruzeni": "spolek",
        "nadace": "nadace",
        "ustav": "ustav",
        "prispevkovaorganizace": "prispevkova_org",
    }
    return _CANON.get(compact, compact)


# Asker-fact assertion detectors: what the claim says about the asker. A detector returns None when
# the claim makes no assertion on its axis, otherwise the asserted value (a normalized token or a
# bool). They only classify; the claim text is never transformed.

# legal_form assertions in the claim ("jako s.r.o.", "vaše a.s.", "jste osvč / živnostník").
_LF_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("sro", re.compile(r"\bs\.?\s?r\.?\s?o\.?\b|spole[čc]nost[ií]?\s+s\s+ru[čc]en[íi]m\s+omezen", re.I)),
    ("as", re.compile(r"\ba\.?\s?s\.?\b|akciov[áa]\s+spole[čc]nost", re.I)),
    ("osvc", re.compile(r"\bosv[čc]\b|[žz]ivnostn[íi]k|[žz]ivnostensk", re.I)),
    ("vos", re.compile(r"\bv\.?\s?o\.?\s?s\.?\b|ve[řr]ejn[áa]\s+obchodn[íi]", re.I)),
    ("ks", re.compile(r"\bk\.?\s?s\.?\b|komanditn[íi]\s+spole[čc]nost", re.I)),
    ("spolek", re.compile(r"\bspolek\b|\bspolku\b|\bspolkem\b", re.I)),
]

# Gate assertions. A positive pattern asserts the gate holds for the asker; a negated pattern
# asserts it does not. A gate counts as asserted only when one of these fires, so a claim that
# mentions the topic without ascribing it to the asker is left alone (no false accusation).
_NEG = r"(?:ne|nen[íi]|nemus[íi]te?|nezpracov|nezam[ěe]stn)"


def _gate_assertion(claim: str, pos: re.Pattern, neg: re.Pattern | None) -> bool | None:
    """Return whether the claim asserts that a gate holds for the asker.

    True or False when the claim asserts that the gate holds or does not (a negated match wins);
    None when it makes no asker-scoped assertion on this axis.
    """
    if neg is not None and neg.search(claim):
        return False
    if pos.search(claim):
        return True
    return None


# is_employer — "zaměstnáváte", "jako zaměstnavatel", "vaši zaměstnanci".
_EMP_POS = re.compile(r"zam[ěe]stn[áa]v[áa]te|jako\s+zam[ěe]stnavatel|va[šs]i?\s+zam[ěe]stnanc", re.I)
_EMP_NEG = re.compile(r"nezam[ěe]stn[áa]v[áa]te|nejste\s+zam[ěe]stnavatel|nem[áa]te\s+zam[ěe]stnanc", re.I)
# processes_personal_data — "zpracováváte osobní údaje".
_PD_POS = re.compile(r"zpracov[áa]v[áa]te\s+osobn[íi]\s+[úu]daj|nakl[áa]d[áa]te\s+s\s+osobn[íi]mi", re.I)
_PD_NEG = re.compile(r"nezpracov[áa]v[áa]te\s+osobn[íi]|nenakl[áa]d[áa]te\s+s\s+osobn[íi]mi", re.I)
# handles_client_money — "spravujete peníze klientů", "klientské prostředky".
_CM_POS = re.compile(r"pen[íi]ze\s+klient|klientsk[éě]\s+prost[řr]edk|spravujete\s+prost[řr]edky", re.I)
_CM_NEG = re.compile(r"nespravujete\s+(?:pen[íi]ze|prost[řr]edky)\s+klient", re.I)
# has_real_estate — "vlastníte nemovitost".
_RE_POS = re.compile(r"vlastn[íi]te\s+nemovitost|va[šs][ei]\s+nemovitost", re.I)
_RE_NEG = re.compile(r"nevlastn[íi]te\s+nemovitost", re.I)
# cross_border — "působíte přeshraničně", "v zahraničí".
_CB_POS = re.compile(r"p[řr]eshrani[čc]n|p[ůu]sob[íi]te\s+v\s+zahrani[čc][íi]", re.I)
_CB_NEG = re.compile(r"nep[ůu]sob[íi]te\s+v\s+zahrani[čc][íi]|pouze\s+v\s+[čc]esk", re.I)

_GATE_ASSERT: dict[str, tuple[re.Pattern, re.Pattern | None]] = {
    "is_employer": (_EMP_POS, _EMP_NEG),
    "processes_personal_data": (_PD_POS, _PD_NEG),
    "handles_client_money": (_CM_POS, _CM_NEG),
    "has_real_estate": (_RE_POS, _RE_NEG),
    "cross_border": (_CB_POS, _CB_NEG),
}


def _detect_legal_form(claim: str) -> str | None:
    """Return the legal form the claim ascribes to the asker (canonical token), or None."""
    for token, pat in _LF_PATTERNS:
        if pat.search(claim):
            return token
    return None


# The verified asker record, built from the evidence
def _get_field(fact: Any, key: str) -> Any:
    """Read a field from an IntakeFact-like object or a plain dict."""
    if isinstance(fact, dict):
        return fact.get(key)
    return getattr(fact, key, None)


def _build_record(evidence: dict[str, Any]) -> dict[str, Any]:
    """Fold the intake facts and raw profile context into a flat asker record keyed by profile axis.

    Axes: 'legal_form' (canonical token), the five gate booleans, and 'municipality' and
    'headcount_band' (normalized). An axis is present only when a source actually carries it;
    unknown keys are ignored. No claim detector asserts on municipality or headcount yet, so those
    axes are recorded but not compared. Returns {} when there are no intake or profile facts at all;
    the caller then skips.
    """
    rec: dict[str, Any] = {}

    # 1. Intake facts (intake.IntakeFact; a closed set of fields, from the query or the profile).
    facts = evidence.get("intake_facts")
    if isinstance(facts, (list, tuple)):
        for f in facts:
            field = _get_field(f, "field")
            value = _get_field(f, "value")
            if not field:
                continue
            if field == "legal_form" and value:
                rec["legal_form"] = _norm_legal_form(value)
            elif field == "region" and value:
                rec.setdefault("municipality", _norm(value))
            # Intake gate fields hold a value like "yes"; map them to their gate booleans.
            elif field == "employment" and value:
                rec.setdefault("is_employer", _truthy(value))
            elif field == "gdpr" and value:
                rec.setdefault("processes_personal_data", _truthy(value))
            elif field == "licensing" and value:
                # licensing is only a proxy for handles_client_money / has_real_estate. It is
                # recorded for completeness, but no specific gate is inferred from it, so it never
                # causes a contradiction.
                rec.setdefault("licensing", _truthy(value))

    # 2. The saved profile (project_context) is authoritative: it overrides intake on the same axis.
    profile = evidence.get("profile_context")
    if isinstance(profile, dict):
        lf = profile.get("legal_form")
        if isinstance(lf, str) and lf.strip():
            rec["legal_form"] = _norm_legal_form(lf)
        for gate in (
            "is_employer",
            "processes_personal_data",
            "handles_client_money",
            "has_real_estate",
            "cross_border",
        ):
            if gate in profile and profile.get(gate) is not None:
                rec[gate] = bool(profile.get(gate))
        muni = profile.get("municipality") or profile.get("municipality_code")
        if muni is not None and str(muni).strip():
            rec["municipality"] = _norm(muni)
        hb = profile.get("headcount_band")
        if hb is not None and str(hb).strip():
            rec["headcount_band"] = _norm(hb)

    return rec


def _truthy(value: Any) -> bool:
    """Return True for an affirmative intake gate value ('yes', 'ano', 'true', '1', 'y')."""
    return _norm(value) in {"yes", "ano", "true", "1", "y"}


def verify(claim: "Claim", evidence: dict[str, Any], settings: Any) -> list["ClaimVerdict"]:
    """Check what a `situation_application` claim says about the asker against the verified record.

    The claim is scanned for assertions on a closed set of profile axes: the legal form and five
    gates (employer, personal data, client money, real estate, cross-border). Each is compared with
    the asker record built from the evidence, as normalized tokens; neither the claim nor the record
    is rewritten.

    Returns exactly one verdict:

    - skip 'not_situation_claim': defensive; a non-situation claim was routed here.
    - skip 'no_asker_facts': the evidence has no intake or profile facts, so nothing can be
      verified.
    - skip 'no_asker_assertion': the claim asserts nothing on a recognized profile axis.
    - fail 'asker_fact_unverified': an asserted fact contradicts the record or is missing from it.
      The evidence carries gap_source='vf_profile' (bucketed as a profile gap) and per-axis mismatch
      details.
    - pass 'asker_fact_consistent': every asserted fact matches the record.

    A skip is conservative: never a silent pass and never a false accusation.

    Args:
        claim: The claim to check.
        evidence: May hold `intake_facts` (IntakeFact objects or `{field, value, source}` dicts: the
            validated intake and profile facts) and `profile_context` (the whitelisted
            project_context dict: legal_form, headcount_band, the five gate booleans,
            municipality_code / municipality). Either may be absent; with neither, the verifier
            skips.
        settings: Unused.
    """
    # Only situation_application claims are routed here; skip others rather than invent a verdict.
    if getattr(claim, "type", None) != "situation_application":
        return [
            ClaimVerdict(
                check=_CHECK,
                status="skip",
                reason=_R_SKIP_NOT_SITUATION,
                evidence={"claim_type": getattr(claim, "type", None)},
            )
        ]

    if not isinstance(evidence, dict):
        return [ClaimVerdict(check=_CHECK, status="skip", reason=_R_SKIP_NO_FACTS, evidence={})]

    record = _build_record(evidence)
    if not record:
        # No verified asker facts at all: skip, since being unable to verify is not being wrong.
        return [ClaimVerdict(check=_CHECK, status="skip", reason=_R_SKIP_NO_FACTS, evidence={})]

    text = claim.text or ""

    # Collect each (axis, asserted value) the claim states about the asker.
    assertions: list[tuple[str, Any]] = []
    lf = _detect_legal_form(text)
    if lf is not None:
        assertions.append(("legal_form", lf))
    for gate, (pos, neg) in _GATE_ASSERT.items():
        val = _gate_assertion(text, pos, neg)
        if val is not None:
            assertions.append((gate, val))

    if not assertions:
        # The claim ascribes no profile fact to the asker, so there is nothing to check.
        return [ClaimVerdict(check=_CHECK, status="skip", reason=_R_SKIP_NO_ASSERT, evidence={})]

    # An assertion on an axis the record lacks, or one that contradicts the record, is a mismatch.
    mismatches: list[dict[str, Any]] = []
    matched: list[str] = []
    for axis, asserted in assertions:
        if axis not in record:
            mismatches.append({"axis": axis, "asserted": asserted, "record": None, "kind": "absent"})
            continue
        recorded = record[axis]
        if asserted != recorded:
            mismatches.append({"axis": axis, "asserted": asserted, "record": recorded, "kind": "contradict"})
        else:
            matched.append(axis)

    if mismatches:
        return [
            ClaimVerdict(
                check=_CHECK,
                status="fail",
                reason=_R_FAIL,
                evidence={
                    "gap_source": _GAP_SOURCE,
                    "mismatches": mismatches,
                    "matched_axes": matched,
                },
            )
        ]

    return [ClaimVerdict(check=_CHECK, status="pass", reason=_R_PASS, evidence={"matched_axes": matched})]
