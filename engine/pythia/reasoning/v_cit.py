"""Citation verifier: checks that a claim cites a provision and that the cited text supports it.

One of the per-claim verifiers the claim-verification orchestrator calls synchronously through
`verify(claim, evidence, settings) -> list[ClaimVerdict]`. Deterministic by default (no LLM,
database, clock or randomness); the only LLM use is an optional, quote-gated entailment check for
claims whose lexical support is ambiguous. Numbers and dates are never transformed: the support
score only reads tokens. Citation chips are read, never modified.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

from pythia.reasoning.claim_ledger import Claim, ClaimVerdict

_CHECK = "v_cit"

# Support thresholds: at least _PASS_AT passes, below _FAIL_BELOW fails, in between is ambiguous.
_PASS_AT: float = 0.5
_FAIL_BELOW: float = 0.2
_STEM_PREFIX: int = 6

# Slots whose normative claims need no citation: the restatement of the asker's facts (situation /
# vaše situace), the overall assessment (hodnocení) and the honest no-coverage gap. Written in
# folded lower-case ASCII so both the sections.py kinds and the seven-slot names match.
_CITATION_EXEMPT_SLOTS: frozenset[str] = frozenset(
    {
        "assessment",
        "hodnoceni",
        "situace",
        "vase_situace",
        "situation",
        "gap",
    }
)

# Czech and a few English function words that carry no legal signal. Matched against the
# diacritic-folded token, so 'ně'/'ne' and 'při'/'pri' collapse. Kept deliberately small: the
# six-character prefix stem already collapses inflection, and stripping more would hide real support
# or lack of support.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "i",
        "o",
        "u",
        "v",
        "k",
        "s",
        "z",
        "do",
        "na",
        "za",
        "po",
        "od",
        "ke",
        "ku",
        "se",
        "si",
        "je",
        "js",
        "by",
        "ze",
        "co",
        "ci",
        "ac",
        "az",
        "an",
        "ono",
        "ona",
        "oni",
        "ten",
        "tato",
        "toto",
        "the",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "as",
        "is",
        "be",
        "or",
        "and",
        "for",
        "ktery",
        "ktera",
        "ktere",
        "jako",
        "tim",
        "tato",
        "tento",
        "tato",
        "tyto",
        "vsak",
        "tedy",
        "pak",
        "pri",
        "pro",
        "nebo",
        "ale",
        "tak",
        "kdyz",
        "aby",
        "byt",
        "byl",
        "byla",
        "bylo",
        "jsou",
        "jest",
        "podle",
        "tzn",
        "resp",
        "apod",
        "atd",
        "musi",
        "muze",
        "lze",
        "ma",
        "maji",
        "jeho",
        "jejich",
        "tato",
        "této",
        "toho",
        "tom",
        "tomu",
        "ten",
        "této",
        "také",
        "take",
        "dale",
        "vice",
        "mene",
    }
)

# A content token is a run of letters or digits (Czech letters included). Numbers stay as they are,
# since they are verbatim facts. Punctuation and the § sign separate tokens.
_TOKEN_RE = re.compile(r"[0-9A-Za-zÀ-ž]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")

# Entailment prompt, used only when claim_entailment_llm_enabled is on and an entailment callable is
# supplied. Its hash is recorded so downstream consumers can tell exactly which prompt text was
# used. The callable chooses the model; this module only defines the prompt and requires the
# returned quote to be a verbatim (whitespace-collapsed) substring of the provision.
CLAIM_ENTAILMENT_PROMPT: str = (
    "Rozhodni, zda následující TVRZENÍ vyplývá z uvedeného USTANOVENÍ. "
    "Odpověz pouze citací DOSLOVNÉHO úryvku z ustanovení, který tvrzení podpírá; "
    "pokud žádný takový doslovný úryvek neexistuje, vrať prázdný řetězec. "
    "Nepřepisuj, neparafrázuj, necituj nic, co v ustanovení není.\n"
    "TVRZENÍ: {claim}\nUSTANOVENÍ: {provision}"
)
CLAIM_ENTAILMENT_PROMPT_HASH: str = hashlib.sha256(CLAIM_ENTAILMENT_PROMPT.encode("utf-8")).hexdigest()


def _fold(text: str) -> str:
    """Lower-case `text` and strip diacritics (NFD decomposition, combining marks dropped).

    Used for both token normalization and the verbatim-quote gate, so 'lhůtě' and 'lhute' match.
    """
    nfd = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in nfd if not unicodedata.combining(c))
    return stripped.lower()


def _content_tokens(text: str) -> set[str]:
    """Return the set of normalized content tokens of `text`.

    Each maximal alphanumeric run is diacritic-folded and lower-cased; stopwords and single letters
    are dropped, and the first `_STEM_PREFIX` characters serve as a crude stem that collapses Czech
    inflection without a full stemmer. Purely numeric tokens (dates, amounts) are kept whole,
    because a number is a fact. A set is returned so that repetition does not inflate the overlap
    score.
    """
    out: set[str] = set()
    for raw in _TOKEN_RE.findall(text):
        folded = _fold(raw)
        if folded in _STOPWORDS:
            continue
        if folded.isdigit():
            out.add(folded)  # a number is a fact: keep it whole, do not stem
            continue
        if len(folded) <= 1:  # single letters carry no signal
            continue
        out.add(folded[:_STEM_PREFIX])
    return out


def _support_score(claim_text: str, provision_texts: list[str]) -> tuple[float, int, int]:
    """Return the fraction of the claim's content tokens found in any of the provisions.

    Returns (score, n_claim_tokens, n_matched). A claim without content tokens yields (0.0, 0, 0);
    the caller treats it as not scorable (advisory), never as a failure.
    """
    claim_tokens = _content_tokens(claim_text)
    if not claim_tokens:
        return 0.0, 0, 0
    provision_tokens: set[str] = set()
    for pt in provision_texts:
        provision_tokens |= _content_tokens(pt)
    matched = claim_tokens & provision_tokens
    return len(matched) / len(claim_tokens), len(claim_tokens), len(matched)


def _cited_provision_ids(claim: "Claim") -> list[str]:
    """Return the distinct provision ids on the claim's chips, in first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for ch in claim.chips:
        pid = ch.get("provision_id")
        if pid is None:
            continue
        pid = str(pid)
        if pid not in seen:
            seen.add(pid)
            out.append(pid)
    return out


def _collapse_ws(text: str) -> str:
    """Return the whitespace-collapsed, diacritic-folded form used by the verbatim-quote gate."""
    return _WS_RE.sub(" ", _fold(text)).strip()


def _is_verbatim_quote(quote: str, provision_text: str) -> bool:
    """Return True if `quote` is non-empty and appears verbatim in the provision text.

    Both sides are whitespace-collapsed and diacritic-folded. This is the anti-hallucination gate:
    the entailment check counts a claim as supported only when the model's quote really occurs in
    the cited provision, so a paraphrase fails.
    """
    q = _collapse_ws(quote)
    if not q:
        return False
    return q in _collapse_ws(provision_text)


def _run_entailment(
    claim: "Claim",
    provision_texts: list[str],
    evidence: dict[str, Any],
) -> ClaimVerdict | None:
    """Run the quote-gated entailment check for a claim in the ambiguous support band.

    Uses the callable at `evidence['entailment_llm']`, a synchronous batched interface (the
    orchestrator runs this verifier synchronously): `entailment_llm(prompt, batch) -> list[str]`,
    one verbatim-quote answer per batch item, with an empty string meaning no supporting quote. This
    verifier submits a single item; batching across claims (at most 8 per call) is the caller's job.

    Returns:
        A pass verdict ('entailment_supported') when the quote is found verbatim in any cited
        provision; a fail verdict ('entailment_unsupported') when the quote is empty or not found;
        an advisory verdict ('entailment_error') when the call raises; or None when no callable is
        supplied, in which case the caller falls back to the advisory verdict.
    """
    fn = evidence.get("entailment_llm")
    if not callable(fn):
        return None
    prompt = CLAIM_ENTAILMENT_PROMPT.format(
        claim=claim.text.strip(),
        provision=" ‖ ".join(pt.strip() for pt in provision_texts),
    )
    try:
        # Single-item batch; the callable returns a list of quote strings.
        results = fn(prompt, [{"claim": claim.text, "provision_ids": [ch.get("provision_id") for ch in claim.chips]}])
    except Exception as exc:  # noqa: BLE001 - an LLM failure degrades to advisory
        return ClaimVerdict(
            check=_CHECK,
            status="advisory",
            reason="entailment_error",
            evidence={"error": type(exc).__name__, "prompt_hash": CLAIM_ENTAILMENT_PROMPT_HASH},
        )
    quote = ""
    if isinstance(results, list) and results:
        first = results[0]
        quote = first if isinstance(first, str) else str(first or "")
    supported = any(_is_verbatim_quote(quote, pt) for pt in provision_texts)
    if supported:
        return ClaimVerdict(
            check=_CHECK,
            status="pass",
            reason="entailment_supported",
            evidence={"quote": quote[:280], "prompt_hash": CLAIM_ENTAILMENT_PROMPT_HASH},
        )
    return ClaimVerdict(
        check=_CHECK,
        status="fail",
        reason="entailment_unsupported",
        evidence={"quote_returned": bool(quote), "quote": quote[:280], "prompt_hash": CLAIM_ENTAILMENT_PROMPT_HASH},
    )


def verify(claim: "Claim", evidence: dict[str, Any], settings: Any) -> list["ClaimVerdict"]:
    """Check one claim's citation and lexical support; returns exactly one ClaimVerdict.

    1. Citation presence: a normative claim with no chips fails ('uncited_normative_claim'), unless
       it sits in an exempt slot (situation, assessment, gap) or is a gap claim. Restating the
       asker's facts or honestly reporting missing coverage needs no citation. Non-normative claims
       are skipped.
    2. Support: the share of the claim's content tokens (stopwords removed, diacritics folded,
       six-character prefix stems) found in the full text of the cited provisions. A score of at
       least 0.5 passes ('cited_and_supported'); below 0.2 fails ('cited_but_unsupported').
    3. Ambiguous band (0.2 to 0.5): if `claim_entailment_llm_enabled` is on and an entailment
       callable is supplied, the quote-gated entailment check decides. Otherwise the verdict is
       advisory ('unverified_support'): an honest soft signal, not a hard fail, because without
       entailment the prose is only "advisory-plausible".

    Deterministic unless the entailment check runs.

    Args:
        claim: The claim to check.
        evidence: The shared evidence dict: `cited_texts` (`{provision_id: full provision text}`)
            and an optional synchronous `entailment_llm` callable.
        settings: Read only for `claim_entailment_llm_enabled`.
    """
    # Non-normative claims (headings, blank lines, disclaimers) assert nothing to verify.
    if not claim.normative:
        return [ClaimVerdict(check=_CHECK, status="skip", reason="non_normative_claim")]

    slot = _fold(str(claim.slot or "")).replace(" ", "_")
    exempt = (slot in _CITATION_EXEMPT_SLOTS) or (claim.type == "gap")
    pids = _cited_provision_ids(claim)

    # 1. Citation presence: a normative claim in a non-exempt slot without chips is a hard fail.
    if not pids:
        if exempt:
            return [
                ClaimVerdict(
                    check=_CHECK,
                    status="skip",
                    reason="citation_exempt_slot",
                    evidence={"slot": claim.slot, "type": claim.type},
                )
            ]
        return [
            ClaimVerdict(
                check=_CHECK,
                status="fail",
                reason="uncited_normative_claim",
                evidence={"slot": claim.slot, "type": claim.type},
            )
        ]

    # 2. Support: lexical score of the claim against the full text of its cited provisions.
    cited_texts: dict[Any, str] = evidence.get("cited_texts") or {}
    provision_texts: list[str] = []
    missing: list[str] = []
    for pid in pids:
        txt = cited_texts.get(pid)
        if txt is None:  # keys may be non-str (UUID); retry with str
            txt = cited_texts.get(str(pid))
        if isinstance(txt, str) and txt:
            provision_texts.append(txt)
        else:
            missing.append(pid)

    # Cited, but no provision text is in the evidence, so support cannot be scored. Advisory rather
    # than a fail: the citation is present, only the text needed to score it is missing.
    if not provision_texts:
        return [
            ClaimVerdict(
                check=_CHECK,
                status="advisory",
                reason="cited_text_unavailable",
                evidence={"provision_ids": pids, "missing": missing},
            )
        ]

    score, n_tokens, n_matched = _support_score(claim.text, provision_texts)
    ev = {"score": round(score, 4), "n_claim_tokens": n_tokens, "n_matched": n_matched, "provision_ids": pids}

    # No scorable tokens (only stopwords or single letters): advisory, never a false fail.
    if n_tokens == 0:
        return [ClaimVerdict(check=_CHECK, status="advisory", reason="no_scorable_tokens", evidence=ev)]

    if score >= _PASS_AT:
        return [ClaimVerdict(check=_CHECK, status="pass", reason="cited_and_supported", evidence=ev)]

    if score < _FAIL_BELOW:
        # No repoint repair is proposed here. Support is monotonic over the cited provisions, so if
        # another cited provision supported the claim the combined score would already pass. A valid
        # repoint needs the right provision among the retrieved but uncited ones, which requires a
        # citation resolver over the whole retrieved set. Repair proposals come from v_fact
        # (substitution) and v_calc (strip).
        return [ClaimVerdict(check=_CHECK, status="fail", reason="cited_but_unsupported", evidence=ev)]

    # 3. Ambiguous band (0.2 to 0.5): entailment check if enabled and supplied, else advisory.
    if getattr(settings, "claim_entailment_llm_enabled", False):
        ent = _run_entailment(claim, provision_texts, evidence)
        if ent is not None:
            # Merge the lexical score into the entailment verdict's evidence for the audit trail.
            ent.evidence = {**ev, **ent.evidence}
            return [ent]
    return [ClaimVerdict(check=_CHECK, status="advisory", reason="unverified_support", evidence=ev)]
