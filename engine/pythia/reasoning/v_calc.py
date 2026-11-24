"""Calculation verifier: flags numbers and dates that the draft computed instead of quoting.

Under the verbatim-only rule, a number or date asserted as law must appear verbatim in a cited
source. One that does not was computed by the model (days added, amounts summed, a percentage
inferred), and a computed value asserted as law is exactly the failure this check exists to catch.
The only sanctioned computation is a fully verified worked example behind
`claim_calc_worked_example_enabled`. Pure and deterministic: regex extraction and substring
membership, with symmetric whitespace collapsing as the only normalization.
"""

from __future__ import annotations

import re
from typing import Any

from pythia.reasoning.claim_ledger import Claim, ClaimVerdict

_CHECK = "v_calc"

# Note: `claim_typing_llm_enabled` is reserved and has no effect. Claim types come from the
# deterministic slot default and regex refinement (claims.py); an LLM typing assist would only be
# added if evaluation showed that deterministic typing misroutes claims.

# Caveat attached to a verified worked example: the figure is illustrative guidance re-derived from
# grounded inputs, not a value set by law. The wording is awaiting lawyer review.
_WORKED_EXAMPLE_CAVEAT = (
    "DRAFT: Uvedený výpočet je pouze ILUSTRATIVNÍ vodítko (worked example), NENÍ to zákonem stanovená "
    "hodnota — ověřte podle citovaného ustanovení."
)

# Number and date token patterns. Each captures the raw token exactly as written; the check tests
# whether that token (whitespace-normalized on both sides) occurs in a cited source. Ordered most
# specific first, so a date is matched as one token rather than split into numbers.
_DATE_DMY = re.compile(r"\b\d{1,2}\.\s?\d{1,2}\.\s?\d{4}\b")  # 1.1.2025 / 1. 1. 2025
_DATE_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")  # 2025-01-01
# A number with optional thousands groups (space, NBSP or thin space), decimal part and %.
_NUMBER = re.compile(r"\d{1,3}(?:[\s  ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?%?|\d+")

_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (_DATE_DMY, _DATE_ISO, _NUMBER)

# Whitespace characters that the symmetric collapse treats as interchangeable.
_WS_RUN = re.compile(r"[\s  ]+")


def _normalize_ws(text: str) -> str:
    """Collapse every whitespace run (including NBSP and thin space) to a single ASCII space.

    Applied to both the claim token and the source text, so it can never change a digit, a decimal
    separator or a date; it only neutralizes space-character mismatches (e.g. '5 000' written with a
    NBSP versus a plain space).
    """
    return _WS_RUN.sub(" ", text)


def _extract_value_tokens(text: str) -> list[str]:
    """Extract the raw number and date tokens of `text`, most specific first, without overlaps.

    Each token is an exact substring of `text`. Dates are matched whole rather than split into
    numbers.
    """
    n = len(text)
    claimed = [False] * n  # positions taken by a more specific pattern
    spans: list[tuple[int, int, str]] = []
    for pat in _TOKEN_PATTERNS:
        for m in pat.finditer(text):
            s, e = m.start(), m.end()
            if any(claimed[i] for i in range(s, e)):
                continue  # overlaps a more specific token: skip
            for i in range(s, e):
                claimed[i] = True
            spans.append((s, e, m.group(0)))
    spans.sort(key=lambda t: t[0])
    return [tok for _s, _e, tok in spans]


def _iter_source_texts(claim: "Claim", evidence: dict[str, Any]) -> list[str]:
    """Return the whitespace-normalized cited source texts available to this claim.

    Prefers the full texts of the claim's own chips from `evidence['cited_texts']`. If the claim has
    no chips, or none resolves to text, falls back to all cited texts: a value quoted from any cited
    source is not a fabrication. Keys may be provision-id strings or ints, so they are matched as
    strings.
    """
    cited_texts: dict = evidence.get("cited_texts") or {}
    # Normalize the cited_texts keys to strings once.
    norm_by_key: dict[str, str] = {}
    for k, v in cited_texts.items():
        if isinstance(v, str) and v:
            norm_by_key[str(k)] = _normalize_ws(v)

    chip_pids = [str(ch.get("provision_id")) for ch in getattr(claim, "chips", []) if ch.get("provision_id")]
    texts: list[str] = []
    if chip_pids:
        for pid in chip_pids:
            t = norm_by_key.get(pid)
            if t:
                texts.append(t)
        if texts:
            return texts
    # No chips, or none resolved to text: use every cited text.
    return list(norm_by_key.values())


def _claim_cites_own_provision(claim: "Claim", evidence: dict[str, Any]) -> bool:
    """Return True if one of the claim's own chips resolves to a cited source text.

    This is worked-example check 1 (rule present): the claim cites the provision whose rule the
    computation applies, rather than only borrowing text from the whole bundle. Mirrors the chip
    lookup in `_iter_source_texts`.
    """
    cited_texts: dict = evidence.get("cited_texts") or {}
    keys = {str(k) for k, v in cited_texts.items() if isinstance(v, str) and v}
    for ch in getattr(claim, "chips", []) or []:
        pid = ch.get("provision_id") if isinstance(ch, dict) else None
        if pid is not None and str(pid) in keys:
            return True
    return False


# Worked-example grammar. A claim shows its working as an inline expression `a op b = c`, with the
# operator (addition, subtraction, multiplication or division, in any of the listed spellings)
# written in the claim. a op b is evaluated over the two input tokens and compared with the result
# token c. No eval(): a fixed dispatch table over the operators.
_WORKED_EXPR = re.compile(
    r"(?P<a>\d+(?:[.,]\d+)?)\s*(?P<op>[+\-*/×·÷])\s*(?P<b>\d+(?:[.,]\d+)?)\s*=\s*(?P<c>\d+(?:[.,]\d+)?)"
)
_OPS = {
    "+": lambda a, b: a + b,
    "-": lambda a, b: a - b,
    "*": lambda a, b: a * b,
    "×": lambda a, b: a * b,
    "·": lambda a, b: a * b,
    "/": lambda a, b: (a / b) if b != 0 else None,
    "÷": lambda a, b: (a / b) if b != 0 else None,
}


def _to_number(tok: str) -> float | None:
    """Parse a claim number token ('30', '5,5', '1.5') to a float, accepting a Czech decimal comma.

    Returns None for anything it cannot parse; never raises.
    """
    try:
        return float(tok.replace(",", "."))
    except (ValueError, AttributeError):  # noqa: BLE001 - non-numeric token, not a worked input
        return None


class _Worked:
    """An inline worked expression `a op b = c` parsed from a claim.

    Holds the raw operand tokens (for the grounded-inputs check) and the numeric operands and result
    (for the arithmetic checks).
    """

    __slots__ = ("a_tok", "b_tok", "c_tok", "a", "b", "c", "op")

    def __init__(self, a_tok: str, op: str, b_tok: str, c_tok: str, a: float, b: float, c: float):
        self.a_tok, self.op, self.b_tok, self.c_tok = a_tok, op, b_tok, c_tok
        self.a, self.b, self.c = a, b, c


def _parse_worked(text: str) -> "_Worked | None":
    """Return the first inline worked expression `a op b = c` in the claim text, or None.

    None also covers operands that cannot be parsed.
    """
    m = _WORKED_EXPR.search(text or "")
    if m is None:
        return None
    a, b, c = _to_number(m.group("a")), _to_number(m.group("b")), _to_number(m.group("c"))
    if a is None or b is None or c is None:
        return None
    return _Worked(m.group("a"), m.group("op"), m.group("b"), m.group("c"), a, b, c)


def _numbers_close(x: float, y: float) -> bool:
    """Return True if x equals y up to floating-point representation error.

    The epsilon is not a rounding allowance: a worked example must reproduce the figure exactly.
    """
    return abs(x - y) <= 1e-9 * max(1.0, abs(x), abs(y))


def verify(claim: "Claim", evidence: dict[str, Any], settings: Any) -> list["ClaimVerdict"]:
    """Check that every number and date in a claim is quoted from a cited source, not computed.

    Returns exactly one verdict:

    - skip 'no_computed_value': the claim contains no number or date.
    - skip 'no_source_available': numbers or dates are present but there is no cited source text to
      check them against. Conservative: neither a silent pass nor a fail on missing evidence.
    - pass 'all_values_in_source': every number and date token appears verbatim in a cited source.
    - pass 'calc_worked_example_verified': only with `claim_calc_worked_example_enabled` on (default
      off; enabling it requires lawyer sign-off). The claim shows its working as `a op b = c` and
      all four checks hold:

      1. Rule present: the claim's own chips cite the governing provision, so the computation
         applies a provision rather than thin air.
      2. Inputs grounded: both operands appear verbatim in a cited source.
      3. Arithmetic correct: recomputing `a op b` reproduces c.
      4. Result matches the claim: c is the only unquoted figure, so the working accounts for every
         computed value the claim asserts.

      The recomputation is a fixed evaluation over the two grounded operands (no LLM, no eval()). It
      never invents a value; it only confirms the figure the draft already wrote.
    - fail 'computed_value_not_in_source': at least one token is absent from every cited source and
      the claim is not a fully verified worked example. The evidence names the failing value for the
      strip repair and, for a failed worked example, the result of each check.
    """
    text = claim.text or ""
    tokens = _extract_value_tokens(text)
    if not tokens:
        return [ClaimVerdict(check=_CHECK, status="skip", reason="no_computed_value")]

    sources = _iter_source_texts(claim, evidence)
    if not sources:
        return [
            ClaimVerdict(
                check=_CHECK,
                status="skip",
                reason="no_source_available",
                evidence={"tokens": tokens, "n_tokens": len(tokens)},
            )
        ]

    # A token is in the source if its whitespace-normalized form is a substring of a normalized
    # cited source. Only space characters are normalized, never digits or dates.
    missing: list[str] = []
    for tok in tokens:
        needle = _normalize_ws(tok)
        if not any(needle in src for src in sources):
            missing.append(tok)

    if not missing:
        return [
            ClaimVerdict(
                check=_CHECK,
                status="pass",
                reason="all_values_in_source",
                evidence={"tokens": tokens, "n_tokens": len(tokens), "n_sources": len(sources)},
            )
        ]

    # At least one computed value, so the default is a fail (verbatim-only rule). With the
    # worked-example flag on and the working shown as `a op b = c`, the claim passes only if all
    # four checks hold; otherwise it falls through to the same fail as with the flag off.
    if getattr(settings, "claim_calc_worked_example_enabled", False):
        worked = _parse_worked(text)
        if worked is not None:
            # 1. Rule present: the claim's own chips cite the governing provision.
            leg_rule = _claim_cites_own_provision(claim, evidence)
            # 2. Inputs grounded: both operand tokens appear verbatim in a cited source.
            leg_inputs = all(
                any(_normalize_ws(op_tok) in src for src in sources) for op_tok in (worked.a_tok, worked.b_tok)
            )
            # 3. Arithmetic correct: recomputing a op b reproduces the stated result c. A wrong sum
            #    such as 15 + 15 = 40 fails here.
            recomputed = _OPS[worked.op](worked.a, worked.b)
            leg_arith = recomputed is not None and _numbers_close(recomputed, worked.c)
            # 4. Result matches the claim: the unquoted tokens are exactly {c}. If the claim asserts
            #    any other unexplained figure (shows '=30' but then states '35 dnů'), that token
            #    stays in `missing` and this check fails: the working does not account for the value
            #    the claim asserts.
            leg_result = {_normalize_ws(v) for v in missing} == {_normalize_ws(worked.c_tok)}
            legs = {
                "rule_present": leg_rule,
                "inputs_grounded": leg_inputs,
                "arithmetic_correct": leg_arith,
                "result_matches_claim": leg_result,
            }
            if all(legs.values()):
                return [
                    ClaimVerdict(
                        check=_CHECK,
                        status="pass",
                        reason="calc_worked_example_verified",
                        evidence={
                            "worked_example": True,
                            "legs": legs,
                            "expression": f"{worked.a_tok} {worked.op} {worked.b_tok} = {worked.c_tok}",
                            "recomputed": recomputed,
                            "tokens": tokens,
                            "caveat": _WORKED_EXAMPLE_CAVEAT,
                        },
                    )
                ]
            # A worked expression that fails any check falls through to the default fail below,
            # annotated with the check results so the repair loop and reviewers can see why it did
            # not verify.
            _worked_fail_legs = legs
        else:
            _worked_fail_legs = None
    else:
        _worked_fail_legs = None

    _fail_evidence: dict[str, Any] = {
        "computed_values": missing,
        "n_computed": len(missing),
        "tokens": tokens,
        "n_sources": len(sources),
        # Repair evidence: the failing computed value (a verbatim substring of the claim) that the
        # strip repair removes. The repair loop re-verifies the stripped claim, so a wrong span is
        # caught and demoted, never replaced by a manufactured value.
        "failing_subspan": missing[0] if missing else "",
    }
    if _worked_fail_legs is not None:
        _fail_evidence["worked_example_legs"] = _worked_fail_legs

    return [ClaimVerdict(check=_CHECK, status="fail", reason="computed_value_not_in_source", evidence=_fail_evidence)]
