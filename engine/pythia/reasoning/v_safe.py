"""Safety verifier (V-SAFE): per-claim check for acting on the asker's behalf and for temporal honesty.

The claim router appends V-SAFE to every normative claim. It runs two deterministic checks over the
claim text and its evidence (no LLM, database, clock or network) and returns one ClaimVerdict:

  1. Reserved advice. The product may describe the law, summarise procedures, help draft documents
     and advise on the asker's own matter, all as information. It must not act as the asker's agent.
     A claim that reads as the product representing the asker, or filing, submitting, lodging,
     serving, suing, appearing or signing for them or in their name, fails with
     ``reserved_advice_claim``.

     The pattern list is copied verbatim from the app's
     ``regsuper.analysis.compliance_guard._RESERVED_ADVICE_RE``; the engine must not import the app.
     ``TIER1_PATTERN_VERSION`` is a hash of the pattern source, and parity tests on both sides fail
     when the two copies drift apart.

  2. Temporal honesty. A claim that presents a rule as current ("aktuálně platí", "je účinný",
     present-tense current-law wording) while a cited provision is not in force at ``as_of``
     (superseded, repealed, annulled, not yet in force) fails with ``temporal_inconsistent``. The
     temporal status comes from the evidence bundle (per-provision ``pit_status`` /
     ``temporal_status``, the same vocabulary as answer.py's ``_temporal_marker``); it is never
     re-derived or read from a clock. Without a temporal signal the check does not fire: no signal
     means no verdict, never a false "inconsistent".

V-SAFE only classifies; it never changes a number, date or quote. Evidence dicts are JSON-safe.

A normative claim gets exactly one verdict: ``fail`` if either check trips (reserved advice wins as
the more severe signal), otherwise ``pass`` (``lawful_lane_ok``). Non-normative and 'gap' claims are
never routed here; if one arrives anyway it gets ``skip`` (``non_normative``), never a silent pass.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from pythia.reasoning.claim_ledger import Claim, ClaimVerdict

_CHECK = "v_safe"

# Reserved-advice ("acting on the asker's behalf") patterns, copied verbatim from the app's
# `regsuper.analysis.compliance_guard._RESERVED_ADVICE_RE`; the engine does not import the app. The
# only forbidden class is the product acting as the asker's agent: representing them, or filing,
# submitting, lodging, serving, suing, appearing or signing for them or in their name. Summaries,
# procedures, court preparation, drafting and advice on the asker's own matter are not matched; they
# are allowed as information under the disclaimer. High-precision markers only.
#
# Parity: `TIER1_PATTERN_VERSION` is the first 16 hex digits of the SHA-256 of `_TIER1_PATTERN_SRC`.
# Parity tests pin it, so any drift from the app's list fails loudly and the copy gets re-synced.
_TIER1_PATTERN_SRC: str = (
    r"(?:"
    # Czech: represent me, act or file for me, in my name. Object pronoun: mě, me or mne.
    r"(?:zastup|zastoup|reprezent)\w*\s+m(?:ě|e|ne)\b"
    r"|m(?:ě|e|ne)\s+(?:\w+\s+){0,3}(?:zastup|zastoup|reprezent)\w*"
    r"|právní\s+zastoupen"
    r"|zastoupen[íi]\s+(?:u\s+soud|před\s+soud|v\s+(?:soudním\s+)?řízení)"
    # File, submit, draft or fill in for me; allows an object between the verb and "za mě".
    r"|(?:podej(?:te|íte)?|podat|pode[jš]le|sepi[šs](?:te)?|sepsat|vypl[ňn](?:te|it)?)\b[^.?!\n]{0,40}\bza\s+m(?:ě|e|ne)\b"
    # In my name: the verb may come after or before "mým jménem".
    r"|m(?:ým|ym)\s+jménem\s+(?:podej|podat|zastup|jednej|jednat|reprezent)"
    r"|(?:podej(?:te|íte)?|podat|zastup\w*|jednej(?:te)?|jednat|reprezent\w*|sepi[šs](?:te)?|sepsat)\b[^.?!\n]{0,40}\bm(?:ým|ym)\s+jménem\b"
    r"|jednej(?:te)?\s+za\s+m(?:ě|e|ne)\b"
    # English: represent, act, file, submit, sue or appear for me or on my behalf.
    r"|represent\s+me\b"
    r"|legal\s+representation\b"
    r"|on\s+my\s+behalf\b"
    r"|act\s+for\s+me\b"
    r"|(?:file|lodge|submit|sue|appear)\b[^.?!\n]{0,40}\bfor\s+me\b"
    r")"
)
_RESERVED_ADVICE_RE = re.compile(_TIER1_PATTERN_SRC, re.IGNORECASE)

# Parity hash of the pattern source. Re-syncing this copy after the app's list changes changes the
# hash, and the parity tests flag it; the two copies must stay identical. Exposed so tests can
# assert it directly.
TIER1_PATTERN_VERSION: str = hashlib.sha256(_TIER1_PATTERN_SRC.encode("utf-8")).hexdigest()[:16]


# Temporal-honesty vocabulary. A claim presenting a rule as current while its cited provision is not
# in force at as_of is inconsistent. The not-in-force sets match answer.py's `_temporal_marker`, so
# both agree on what "not current law" means. pit_status is the provision's point-in-time status;
# temporal_status is the document-level status. Values are normalised for case and hyphens before
# lookup.
_PIT_SUPERSEDED: frozenset[str] = frozenset({"SUPERSEDED"})
_PIT_NOT_YET: frozenset[str] = frozenset({"NOT_YET_IN_FORCE"})
_DOC_SUPERSEDED: frozenset[str] = frozenset({"repealed", "superseded", "annulled", "abolished", "annulled_by_cc"})
_DOC_NOT_YET: frozenset[str] = frozenset({"future_effective", "not_yet_in_force", "not_yet_effective"})

# Wording that presents a rule as current law: present-tense Czech (and a little English), tolerant
# of case and missing diacritics. Detection only. The claim must positively assert currency for a
# not-in-force citation to count as inconsistent; a neutral historical statement ("do roku 2023
# platilo...") does not.
_RE_ASSERTS_CURRENT = re.compile(
    r"\b(?:aktu[áa]ln[ěe]\s+plat[íi]|nyn[íi]\s+plat[íi]|st[áa]le\s+plat[íi]|plat[íi]\s+(?:i\s+)?dnes"
    r"|je\s+ú[čc]inn[ýyáa]|jsou\s+ú[čc]inn|je\s+(?:st[áa]le\s+)?v\s+platnosti|je\s+v\s+ú[čc]innosti"
    r"|sou[čc]asn[áá]?\s+(?:pr[áa]vn[íi]\s+)?[úu]prava|dle\s+(?:sou[čc]asn[éě]|platn[éě])"
    r"|currently\s+in\s+force|is\s+currently\s+in\s+effect|is\s+in\s+force)\b",
    re.IGNORECASE,
)


def _norm_pit(v: Any) -> str:
    return str(v or "").strip().upper().replace("-", "_")


def _norm_doc(v: Any) -> str:
    return str(v or "").strip().lower().replace("-", "_")


def _is_not_in_force(pit_status: Any, temporal_status: Any) -> bool:
    """Return True if the provision is not current law at as_of.

    Not current means superseded, repealed, annulled or not yet in force. Uses the same vocabulary
    as answer.py's ``_temporal_marker`` so the claim check and the prose caveat agree. A blank or
    unknown status counts as in force: the absence of a not-in-force marker is not evidence that the
    provision is stale.
    """
    pit = _norm_pit(pit_status)
    doc = _norm_doc(temporal_status)
    if pit in _PIT_SUPERSEDED or doc in _DOC_SUPERSEDED:
        return True
    if pit in _PIT_NOT_YET or doc in _DOC_NOT_YET:
        return True
    return False


def _provision_temporal(evidence: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return ``{provision_id: {'pit_status': ..., 'temporal_status': ...}}`` from the evidence bundle.

    Reads the optional ``provision_temporal`` map. Returns ``{}`` when the bundle has none, in which
    case the temporal check does not fire.
    """
    out: dict[str, dict[str, Any]] = {}
    pt = evidence.get("provision_temporal")
    if isinstance(pt, dict):
        for pid, info in pt.items():
            if isinstance(info, dict):
                out[str(pid)] = info
    return out


def _claim_cited_pids(claim: "Claim") -> list[str]:
    """Return the distinct provision ids cited by this claim, in order of first appearance."""
    seen: set[str] = set()
    pids: list[str] = []
    for ch in claim.chips:
        if not isinstance(ch, dict):
            continue
        pid = ch.get("provision_id")
        if pid is None:
            continue
        s = str(pid)
        if s not in seen:
            seen.add(s)
            pids.append(s)
    return pids


def _temporal_verdict(claim: "Claim", evidence: dict[str, Any]) -> ClaimVerdict | None:
    """Return a ``temporal_inconsistent`` failure if the claim presents a not-in-force rule as current.

    Returns None when there is no defect or no temporal signal. Nothing is read from a clock; the
    as_of status comes entirely from the per-provision fields in the evidence.
    """
    if not _RE_ASSERTS_CURRENT.search(claim.text or ""):
        return None  # the claim does not assert currency, so there is nothing to contradict
    pt = _provision_temporal(evidence)
    if not pt:
        return None  # no temporal signal in the evidence
    for pid in _claim_cited_pids(claim):
        info = pt.get(pid)
        if not isinstance(info, dict):
            continue
        pit = info.get("pit_status")
        doc = info.get("temporal_status")
        if _is_not_in_force(pit, doc):
            return ClaimVerdict(
                check=_CHECK,
                status="fail",
                reason="temporal_inconsistent",
                evidence={
                    "provision_id": pid,
                    "pit_status": _norm_pit(pit) or None,
                    "temporal_status": _norm_doc(doc) or None,
                    "asserted_current": True,
                    "as_of": evidence.get("as_of"),
                },
            )
    return None


def verify(claim: "Claim", evidence: dict[str, Any], settings: Any) -> list["ClaimVerdict"]:
    """Return exactly one V-SAFE verdict for a normative claim.

      * ``fail`` / ``reserved_advice_claim``: the claim reads as acting on the asker's behalf.
      * ``fail`` / ``temporal_inconsistent``: the claim presents a rule as current while a cited
        provision is not in force at as_of.
      * ``pass`` / ``lawful_lane_ok``: neither defect.

    Reserved advice wins when both apply (higher severity). A non-normative or 'gap' claim, which
    the router never sends here, gets ``skip`` / ``non_normative`` so it is never a silent pass.
    """
    if not getattr(claim, "normative", True) or getattr(claim, "type", None) == "gap":
        return [ClaimVerdict(check=_CHECK, status="skip", reason="non_normative")]

    text = claim.text or ""
    evidence = evidence if isinstance(evidence, dict) else {}

    # 1. Reserved advice (acting on the asker's behalf). Highest severity, so checked first.
    m = _RESERVED_ADVICE_RE.search(text)
    if m:
        return [
            ClaimVerdict(
                check=_CHECK,
                status="fail",
                reason="reserved_advice_claim",
                evidence={
                    "matched_signal": m.group(0).strip()[:80],
                    "pattern_version": TIER1_PATTERN_VERSION,
                },
            )
        ]

    # 2. Temporal honesty: presented as current while a cited provision is not in force at as_of.
    tv = _temporal_verdict(claim, evidence)
    if tv is not None:
        return [tv]

    # 3. Neither defect.
    return [
        ClaimVerdict(
            check=_CHECK,
            status="pass",
            reason="lawful_lane_ok",
            evidence={"pattern_version": TIER1_PATTERN_VERSION},
        )
    ]
