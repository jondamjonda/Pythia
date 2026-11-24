"""VL-B-07 (WS2 step 17) — the EvidenceBundle full-text never crosses the /v1/answer wire.

Before this, PythiaAnswer.evidence serialized every provision's FULL text (≤20k × N) onto the response — a
payload leak and the opposite of the C5 contract (the text is an INTERNAL verifier carrier; consumers index
char spans into it, they don't need it on the wire). Pins: JSON serialization redacts EvidenceProvision.text
to ''; internal attribute access + python-mode model_dump keep it; the build-time carrier is capped.
"""

from __future__ import annotations

import json

from pythia.contract import EvidenceBundle, EvidenceProvision, PythiaAnswer

_BIG = "PARAGRAF " * 3000  # ~24k chars — a large provision text carrier


def test_json_serialization_redacts_provision_text():
    prov = EvidenceProvision(provision_id="p1", ext="262/2006", label="§ 93", text=_BIG)
    blob = prov.model_dump_json()
    data = json.loads(blob)
    assert data["text"] == ""  # the full text never serializes
    # ids / labels / locators DO serialize (spans stay resolvable)
    assert data["provision_id"] == "p1" and data["ext"] == "262/2006" and data["label"] == "§ 93"


def test_internal_attribute_access_keeps_full_text():
    prov = EvidenceProvision(provision_id="p1", text=_BIG)
    assert prov.text == _BIG  # verifier lane reads the full carrier unaffected
    # python-mode model_dump also keeps it (only json mode redacts)
    assert prov.model_dump()["text"] == _BIG


def test_bundle_text_accessors_unaffected():
    bundle = EvidenceBundle(provisions=[EvidenceProvision(provision_id="p1", text=_BIG)])
    assert bundle.text_for("p1") == _BIG
    assert bundle.cited_texts()["p1"] == _BIG


def test_pythia_answer_wire_has_no_provision_text():
    ans = PythiaAnswer(
        drafted_answer="See § 93 [1].",
        evidence=EvidenceBundle(
            provisions=[
                EvidenceProvision(provision_id="p1", label="§ 93", text=_BIG),
                EvidenceProvision(provision_id="p2", label="§ 94", text=_BIG, promoted=True),
            ]
        ),
    )
    wire = ans.model_dump_json()  # exactly the FastAPI response serialization path
    # no >1k contiguous run of the carrier text anywhere in the serialized answer
    assert _BIG[:1000] not in wire
    assert "PARAGRAF PARAGRAF PARAGRAF" not in wire
    # but the evidence structure (ids/labels) survives for the citation map
    reloaded = json.loads(wire)
    labels = [p["label"] for p in reloaded["evidence"]["provisions"]]
    assert labels == ["§ 93", "§ 94"]
