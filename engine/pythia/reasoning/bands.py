"""Confidence band: how confidently to present an answer the question gate has already cleared.

The metagate (question gate) owns the safety decision of whether to answer at all. ``grade()`` only
decides how confidently to present an answer the gate cleared as ``sound`` (or ``false_premise``,
the grounded answer that leads with a correction). It is a pure function of
``(C_retrieval, R_grounding, meta)``, so it can never change which provisions are cited, their
order, the verdict, or whether the answer is refused.

Invariants (tests/test_bands.py):
  * Presentation only. ``grade()`` never decides "refuse", "closest" or "notlegal"; those come from
    the gate route and the post-generation verification. It passes a non-cleared route through
    unchanged, and for a cleared answer it returns exactly "full" or "hedged".
  * answer.py calls it only when ``metagate_enabled`` is on. With the flag off, the answer's band,
    render_mode and hedge stay None.
  * Safe with uncalibrated inputs. C_retrieval (the raw top cosine similarity) is not calibrated, so
    the thresholds are presentation edges only. The worst outcome of a wrong band is an overly
    cautious amber banner, never a fabricated answer.

``meta`` (the MetaVerdict, or None) is read only to map a non-sound route to its render mode; the
full-or-hedged grade depends only on (C, R) against the two thresholds.
"""

from __future__ import annotations

from typing import Any

from pythia.contract import MetaVerdict

# Render mode for each non-sound gate route. The gate decided the route; the band only carries it to
# the presentation layer. `sound` and `false_premise` are cleared routes and are graded on (C, R)
# below.
_ROUTE_RENDER_MODE: dict[str, str] = {
    "not_a_legal_question": "notlegal",
    "not_in_corpus": "closest",
    # false_premise is a generated, grounded and verified answer that leads with a correction, so it
    # is cleared and graded full or hedged like sound. It has no static frame here: the app shows
    # the correction banner based on `meta.route`, and the band grades the corrected answer beneath
    # it.
}

# Amber banner copy shown when an answer is graded `hedged` (user-facing copy, subject to lawyer
# review). The band only attaches it; it never refuses or drops anything.
_HEDGE_COPY = "Nižší jistota — ověřte u odborníka; databáze toto téma pokrývá jen částečně."


def _hedge_banner() -> dict[str, Any]:
    """Return the amber banner a `hedged` answer carries (rendered above the sections).

    A fresh dict is returned on each call so callers can never mutate a shared object.
    """
    return {"level": "amber", "copy": _HEDGE_COPY}


def grade(
    c_retrieval: float | None,
    r_grounding: float | None,
    meta: MetaVerdict | None,
    *,
    t_full: float = 0.60,
    t_hedge: float = 0.40,
) -> dict[str, Any]:
    """Grade the presentation confidence of an answer the question gate has cleared.

    Returns ``{"band", "render_mode", "hedge"}``:
      * band: "full" or "hedged" for a cleared answer; for a non-sound route, the route's render
        mode (closest or notlegal), passed through unchanged.
      * render_mode: the frontend switch ("full", "hedged", "closest", "notlegal"). "refuse" is
        never produced here; answer.py only grades non-refused answers, and verification sets
        "refuse".
      * hedge: the amber banner dict when band is "hedged", otherwise None.

    A cleared answer is "full" only when both C_retrieval and R_grounding are at or above
    ``t_full``. Everything else, including grounded-but-thin (R below ``t_full``) and weak cosine
    (C between ``t_hedge`` and ``t_full``), is "hedged". There is no third grade: the gate already
    vouched for the answer and the band only tunes the badge.

    None, negative or NaN inputs clamp to 0.0, which pushes the grade toward "hedged", never toward
    a falsely confident "full".

    Non-sound routes (not_a_legal_question, not_in_corpus) map to their render mode with no hedge.
    answer.py only grades sound answers, so this branch is defensive; it lets a caller route every
    answer through ``grade()`` and guarantees the band never invents a closest or notlegal result.
    """
    # Pass a non-sound gate route through to its render mode (never re-decided here).
    if meta is not None and meta.route in _ROUTE_RENDER_MODE:
        rm = _ROUTE_RENDER_MODE[meta.route]
        return {"band": rm, "render_mode": rm, "hedge": None}

    c = _clamp01(c_retrieval)
    r = _clamp01(r_grounding)
    if c >= t_full and r >= t_full:
        return {"band": "full", "render_mode": "full", "hedge": None}
    # Grounded but thin, or weak cosine: hedged (cautious presentation of a real, cleared answer).
    return {"band": "hedged", "render_mode": "hedged", "hedge": _hedge_banner()}


def _clamp01(x: float | None) -> float:
    """Clamp a confidence value to [0, 1].

    None, NaN and unparseable values become 0.0, the cautious side that pushes toward "hedged".
    """
    try:
        v = float(x) if x is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN guard (NaN != NaN): cautious 0.0
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v
