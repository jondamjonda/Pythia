"""Deterministic choice of a ``gap_kind`` for each claim demoted during claim enforcement.

When enforcement (``answer._run_claim_enforce``) demotes a claim it could not verify, it renders an
honest gap line (``gap_demote.GapLine``). The line's ``gap_source`` says why it was rendered (a
``vf_*`` verification failure). Its independent ``gap_kind`` (gap_kind.py) says what missing
material would have to be added to answer it, which is what the gap backlog (gap_backlog.py) ranks.

This module maps each demoted claim to one of the eight backlog codes
(``gap_kind.GAP_KIND_BACKLOG_CODES``) using ordered rules over signals the caller has already
resolved. The caller does any corpus lookups (is the act covered? does a delegating provision lack
an implements edge? is the topic soft-law only? is an annual instrument past its reissue date?) and
passes the answers as ``GapSignals``, so the rule itself stays pure and reproducible. Missing
signals fall back to ``no_fact_row``; the function never raises.

Rules (first match wins, most specific first):
  1. act not in extraction_coverage        -> extraction_not_run (coverage gap, not a fact gap)
  2. delegating provision, no implements edge -> decree_absent (implementing decree not ingested)
  3. annual instrument past reissue window -> annual_instrument_stale
  4. a `where` / authority slot            -> authority_locator (corpus names the body, not the
     branch)
  5. a `steps` / form / channel slot       -> form_portal (the form or portal is not in statute
     text)
  6. soft-law-only topic                   -> soft_law_absent (the guidance is not in the corpus)
  7. live text probe also found nothing    -> no_text_support
  8. otherwise (act covered, no fact row)  -> no_fact_row
"""

from __future__ import annotations

from dataclasses import dataclass

from pythia.reasoning.gap_kind import GAP_KIND_BACKLOG_CODES, GapKind

# Slots whose prose points to an authority ("Kam se obrátit"). A demoted claim here is an authority
# locator gap: the corpus names the type of authority, not the local office. Mirrors
# slot_verify._SLOT_GAP_CLASS["where"].
_WHERE_SLOTS: frozenset[str] = frozenset({"where", "authority"})

# Slots whose prose says how to submit. A demoted claim here is a form/portal gap: the online form
# or datová schránka channel is not in the statute text. Mirrors
# slot_verify._SLOT_GAP_CLASS["steps"].
_STEPS_SLOTS: frozenset[str] = frozenset({"steps", "form", "channel", "procedure_step"})


@dataclass(frozen=True)
class GapSignals:
    """Signals about a demoted claim, already resolved by the caller.

    The caller may have queried the corpus to fill these; the deriver is pure over them. Defaults
    are the safe values, so a bare ``GapSignals(slot="where")`` still derives correctly.

    Attributes:
        slot: The claim's action-plan slot ('where', 'steps', 'obligations', ...).
        act_covered: The claim's act is in the extraction_coverage manifest. False means the
            extractor never processed it (extraction_not_run, a coverage gap rather than a fact
            gap). Defaults to True so a missing signal never wrongly blames coverage.
        is_delegating: The claim's provision delegates a decree (has a rel-11 edge).
        has_implements_edge: The delegation has a matching rel-12 implements edge, i.e. the decree
            is in the corpus. Delegating without this edge gives decree_absent.
        annual_instrument_stale: The claim rests on an annual instrument (minimum-wage government
            regulation, sdělení) past its reissue window, as reported by the staleness checker.
        soft_law_only: The topic is covered only by soft law (metodika, stanovisko, sdělení) that is
            not in the corpus.
        text_probe_empty: A live regex probe over the provision's raw text also found nothing
            (no_text_support instead of no_fact_row). Consulted only once the act is covered.
    """

    slot: str = ""
    act_covered: bool = True
    is_delegating: bool = False
    has_implements_edge: bool = False
    annual_instrument_stale: bool = False
    soft_law_only: bool = False
    text_probe_empty: bool = False


def derive_gap_kind(signals: GapSignals) -> str:
    """Map a demoted claim's signals to one backlog code (a GapKind wire string).

    The first matching rule wins; empty or unrecognised signals fall back to ``no_fact_row``. The
    result is always in ``gap_kind.GAP_KIND_BACKLOG_CODES`` (the tests assert this), so the backlog
    rollup can rank it without further checks. Never raises.
    """
    slot = (signals.slot or "").strip().lower()

    # 1. A coverage gap wins: "no fact" for an act never extracted is not a real fact gap.
    if not signals.act_covered:
        return GapKind.EXTRACTION_NOT_RUN.value
    # 2. A delegating provision whose implementing decree is not in the corpus (rel-11, no rel-12).
    if signals.is_delegating and not signals.has_implements_edge:
        return GapKind.DECREE_ABSENT.value
    # 3. An annual instrument past its reissue window; the value in the corpus may be superseded.
    if signals.annual_instrument_stale:
        return GapKind.ANNUAL_INSTRUMENT_STALE.value
    # 4. An authority slot: the corpus names the body, not the concrete branch.
    if slot in _WHERE_SLOTS:
        return GapKind.AUTHORITY_LOCATOR.value
    # 5. A submission-channel or form slot: the online form or portal is not in the statute text.
    if slot in _STEPS_SLOTS:
        return GapKind.FORM_PORTAL.value
    # 6. A soft-law-only topic: the guidance holding the answer is not ingested.
    if signals.soft_law_only:
        return GapKind.SOFT_LAW_ABSENT.value
    # 7 and 8. The act was covered but has no fact: tell "no row" apart from "text probe also
    # empty".
    if signals.text_probe_empty:
        return GapKind.NO_TEXT_SUPPORT.value
    return GapKind.NO_FACT_ROW.value


# Re-exported so callers can import the deriver and the code set from one module.
BACKLOG_CODES: tuple[str, ...] = GAP_KIND_BACKLOG_CODES
