"""Retrieval-gap probe: tells a fabricated citation apart from one that retrieval missed.

``verify.ground()`` marks a cited § as ungrounded when it is not among the retrieved provisions.
That covers two different failures: (a) the § does not exist in the corpus (a fabricated or
out-of-corpus citation, a real hard failure), and (b) the § exists but was not retrieved for this
answer (a recall miss, not a hallucination).

This module holds the pure half of the probe. It parses the ungrounded citation strings back into
``(act, §)`` pairs, selects a capped set that names an act, and turns the lookup outcomes into
``verify_evidence`` telemetry rows. The corpus lookup itself is ``NexusPort.resolve_citation``;
answer.py runs the async probe and calls the helpers here.

The probe only informs verification. A ``missed_in_corpus`` finding is telemetry (and a valid target
for the later citation repoint step); it never adds a citation to the served answer's
``cited_provisions`` or ``citations_map``. A ``not_in_corpus`` finding stays the usual fabricated-
citation hard failure. At most ``GAP_PROBE_CAP`` lookups are made per answer.
"""

from __future__ import annotations

import re

# Per-answer probe budget. answer.py never issues more resolve_citation calls than this, even when
# the draft has more act-qualified ungrounded citations.
GAP_PROBE_CAP = 8

# verify.ground() reports an act-qualified ungrounded citation as f"{act} §{sec}" (for example
# "240/2013 §297"). The bare ("§297") and ambiguous ("§297 (ambiguous act)") forms name no act, so
# there is nothing to resolve and they are skipped. The regex mirrors verify's output format; it
# does not parse free prose.
_UNGROUNDED_ACT_RE = re.compile(r"^(?P<ext>\S+)\s+§\s*(?P<sec>\d+[a-z]?)$", re.I)


def parse_probe_targets(ungrounded: list[str], *, cap: int = GAP_PROBE_CAP) -> list[tuple[str, str]]:
    """Parse ungrounded citation strings into a capped, de-duplicated list of ``(ext, sec)`` pairs.

    Only act-qualified entries (``"{act} §{sec}"``) yield a target; a bare or ambiguous ``§N`` has
    no act to look up and stays a plain ungrounded citation. First-appearance order is preserved and
    the cap is applied after de-duplication, so at most ``cap`` distinct pairs are returned.
    """
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for raw in ungrounded or []:
        m = _UNGROUNDED_ACT_RE.match((raw or "").strip())
        if not m:
            continue
        key = (m.group("ext").strip(), m.group("sec").lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= cap:
            break
    return out


def build_verify_evidence(resolved: list[tuple[str, str, str | None]]) -> list[dict]:
    """Build ``verify_evidence`` telemetry rows from the probe outcomes.

    ``resolved`` holds ``(ext, sec, pid)`` tuples, where ``pid`` is the provision id the citation
    resolved to, or None. Each row classifies the ungrounded citation:
      * ``pid`` set: ``missed_in_corpus`` (exists but was not retrieved), with ``provision_id``
        recording the valid repoint target;
      * ``pid`` None: ``not_in_corpus`` (genuinely absent; the usual fabricated-citation failure).

    ``tag`` echoes the citation as ``"{ext} §{sec}"`` so the row can be matched against
    ``verdict.ungrounded``. The caller stores the list under ``audit_trace['verify_evidence']``;
    nothing here adds a citation to the answer.
    """
    rows: list[dict] = []
    for ext, sec, pid in resolved:
        row = {
            "tag": f"{ext} §{sec}",
            "ext": ext,
            "sec": sec,
            "classification": "missed_in_corpus" if pid is not None else "not_in_corpus",
        }
        if pid is not None:
            row["provision_id"] = pid
        rows.append(row)
    return rows
