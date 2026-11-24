"""Fact verifier: checks deadline, amount and authority claims verbatim against cited provisions.

Every fact token extracted from such a claim must appear verbatim (after whitespace collapsing only,
never a numeric or date transformation) in a cited provision's full text or in one of its
practical-fact values. Pure and deterministic: it never queries the database and never edits the
answer. Its verdicts are recorded in the claim ledger and, in enforce mode, drive the repair loop.
"""

from __future__ import annotations

import re
from typing import Any

from pythia.reasoning import practical_facts
from pythia.reasoning.claim_ledger import Claim, ClaimVerdict
from pythia.reasoning.claims import _RE_AMOUNT, _RE_AUTHORITY, _RE_DEADLINE

_CHECK = "v_fact"

# Claim types this verifier checks. Others are skipped (they only arrive through a routing bug).
_FACT_TYPES: frozenset[str] = frozenset({"deadline", "amount", "authority"})

# Claim type to the regexes that extract its fact tokens. These are the atomizer's compiled patterns
# from claims.py, so extraction cannot drift from the atomizer's typing. Deadlines use a different
# extractor (see _extract_tokens).
_TOKEN_RE: dict[str, list[re.Pattern[str]]] = {
    "deadline": [_RE_DEADLINE],
    "amount": [_RE_AMOUNT],
    "authority": [_RE_AUTHORITY],
}

# Czech authority lexicon, with inflected forms listed. Reused read-only from practical_facts, which
# holds the only such lexicon in the codebase.
_AUTHORITY_LEXICON: list[re.Pattern[str]] = list(practical_facts._AUTHORITY_PATTERNS)

# Shape of a concrete office or branch locator. Statutes name competent bodies only generically; the
# specific branch for a place is resolved only by the app-side authority registry (a
# municipality-to-okres/ORP/kraj gazetteer). An authority claim that reads like such a locator but
# is in neither the provision text nor the lexicon fails 'authority_only_in_registry' rather than
# being invented. Conservative: it only matches the explicit place or branch qualifiers that the
# registry, not statute, supplies.
_P3_LOCATOR_RE = re.compile(
    r"\b(?:pro\s+(?:okres|obec|m[eě]sto|kraj|[uú]zem[ií])|"  # "… pro okres/obec/město/kraj …"
    r"pobo[cč]k[aeuy]|[uú]zemn[ií]\s+pracovi[sš]t[eě]|"  # "pobočka" / "územní pracoviště"
    r"m[ií]stn[ěe]\s+p[rř][ií]slu[sš]n)",  # "místně příslušný …"
    re.IGNORECASE | re.UNICODE,
)


def _collapse_ws(s: str) -> str:
    """Collapse whitespace runs to single spaces and trim: the only transformation allowed.

    Case, digits and letters are kept verbatim, so '30 dnů' never matches '30dnů' or '13 dnů'.
    """
    return re.sub(r"\s+", " ", s).strip()


def _extract_tokens(claim_text: str, claim_type: str) -> list[str]:
    """Extract the claim's fact values as verbatim, whitespace-collapsed strings, de-duplicated.

    Deadlines use the extractor that populates practical_fact
    (`practical_facts.extract_practical_facts`) rather than the atomizer's `_RE_DEADLINE`. That
    regex is only a typing signal: it truncates the number (``do 30 dnů`` becomes ``do 3``) and
    yields inflected keywords (``Lhůta``), which would falsely pass a wrong ``do 3 dnů`` source and
    falsely fail an inflected match. The practical-fact value_raw is the whole ``do 30 dnů …``
    phrase, so the substring check is precise.
    """
    out: list[str] = []
    seen: set[str] = set()
    if claim_type == "deadline":
        for f in practical_facts.extract_practical_facts(claim_text):
            if f.fact_type == "deadline":
                tok = _collapse_ws(f.value_raw)
                if tok and tok not in seen:
                    seen.add(tok)
                    out.append(tok)
        return out
    for pat in _TOKEN_RE.get(claim_type, ()):
        for m in pat.finditer(claim_text):
            tok = _collapse_ws(m.group(0))
            if tok and tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out


def _claim_pids(claim: "Claim", evidence: dict[str, Any]) -> list[str]:
    """Return the provision ids (as strings, de-duplicated) the claim's fact tokens may come from.

    The claim's own chips come first (the most specific), followed by every cited provision in the
    evidence. String ids key into the cited_texts and fact_rows maps uniformly.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(pid: Any) -> None:
        if pid is None:
            return
        p = str(pid)
        if p and p not in seen:
            seen.add(p)
            out.append(p)

    for ch in getattr(claim, "chips", None) or []:
        if isinstance(ch, dict):
            _add(ch.get("provision_id"))
    # Also include the whole cited set, so a loosely cited token still has somewhere to be found.
    cited_texts = evidence.get("cited_texts") or {}
    if isinstance(cited_texts, dict):
        for pid in cited_texts:
            _add(pid)
    return out


def _fact_values_for(pid: str, evidence: dict[str, Any]) -> list[str]:
    """Return the practical-fact value_raw strings for a provision (source b).

    Read from `evidence['fact_rows']` (`{pid: [row]}`), where a row is a dict with 'value_raw' or an
    object with a `value_raw` attribute (such as fact_lookup.FactRow). A missing map or pid gives
    [], so source (b) is skipped for that provision rather than failed. Never touches the database.
    """
    fact_rows = evidence.get("fact_rows")
    if not isinstance(fact_rows, dict):
        return []
    rows = fact_rows.get(pid) or fact_rows.get(str(pid)) or []
    values: list[str] = []
    for r in rows:
        v = r.get("value_raw") if isinstance(r, dict) else getattr(r, "value_raw", None)
        if isinstance(v, str) and v:
            values.append(v)
    return values


def _temporal_shadow(pids: list[str], evidence: dict[str, Any]) -> dict[str, Any]:
    """Return a temporal annotation if any of the provisions' fact rows is known to be superseded.

    Fact rows fetched with `with_temporal=True` carry a fail-open `temporal_superseded` flag: True
    means known not to be in force; False or absent means current or unknown and is not flagged. If
    any row is flagged, returns `{'temporal_superseded': True, 'temporal_pids': [...]}` for the
    verdict evidence, otherwise {}.

    This never changes the pass or fail status; it only makes a superseded fact row visible. Acting
    on it (a temporal verdict that changes the output) is not wired here.
    """
    fact_rows = evidence.get("fact_rows")
    if not isinstance(fact_rows, dict):
        return {}
    flagged: list[str] = []
    for pid in pids:
        for r in fact_rows.get(pid) or fact_rows.get(str(pid)) or []:
            sup = r.get("temporal_superseded") if isinstance(r, dict) else getattr(r, "temporal_superseded", None)
            if sup is True:
                flagged.append(str(pid))
                break
    return {"temporal_superseded": True, "temporal_pids": flagged} if flagged else {}


def _token_in_sources(token: str, pids: list[str], evidence: dict[str, Any]) -> bool:
    """Return True if `token` appears verbatim in a cited provision's text or practical-fact values.

    Whitespace collapsing, applied identically to the token and the text searched, is the only
    normalization; numbers and dates are never transformed.
    """
    cited_texts = evidence.get("cited_texts") or {}
    for pid in pids:
        # (a) Full provision text.
        full = cited_texts.get(pid) if isinstance(cited_texts, dict) else None
        if isinstance(full, str) and token in _collapse_ws(full):
            return True
        # (b) Practical-fact value_raw rows, if supplied.
        for val in _fact_values_for(pid, evidence):
            if token in _collapse_ws(val):
                return True
    return False


def _authority_in_lexicon(token: str) -> bool:
    """Return True if the token matches the authority lexicon, i.e. names a competent body."""
    return any(pat.search(token) for pat in _AUTHORITY_LEXICON)


def _looks_like_p3_locator(token: str) -> bool:
    """Return True if the text reads like a concrete office or branch locator.

    Statutes name the body generically, so a specific branch qualifier comes from the app-side
    authority registry (the municipality-to-okres/ORP/kraj gazetteer), not from a provision.
    Conservative: requires an explicit qualifier.
    """
    return bool(_P3_LOCATOR_RE.search(token))


# Claim type to the fact types a substitution repair may take its correct value from.
_SUBST_FACT_TYPES: dict[str, frozenset[str]] = {
    "deadline": frozenset({"deadline"}),
    "amount": frozenset({"amount", "penalty", "fee", "min_wage_coefficient"}),
}


def _fact_substitution(ctype: str, missing: list[str], pids: list[str], evidence: dict[str, Any]) -> dict:
    """Propose a substitution repair when the cited provisions offer exactly one correct value.

    When fact rows of a matching category yield a single unambiguous verbatim value, returns
    `{wrong_value, correct_value, source_text}` so the repair step can replace the claim's wrong
    token. correct_value is a fact-row value_raw and therefore verbatim in the provision text
    (source_text) by construction; `substitute_value` re-checks that and the repair loop re-verifies
    the result, so a wrong substitution is caught and demoted, never manufactured. Returns {} (the
    claim is then demoted) unless exactly one distinct candidate exists.
    """
    allowed = _SUBST_FACT_TYPES.get(ctype)
    if not allowed:
        return {}
    fact_rows = evidence.get("fact_rows")
    cited_texts = evidence.get("cited_texts") or {}
    if not isinstance(fact_rows, dict):
        return {}
    cands: list[tuple[str, str]] = []  # (value_raw, source_text)
    for pid in pids:
        src = cited_texts.get(pid) or cited_texts.get(str(pid)) or ""
        for r in fact_rows.get(pid) or fact_rows.get(str(pid)) or []:
            ft = str((r.get("fact_type") if isinstance(r, dict) else getattr(r, "fact_type", "")) or "")
            vr = str((r.get("value_raw") if isinstance(r, dict) else getattr(r, "value_raw", "")) or "")
            if vr and ft in allowed and src:
                cands.append((vr, src))
    distinct = {v for v, _ in cands}
    if len(distinct) != 1:
        return {}
    correct, source = cands[0]
    wrong = missing[0]
    if _collapse_ws(correct) == _collapse_ws(wrong):
        return {}
    return {"wrong_value": wrong, "correct_value": correct, "source_text": source}


def verify(claim: "Claim", evidence: dict[str, Any], settings: Any) -> list["ClaimVerdict"]:
    """Check a claim's deadline, amount or authority tokens verbatim against its cited sources.

    Each token must appear verbatim (after whitespace collapsing only) in one of two sources for a
    cited provision:

    (a) the provision's full text, `evidence["cited_texts"][pid]`; or
    (b) a practical-fact value_raw from `evidence["fact_rows"]`, which answer.py fills via
        fact_lookup. Without fact rows, source (b) is skipped, not failed.

    Returns exactly one verdict:

    - type is not deadline, amount or authority: skip 'not_a_fact_claim'.
    - no fact token in the claim text: skip 'no_fact_token'.
    - no cited provision to search: skip 'no_cited_provision'.
    - an authority token in neither source nor the lexicon, in a claim shaped like a registry
      locator: fail 'authority_only_in_registry' (the verifier never invents a locator).
    - any token missing from both sources: fail 'fact_token_not_in_source', with the expected
      tokens, the searched provisions and, when unambiguous, a substitution proposal.
    - every token found: pass 'fact_tokens_grounded'.

    Skips are conservative: never a silent pass and never a false fail.
    """
    ctype = getattr(claim, "type", None)
    if ctype not in _FACT_TYPES:
        return [ClaimVerdict(check=_CHECK, status="skip", reason="not_a_fact_claim", evidence={"claim_type": ctype})]

    tokens = _extract_tokens(getattr(claim, "text", "") or "", ctype)
    if not tokens:
        return [ClaimVerdict(check=_CHECK, status="skip", reason="no_fact_token", evidence={"claim_type": ctype})]

    pids = _claim_pids(claim, evidence)
    if not pids:
        return [ClaimVerdict(check=_CHECK, status="skip", reason="no_cited_provision", evidence={"expected": tokens})]

    # Temporal annotation for the claim's provisions: empty unless fact rows fetched with temporal
    # data show one is known to be superseded. Merged into the verdict evidence below; it never
    # changes the status.
    _temporal = _temporal_shadow(pids, evidence)

    claim_text = getattr(claim, "text", "") or ""
    missing: list[str] = []
    for token in tokens:
        if _token_in_sources(token, pids, evidence):
            continue
        # The token is in no cited source. If the claim names a concrete office or branch that only
        # the app-side registry resolves (statute names it only generically), fail
        # 'authority_only_in_registry': the verifier must never invent a locator. Otherwise it is a
        # plain missing token. A lexicon hit only confirms the token is an authority, not that this
        # citation supports it, so it still counts as missing.
        if ctype == "authority" and not _authority_in_lexicon(token) and _looks_like_p3_locator(claim_text):
            return [
                ClaimVerdict(
                    check=_CHECK,
                    status="fail",
                    reason="authority_only_in_registry",
                    evidence={"expected": token, "searched_pids": list(pids)},
                )
            ]
        missing.append(token)

    if missing:
        return [
            ClaimVerdict(
                check=_CHECK,
                status="fail",
                reason="fact_token_not_in_source",
                evidence={
                    "expected": missing,
                    "searched_pids": list(pids),
                    **_fact_substitution(ctype, missing, pids, evidence),
                    **_temporal,
                },
            )
        ]

    return [
        ClaimVerdict(
            check=_CHECK,
            status="pass",
            reason="fact_tokens_grounded",
            evidence={"tokens": tokens, "searched_pids": list(pids), **_temporal},
        )
    ]
