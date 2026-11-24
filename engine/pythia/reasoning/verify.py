"""Programmatic grounding check and refusal decision for a drafted answer (no LLM call).

The drafting step produces a cited answer. This module checks that it is grounded, i.e. that every
inline citation maps to a provision that was actually retrieved, and decides ok, abstain or refuse.

The local LLM formats citations inconsistently ("[240/2013 § 297]", "[240/2013, § 297]",
"[§ 297 of Act 240/2013]", "(čl. 27 32014L0065)", ...), and a single strict regex would refuse
otherwise good answers. Since the retrieved provisions are known, the check runs the other way: it
scans citation spans tolerantly, extracts (act, § number) and matches them against the retrieved
set. That is robust to formatting yet cannot fabricate: a citation counts only if it matches a
provision that was actually retrieved.

Policy:
  * ok: at least one citation is grounded and the grounding ratio is at least ``min_grounding``.
  * abstain: no citations, but the draft explicitly says no provision is on point. This is an
    honest, valid legal outcome and is never presented as a firm answer.
  * refuse: citations exist but none is grounded (hallucination); or there are no citations and no
    honest no-answer (an ungrounded assertion); or the grounding ratio is below the floor.

Confidence is grounded citations divided by total citations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A citation sits in a [..] or (..) group. Either bracket type matches, and the other delimiter is
# allowed inside so a "§ 318(1)" paragraph suffix within [..] is kept. ground() then requires both
# an act id and a § or Article number inside. Other brackets are skipped and a hallucinated act
# simply does not ground. This tolerates the local LLM's variation in order, separators and spacing.
_SPAN_RE = re.compile(r"\[([^\]\n]{1,160})\]|\(([^)\n]{1,160})\)")
# Act identifier inside a span: a Czech "N/YYYY" Sb. number or an EU CELEX number (e.g. 32014L0065).
_EXT_RE = re.compile(r"\b(\d{1,4}/\d{4}|[0-9]{5}[A-Z][0-9]{4})\b")
# Section or article number: "§ 297", "Article 27", "Art. 27", "čl. 27" give "297" or "27"
# (paragraph suffix dropped). The alphabetic markers need a left word boundary so words such as
# "part 27", "start 5" or "particle 7" do not match as citations; such false matches would both let
# an answer with no real citation through and inflate the ungrounded count. The § sign is a boundary
# by itself.
_SEC_RE = re.compile(r"(?:§|\bčl\.?|\bart(?:icle)?\.?)\s*(\d+[a-z]?)", re.I)
# Explicit, honest no-answer phrases (English and Czech) lead to abstain rather than a refusal.
_ABSTAIN_RE = re.compile(
    r"(none of the (?:provisions|above)|no (?:provision|applicable provision)|not on point|"
    r"cannot (?:answer|be answered)|insufficient (?:information|provisions)|"
    r"žádné? z? ?(?:uveden|dostupn|těchto)|neobsahuj|nelze (?:odpovědět|určit|posoudit))",
    re.I,
)


@dataclass
class Verdict:
    verdict: str  # "ok" | "abstain" | "refuse"
    confidence: float  # grounded / total citations (0..1)
    grounded_provision_ids: list[str] = field(default_factory=list)
    n_citations: int = 0
    n_grounded: int = 0
    ungrounded: list[str] = field(default_factory=list)  # cited "act §N" not in the retrieved set
    reason: str | None = None
    # One entry per grounded inline citation span: {start, end, provision_id, label, corpus}. The
    # offsets are the _SPAN_RE / _SEC_RE match offsets in the draft, so the UI can anchor a
    # clickable citation chip. Every provision_id is also in grounded_provision_ids (no chip without
    # a grounded id). The map is purely additional; the ids, counts and verdict do not depend on it.
    citations_map: list[dict] = field(default_factory=list)


def _sec_num(label: str | None) -> str | None:
    m = _SEC_RE.search(label or "")
    return m.group(1).lower() if m else None


def ground(draft: str, provisions: list[tuple[str, str, str]]) -> tuple[list[str], list[str], int, list[dict]]:
    """Ground a draft's citations against the retrieved provisions.

    Two passes, both matched only against what was actually retrieved, so nothing is fabricated:

      1. Bracketed, act-qualified citations such as "[240/2013 § 297]" (any spacing, order or
         separator) are matched exactly on (act, § number). An act and § that were not retrieved are
         reported as ungrounded.
      2. Bare citations in prose such as "podle § 297 zákona", "Article 27" or "čl. 27" (the local
         LLM's usual style, with the act left implicit) are matched by § number. A § number that was
         not retrieved stays ungrounded; only the implicit act is resolved, to the retrieved
         provision with that section. Matching is per section: "§ 318(1)" counts as "§ 318".

         A bare "§ X" is bound only when exactly one retrieved act has that section number. If two
         or more retrieved acts share § X, the implicit act is ambiguous and the citation is
         recorded as ungrounded rather than linked to a possibly wrong statute. It still counts as a
         citation, so an ambiguous reference can lower the grounding ratio and change the verdict.

    Each citation is counted once: distinct (act, §) for bracketed ones, distinct § for bare ones
    not already seen in brackets.

    Returns:
        ``(grounded_provision_ids, ungrounded_citations, n_citations, citations_map)``.
        ``citations_map`` has one ``{start, end, provision_id, label, corpus}`` entry per grounded
        span: the match offsets in the draft (for a clickable chip), the matched text as ``label``
        and the resolved provision's act id as ``corpus``. Every provision id in it is also
        grounded.
    """
    by_act_sec: dict[tuple[str, str], str] = {}
    # Per section number, track the distinct retrieved acts and one representative provision id, so
    # a bare "§ X" is resolved only when exactly one act has it. `by_sec_pid` supplies the provision
    # id for the unambiguous case; `sec_to_acts` detects ambiguity.
    by_sec_pid: dict[str, str] = {}
    sec_to_acts: dict[str, set[str]] = {}
    pid_to_ext: dict[str, str] = {}  # provision id to act id (the chip's `corpus`)
    for pid, ext, label in provisions:
        pid_to_ext.setdefault(pid, (ext or "").strip())
        sec = _sec_num(label)
        if not sec:
            continue
        by_sec_pid.setdefault(sec, pid)
        if ext:
            act = ext.strip()
            by_act_sec.setdefault((act, sec), pid)
            sec_to_acts.setdefault(sec, set()).add(act)
        else:
            # A retrieved provision without an act id still occupies the section number. Record an
            # opaque marker so a bare "§ X" sharing it is treated as ambiguous rather than bound to
            # the wrong act.
            sec_to_acts.setdefault(sec, set()).add(f"__noext__:{pid}")

    draft = draft or ""
    grounded: dict[str, None] = {}  # ordered de-dup of grounded provision ids
    ungrounded: list[str] = []
    citations_map: list[dict] = []  # chips for grounded citations only
    bracket_keys: set[tuple[str, str]] = set()
    bracket_secs: set[str] = set()
    for sm in _SPAN_RE.finditer(draft):
        span = sm.group(1) if sm.group(1) is not None else sm.group(2)
        em, secm = _EXT_RE.search(span), _SEC_RE.search(span)
        if not (em and secm):
            continue  # a bracket without both an act id and a § or Article is not a citation
        act, sec = em.group(1).strip(), secm.group(1).lower()
        if (act, sec) in bracket_keys:
            continue
        bracket_keys.add((act, sec))
        bracket_secs.add(sec)
        pid = by_act_sec.get((act, sec))
        if pid is not None:
            grounded.setdefault(pid, None)
            citations_map.append(
                {
                    "start": sm.start(),
                    "end": sm.end(),
                    "provision_id": pid,
                    "label": (sm.group(0) or "").strip(),
                    "corpus": pid_to_ext.get(pid) or act,
                }
            )
        else:
            ungrounded.append(f"{act} §{sec}")

    bare_secs: set[str] = set()
    for m in _SEC_RE.finditer(draft):
        sec = m.group(1).lower()
        if sec in bracket_secs or sec in bare_secs:
            continue  # already counted via a bracketed citation, or already seen
        bare_secs.add(sec)
        acts = sec_to_acts.get(sec)
        if acts is None:
            ungrounded.append(f"§{sec}")  # section number not retrieved at all: ungrounded
        elif len(acts) > 1:
            # Ambiguous: two or more retrieved acts have this §. The implicit act cannot be
            # resolved, so the citation grounds to nothing (no grounded id, no chip) and is recorded
            # as "§N (ambiguous act)", which the confidence calculation counts as an invalid span.
            # Never link it to a guessed act.
            ungrounded.append(f"§{sec} (ambiguous act)")
        else:
            pid = by_sec_pid.get(sec)  # exactly one act has this §: bind it
            if pid is not None:
                grounded.setdefault(pid, None)
                citations_map.append(
                    {
                        "start": m.start(),
                        "end": m.end(),
                        "provision_id": pid,
                        "label": (m.group(0) or "").strip(),
                        "corpus": pid_to_ext.get(pid) or "",
                    }
                )

    n = len(bracket_keys) + len(bare_secs)
    return list(grounded), ungrounded, n, citations_map


def verify(draft: str, provisions: list[tuple[str, str, str]], *, min_grounding: float = 0.34) -> Verdict:
    """Decide ok, abstain or refuse for a drafted answer (see the module docstring for the policy)."""
    grounded, ungrounded, n, citations_map = ground(draft, provisions)
    n_valid = n - len(ungrounded)  # valid citation spans (a provision cited twice counts twice)
    conf = (n_valid / n) if n else 0.0
    # Citation chips accompany only verdicts that carry grounded ids (the floor refusal and ok
    # below). The no-citation and wholly ungrounded refusals and the honest abstain ground nothing,
    # so their map stays empty; a chip never appears without its grounded provision id.
    if n == 0:
        if _ABSTAIN_RE.search(draft or ""):
            return Verdict("abstain", 0.0, [], 0, 0, [], "no provision on point (honest abstention)")
        return Verdict("refuse", 0.0, [], 0, 0, [], "answer made no grounded citation — withheld")
    if n_valid == 0:
        return Verdict(
            "refuse",
            0.0,
            [],
            n,
            0,
            ungrounded,
            "every citation is to a provision that was not retrieved (possible hallucination)",
        )
    if conf < min_grounding:
        return Verdict(
            "refuse",
            round(conf, 2),
            grounded,
            n,
            n_valid,
            ungrounded,
            f"grounding {conf:.0%} below the {min_grounding:.0%} floor — mostly ungrounded",
            citations_map,
        )
    return Verdict("ok", round(conf, 2), grounded, n, n_valid, ungrounded, None, citations_map)


def apply_faithfulness(verdict: Verdict, unsupported: list[str], *, strict: bool) -> Verdict:
    """Fold a faithfulness check into a grounded verdict.

    The citation check shows that a claim cites a retrieved provision; the faithfulness check shows
    that the provision's text actually supports the claim (an answer can cite § 297 and still
    misstate what it says).

    The result is advisory by default: an LLM fact-check is itself noisy, so unsupported claims are
    reported and confidence is capped, but a grounded answer is not refused. With ``strict``
    (verify='always') any unsupported claim refuses the answer. No-op unless the verdict is 'ok' and
    unsupported claims were found.
    """
    if verdict.verdict != "ok" or not unsupported:
        return verdict
    if strict:
        return Verdict(
            "refuse",
            verdict.confidence,
            verdict.grounded_provision_ids,
            verdict.n_citations,
            verdict.n_grounded,
            verdict.ungrounded,
            f"{len(unsupported)} claim(s) not supported by the cited provisions (strict verify)",
            verdict.citations_map,
        )
    return Verdict(
        "ok",
        round(min(verdict.confidence, 0.6), 2),
        verdict.grounded_provision_ids,
        verdict.n_citations,
        verdict.n_grounded,
        verdict.ungrounded,
        f"grounded, but {len(unsupported)} claim(s) may overstate the cited provisions (advisory)",
        verdict.citations_map,
    )
