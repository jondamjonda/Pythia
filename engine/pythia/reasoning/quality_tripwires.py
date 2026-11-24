"""Answer-quality tripwires: an observe-only check of finished answers against thresholds.

An answer can pass grounding and still be weak in ways the verdict does not show: it grounded no
citations, its practical action plan came back empty, the claim-verify loop demoted many slots, or
it was hedged. Those signals are already computed for the assembled answer. This module compares
them with configured thresholds and reports which tripwires fired, so the answer path can emit
telemetry.

  * Pure: ``evaluate()`` takes a ``QualitySignals`` snapshot and ``TripwireThresholds`` and returns
    a list of ``TripwireHit``. No config, database, clock or LLM.
  * Observe-only: it never aborts, demotes or refuses; the caller records telemetry and leaves the
    answer unchanged.
  * The caller runs it only when ``config.quality_tripwires_enabled`` is on (off by default).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class QualitySignals:
    """Signals of an assembled answer, taken from objects the caller has already built.

    The caller reads these from the PythiaAnswer, the verdict and the claim ledger; nothing is
    recomputed. Every field has an inert default, so a signal the answer does not carry never
    trips its tripwire.
    """

    grounded_citations: int = 0  # verdict.n_grounded: citations that resolved to a provision
    verdict: str = "ok"  # "ok" | "abstain" | "refuse"
    hedged: bool = False  # band render_mode == "hedged" (a low-confidence presentation)
    practical_present: bool = False  # a structured practical action-plan block was produced
    practical_empty: bool = False  # ...but it had no practical items (empty action plan)
    demoted_slots: int = 0  # claim-ledger slots demoted to a gap line (demoted_gap)
    dropped_claims: int = 0  # claims the claim loop dropped (safety or unverifiable)
    per_status: dict[str, int] = field(default_factory=dict)  # ledger count per final status


@dataclass(frozen=True)
class TripwireThresholds:
    """Thresholds for each tripwire.

    The caller fills these from config; the defaults match config's defaults so a bare instance is
    meaningful in tests. A numeric threshold of None disables that tripwire.
    """

    max_demoted_slots: int | None = 3  # fire when demoted_slots > this
    max_dropped_claims: int | None = 2  # fire when dropped_claims > this
    # Boolean tripwires fire on the condition itself; they are switches here so the whole set is
    # config-driven.
    zero_grounded: bool = True  # fire when an OK answer grounded 0 citations
    empty_practical: bool = True  # fire when a practical block was produced but empty
    hedged: bool = True  # fire when the answer was hedged


@dataclass(frozen=True)
class TripwireHit:
    """One tripwire that fired: its ``name``, the observed ``value`` and the ``threshold`` crossed.

    The caller turns it into a telemetry event; values are primitives so the event serialises to
    JSON cleanly.
    """

    name: str
    value: float | int | bool
    threshold: float | int | bool


def evaluate(sig: QualitySignals, th: TripwireThresholds) -> list[TripwireHit]:
    """Return the tripwires ``sig`` crosses under ``th``, in a fixed order.

    Never raises and never mutates its inputs. An empty list means nothing fired.
    """
    hits: list[TripwireHit] = []

    # Zero grounded: verification passed the answer, yet no citation resolved to a provision, so it
    # looks grounded with nothing behind it. Only meaningful for an OK verdict; a refusal or
    # abstention is expected to ground nothing.
    if th.zero_grounded and sig.verdict == "ok" and sig.grounded_citations <= 0:
        hits.append(TripwireHit("zero_grounded_citations", sig.grounded_citations, 0))

    # Empty practical block: the action plan was produced but has no items, so the user got headings
    # with no concrete steps or deadlines.
    if th.empty_practical and sig.practical_present and sig.practical_empty:
        hits.append(TripwireHit("empty_practical_block", True, True))

    # Hedged: the answer was shown as low confidence (amber). A quality signal, not a fault.
    if th.hedged and sig.hedged:
        hits.append(TripwireHit("hedged_answer", True, True))

    # Demoted slots: the claim-verify loop demoted more than max_demoted_slots claims to gap lines.
    if th.max_demoted_slots is not None and sig.demoted_slots > th.max_demoted_slots:
        hits.append(TripwireHit("demoted_slots", sig.demoted_slots, th.max_demoted_slots))

    # Dropped claims: the loop dropped more than max_dropped_claims claims (safety or unverifiable).
    if th.max_dropped_claims is not None and sig.dropped_claims > th.max_dropped_claims:
        hits.append(TripwireHit("dropped_claims", sig.dropped_claims, th.max_dropped_claims))

    return hits
