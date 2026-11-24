"""VL-B-07 (WS2 step 17) — app defensive second net: evidence full-text never propagates through the app.

The engine already redacts EvidenceProvision.text on its wire; this pins the app's own strip so a stale or
unredacted sidecar can't leak provision text onward. Pure function test (no DB / no sidecar).
"""

from __future__ import annotations

from regsuper.api.answer import _strip_evidence_fulltext

_BIG = "PARAGRAF " * 3000


def test_strip_blanks_provision_text_keys():
    ans = {
        "drafted_answer": "See § 93 [1].",
        "evidence": {
            "provisions": [
                {"provision_id": "p1", "label": "§ 93", "text": _BIG, "full_txt": _BIG},
                {"provision_id": "p2", "label": "§ 94", "text": _BIG},
            ],
            "resolved": [{"provision_id": "p1", "resolved": True}],
        },
    }
    _strip_evidence_fulltext(ans)
    for p in ans["evidence"]["provisions"]:
        assert p["text"] == "" and p.get("full_txt", "") == ""
        assert p["provision_id"] and p["label"]  # ids/labels untouched
    assert ans["evidence"]["resolved"][0]["resolved"] is True


def test_strip_tolerates_missing_or_malformed_evidence():
    for ans in (
        {},
        {"evidence": None},
        {"evidence": {}},
        {"evidence": {"provisions": None}},
        {"evidence": {"provisions": ["not-a-dict"]}},
    ):
        _strip_evidence_fulltext(ans)  # must never raise
