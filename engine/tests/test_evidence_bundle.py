"""Answer-Quality Part II / T3 — the EVIDENCE BUNDLE + CITATION RESOLVER (contracts C5 + C8) proof.

Pure unit tests (no live LLM, no live DB — the neighbor-promotion path uses a fake NexusPort). Proves:

  (a) evidence_bundle_enabled defaults False (byte-identical-OFF guard).
  (b) C5 CARRIER — the bundle is built ONCE and carries the FULL (untruncated) text; every consumer indexes
      the SAME text (value_raw == bundle.text_for(pid)[start:end]).
  (c) RESOLVER — a cited § resolves to the right provision id + draft span; an unresolved cite is REPORTED
      (resolved=False) not dropped.
  (d) NEIGHBOR PROMOTION — a citation of a § not in the selection promotes the neighbor (via the port) and
      then resolves.
  (e) CLAUSE-SNAP (C8) — a parent-§ cite narrows to the exact sub-§/clause row when the bundle carries one.
  (f) TOTALITY — empty selection / malformed draft yield an empty bundle, never a raise.
"""

from __future__ import annotations

import asyncio

import pytest

from pythia.config import PythiaSettings
from pythia.contract import EvidenceProvision, ResolvedCitation
from pythia.ports import ProvisionRow
from pythia.reasoning.evidence_bundle import (
    build_bundle,
    parse_citations,
    resolve_against,
)


# ----- (a) flag defaults OFF -------------------------------------------------------------------------------


def test_flag_defaults_false():
    assert PythiaSettings().evidence_bundle_enabled is False


# ----- (b) C5 carrier — one build, full text, span index invariant ----------------------------------------

_FULL = (
    "§ 297 Poplatník je povinen podat oznámení správci daně nejpozději do 30 dnů ode dne zahájení "
    "činnosti. " + ("x" * 900) + " Za porušení lze uložit pokutu do výše 500 000 Kč na konci textu."
)


def test_carrier_carries_full_text_not_truncated():
    meta = {"pA": {"ext": "240/2013", "label": "§ 297", "txt": _FULL[:700], "full_txt": _FULL}}
    bundle = asyncio.run(build_bundle("Viz [240/2013 § 297].", meta, ["pA"]))
    carrier = bundle.text_for("pA")
    assert carrier == _FULL  # FULL text, not the 700-slice
    assert len(carrier) > 700
    # the deadline at ~char 60 AND the penalty near char 1000+ are BOTH inside the carrier (a consumer that
    # indexed a span past 700 gets a real slice, the point of C5).
    idx = carrier.index("500 000 Kč")
    assert carrier[idx : idx + 10] == "500 000 Kč"  # value_raw == text[start:end]


def test_carrier_falls_back_to_700_slice_when_no_full():
    meta = {"pA": {"ext": "240/2013", "label": "§ 297", "txt": "short slice"}}
    bundle = asyncio.run(build_bundle("Viz [240/2013 § 297].", meta, ["pA"]))
    assert bundle.text_for("pA") == "short slice"


# ----- (c) resolver — resolve + report-unresolved ---------------------------------------------------------


def test_cited_section_resolves_to_provision_and_span():
    draft = "Podle [240/2013 § 297] platí povinnost."
    cites = parse_citations(draft)
    provs = [EvidenceProvision(provision_id="pA", ext="240/2013", label="§ 297", text="t")]
    res = resolve_against(cites, provs)
    assert len(res) == 1
    c = res[0]
    assert c.resolved and c.provision_id == "pA"
    assert draft[c.draft_start : c.draft_end] == "[240/2013 § 297]"


def test_unresolved_citation_is_reported_not_dropped():
    draft = "Viz [999/9999 § 5] a [240/2013 § 297]."
    cites = parse_citations(draft)
    provs = [EvidenceProvision(provision_id="pA", ext="240/2013", label="§ 297", text="t")]
    res = resolve_against(cites, provs)
    # BOTH citations are present in the result — the phantom one is reported unresolved, NEVER dropped.
    assert len(res) == 2
    unresolved = [c for c in res if not c.resolved]
    assert len(unresolved) == 1
    assert unresolved[0].act == "999/9999" and unresolved[0].section == "5"


def test_bare_section_ambiguous_across_acts_is_not_mislinked():
    # two retrieved acts share § 5 → a bare "§ 5" is genuinely ambiguous → resolve-or-drop (unresolved).
    draft = "Podle § 5 platí."
    cites = parse_citations(draft)
    provs = [
        EvidenceProvision(provision_id="pA", ext="1/2020", label="§ 5", text="t"),
        EvidenceProvision(provision_id="pB", ext="2/2020", label="§ 5", text="t"),
    ]
    res = resolve_against(cites, provs)
    assert res and not res[0].resolved


# ----- (d) neighbor promotion -----------------------------------------------------------------------------


class _FakePort:
    """A NexusPort stand-in: act_tree exposes an act's §-nodes; fetch_provisions returns ProvisionRow rows
    for the promoted ids. No DB — proves the promotion + re-resolve path purely."""

    def __init__(self, nodes: list[dict], rows: dict[str, ProvisionRow]):
        self._nodes = nodes
        self._rows = rows

    async def act_tree(self, ext: str):
        return {"nodes": self._nodes}

    async def fetch_provisions(self, ids: list[str]):
        return [self._rows[i] for i in ids if i in self._rows]


def test_neighbor_reference_promotes_and_resolves():
    # selection carries § 297; the draft ALSO cites § 298 (act-qualified) which is not selected. act_tree
    # exposes § 298's node; promotion pulls it in and the cite then resolves.
    draft = "Viz [240/2013 § 297] a [240/2013 § 298]."
    meta = {"pA": {"ext": "240/2013", "label": "§ 297", "txt": "s", "full_txt": "full 297"}}
    nodes = [{"id": "pA", "label": "§ 297"}, {"id": "pB", "label": "§ 298"}]
    rows = {
        "pB": ProvisionRow(
            id="pB",
            label="§ 298",
            text="full 298",
            jurisdiction="CZ",
            legal_hierarchy="ZAKON",
            valid_from=None,
            valid_to=None,
            source_title="Act 240/2013",
            external_id="240/2013",
            bindingness="ZAKON",
            is_authoritative=True,
            temporal_status="in-force",
            analysis_roles=None,
            parent_id=None,
        )
    }
    port = _FakePort(nodes, rows)
    bundle = asyncio.run(build_bundle(draft, meta, ["pA"], port=port))
    ids = {p.provision_id for p in bundle.provisions}
    assert "pB" in ids  # the neighbor was promoted into the carrier
    pB = bundle.by_id()["pB"]
    assert pB.promoted is True and pB.text == "full 298"
    by_sec = {c.section: c for c in bundle.resolved}
    assert by_sec["298"].resolved and by_sec["298"].provision_id == "pB"


# ----- (e) clause-snap (C8) -------------------------------------------------------------------------------


def test_clause_snap_narrows_parent_to_subclause():
    draft = "Viz [240/2013 § 297]."
    cites = parse_citations(draft)
    provs = [
        EvidenceProvision(provision_id="pParent", ext="240/2013", label="§ 297", text="parent"),
        EvidenceProvision(
            provision_id="pChild",
            ext="240/2013",
            label="§ 297 odst. 2",
            text="sub",
            promoted=True,
            source_pid="pParent",
        ),
    ]
    res = resolve_against(cites, provs)
    assert res[0].resolved
    assert res[0].provision_id == "pChild"  # snapped to the sub-§ row
    assert res[0].snapped_from == "pParent"


def test_clause_snap_ambiguous_child_leaves_parent():
    # two sub-§ children → ambiguous → do NOT narrow (never mis-snap).
    draft = "Viz [240/2013 § 297]."
    cites = parse_citations(draft)
    provs = [
        EvidenceProvision(provision_id="pParent", ext="240/2013", label="§ 297", text="parent"),
        EvidenceProvision(provision_id="pC1", ext="240/2013", label="§ 297 odst. 1", source_pid="pParent"),
        EvidenceProvision(provision_id="pC2", ext="240/2013", label="§ 297 odst. 2", source_pid="pParent"),
    ]
    res = resolve_against(cites, provs)
    assert res[0].resolved and res[0].provision_id == "pParent" and res[0].snapped_from is None


# ----- (f) totality ---------------------------------------------------------------------------------------


class _EmptyPort:
    """A NexusPort stand-in whose act_tree/fetch return nothing — so promotion finds no neighbor. Keeps the
    totality tests hermetic (no live DB) while still exercising the promotion code path."""

    async def act_tree(self, ext):
        return {"nodes": []}

    async def fetch_provisions(self, ids):
        return []


def test_empty_selection_yields_empty_bundle():
    # a real act-qualified cite over an EMPTY selection: with no neighbor found the carrier stays empty and
    # the cite is REPORTED unresolved (never dropped). (Fake port → hermetic; the live-promotion path is
    # proven separately in test_neighbor_reference_promotes_and_resolves.)
    bundle = asyncio.run(build_bundle("Viz [240/2013 § 297].", {}, [], port=_EmptyPort()))
    assert bundle.provisions == []
    assert bundle.resolved and not bundle.resolved[0].resolved  # cite reported unresolved, never dropped


def test_malformed_draft_never_raises():
    for bad in (None, "", "no citations here", "[[[", "§"):
        bundle = asyncio.run(build_bundle(bad, {}, [], port=_EmptyPort()))
        assert bundle.provisions == []


def test_resolved_citation_shape_is_frozen():
    c = ResolvedCitation(section="297", resolved=True, provision_id="p")
    with pytest.raises(Exception):
        c.resolved = False  # frozen model


# ----- answer()-level wiring: OFF byte-identical, ON builds the shared carrier -----------------------------

import pythia.reasoning.answer as ans  # noqa: E402


def _answer_harness(monkeypatch, settings, *, draft: str, full_text: str):
    """Stub answer.py's LLM / engine / fetch (no real LLM / DB). The fetch stub serves txt=full_text[:700]
    but full_txt=full_text — so a bundle that carries the 700-slice vs the FULL text is distinguishable."""
    monkeypatch.setattr(ans, "get_settings", lambda: settings, raising=True)

    class _LLM:
        async def available(self):
            return True

        async def json(self, prompt, schema, *, tier="cheap", stage=None):
            if "planning step" in prompt:
                return {"sub_queries": ["q"], "jurisdiction": ["CZ"], "matter_type": "x"}
            return {"unsupported": []}

        async def text(self, prompt, *, system=None, tier="generate", stage=None):
            return draft

    monkeypatch.setattr(ans, "get_llm", lambda tier: _LLM(), raising=True)

    class _Eng:
        async def retrieve(self, q, *, method="cte"):
            from pythia.contract import CitedProvision as _CP

            return type("R", (), {"cited_provisions": [_CP(provision_id="p1", label="§ 297", score=0.82)]})()

    monkeypatch.setattr(ans, "PythiaEngine", lambda: _Eng(), raising=True)

    async def _fetch_stub(ids, *, as_of=None, render_asof=False, ctx_window=False, terms=None, keep_full=False):
        m = {
            "id": "p1",
            "label": "§ 297",
            "ext": "240/2013",
            "title": "ZISIF",
            "jurisdiction": "CZ",
            "txt": full_text[:700],
            "ctx_txt": full_text[:700],
            "bindingness": "hard-law",
            "is_authoritative": True,
            "temporal_status": "in-force",
            "legal_hierarchy": "STATUTE",
            "pit_status": None,
            "vector_stale": None,
        }
        if keep_full:
            m["full_txt"] = full_text
        return {"p1": m}

    monkeypatch.setattr(ans, "_fetch", _fetch_stub, raising=True)

    # WS3 step 21: the B3 evidence wire calls fact_lookup (a DB read) on the claim-verify path. These are
    # DB-free unit tests, so stub the builder to an empty extra dict (the bundle/V-CIT behaviour under test is
    # independent of the fact/profile/temporal evidence).
    async def _extra_stub(cited_ids, meta, intake_rec, project_context):
        return {}

    monkeypatch.setattr(ans, "_claim_evidence_extra", _extra_stub, raising=True)


_Q = "Jaké povinnosti má depozitář podle § 297?"
_FULL = (
    "## text  Depozitář musí oznámit údaje podle § 297 do 15 dnů. "
    + "X" * 900
    + " Poslední věta s pokutou do výše 500 000 Kč."
)
_DRAFT = "## Legislativní rámec\nDepozitář musí oznámit údaje podle [240/2013 § 297].\n## Hodnocení\nOK."


def test_answer_off_carries_no_bundle(monkeypatch):
    _answer_harness(monkeypatch, PythiaSettings(), draft=_DRAFT, full_text=_FULL)
    res = asyncio.run(ans.answer(_Q, method="cte"))
    assert res.evidence is None  # OFF ⇒ no bundle → byte-identical for a consumer ignoring it


def test_answer_on_builds_full_text_bundle_and_resolves(monkeypatch):
    _answer_harness(monkeypatch, PythiaSettings(evidence_bundle_enabled=True), draft=_DRAFT, full_text=_FULL)
    res = asyncio.run(ans.answer(_Q, method="cte"))
    assert res.evidence is not None
    # the carrier holds the FULL text (not the 700-slice the served text_md keeps).
    assert res.evidence.text_for("p1") == _FULL
    assert len(res.evidence.text_for("p1")) > 700
    # the served chip payload text_md is STILL the 700-slice (the carrier never leaks into the served grain).
    assert res.cited_provisions and res.cited_provisions[0].text_md == _FULL[:700]
    # the § 297 citation resolved to the provision.
    r297 = [c for c in res.evidence.resolved if c.section == "297"]
    assert r297 and r297[0].resolved and r297[0].provision_id == "p1"


# ----- (g) W3 — the CONSUMER accessors: snap_map + text_for_cited (contract C5/C8) ------------------------

from pythia.contract import EvidenceBundle  # noqa: E402


def test_text_for_cited_non_snapped_equals_text_for():
    # no clause-snap → text_for_cited(pid) is exactly the pid's own full carrier text.
    b = EvidenceBundle(
        provisions=[EvidenceProvision(provision_id="pA", ext="240/2013", label="§ 297", text=_FULL)],
        resolved=[ResolvedCitation(act="240/2013", section="297", resolved=True, provision_id="pA")],
    )
    assert b.snap_map() == {}
    assert b.text_for_cited("pA") == _FULL == b.text_for("pA")
    # the span invariant holds against the returned text.
    carrier = b.text_for_cited("pA")
    idx = carrier.index("500 000 Kč")
    assert carrier[idx : idx + 10] == "500 000 Kč"


def test_text_for_cited_applies_clause_snap_to_narrower_subclause():
    # the citation landed on parent § 297 but snapped to the sub-§ child (pChild); a consumer keyed on the
    # PARENT pid must read the NARROWER child carrier text — the intended C8 precision gain.
    parent_txt = "§ 297 obecný text " + ("y" * 900)
    child_txt = "§ 297 odst. 2: oznámení do 15 dnů správci daně."
    b = EvidenceBundle(
        provisions=[
            EvidenceProvision(provision_id="pParent", ext="240/2013", label="§ 297", text=parent_txt),
            EvidenceProvision(
                provision_id="pChild",
                ext="240/2013",
                label="§ 297 odst. 2",
                text=child_txt,
                promoted=True,
                source_pid="pParent",
            ),
        ],
        resolved=[
            ResolvedCitation(
                act="240/2013", section="297", resolved=True, provision_id="pChild", snapped_from="pParent"
            )
        ],
    )
    assert b.snap_map() == {"pParent": "pChild"}
    assert b.text_for_cited("pParent") == child_txt  # snapped to the narrower sub-§ text
    # the verbatim-span invariant holds against the SNAPPED (narrower) carrier text.
    snapped = b.text_for_cited("pParent")
    idx = snapped.index("15 dnů")
    assert snapped[idx : idx + 6] == "15 dnů"
    # the child pid itself still resolves to its own text (identity, no double-snap).
    assert b.text_for_cited("pChild") == child_txt


def test_text_for_cited_missing_pid_is_safe():
    b = EvidenceBundle()
    assert b.text_for_cited("nope") == ""  # absent pid → '' (never raises)
    assert b.snap_map() == {}


def test_snap_map_ignores_unresolved_and_unsnapped():
    b = EvidenceBundle(
        resolved=[
            ResolvedCitation(section="5", resolved=False),  # unresolved
            ResolvedCitation(section="6", resolved=True, provision_id="pX"),  # resolved, not snapped
            ResolvedCitation(section="7", resolved=True, provision_id="pC", snapped_from="pP"),  # the only snap
        ]
    )
    assert b.snap_map() == {"pP": "pC"}


# ----- (h) W3 — the answer()-level consumer wiring reads text_for_cited (V-CIT + practical) ---------------


def test_answer_vcit_reads_bundle_snapped_text(monkeypatch):
    """ON path: V-CIT (_cv_texts) sources its per-provision text from bundle.text_for_cited — with a
    clause-snapped parent-§ substituted by its NARROWER sub-§ row. We spy on the cited_texts the claim-verify
    consumer receives to prove the bundle carrier (not the meta full_txt/txt slice) is read, and that the snap
    substitutes the narrower child text."""
    settings = PythiaSettings(evidence_bundle_enabled=True, claim_verify_enabled=True)
    _answer_harness(monkeypatch, settings, draft=_DRAFT, full_text=_FULL)

    # Force a clause-snap: after build_bundle, rewrite the bundle so p1's citation snapped to a child p1c whose
    # carrier is a distinct NARROWER text — proving the consumer reads the SNAPPED text, not p1's full carrier.
    from pythia.reasoning import evidence_bundle as _eb

    _child_txt = "§ 297 odst. 2: oznámení do 15 dnů. pokutu do výše 500 000 Kč."
    _real_build = _eb.build_bundle

    async def _snapping_build(draft, meta, ordered, *, port=None):
        b = await _real_build(draft, meta, ordered, port=port)
        provs = list(b.provisions) + [
            EvidenceProvision(
                provision_id="p1c",
                ext="240/2013",
                label="§ 297 odst. 2",
                text=_child_txt,
                promoted=True,
                source_pid="p1",
            )
        ]
        resolved = [
            c.model_copy(update={"provision_id": "p1c", "snapped_from": "p1"})
            if c.section == "297" and c.resolved
            else c
            for c in b.resolved
        ]
        return EvidenceBundle(provisions=provs, resolved=resolved)

    # build_bundle is imported inside answer() via `from ...evidence_bundle import build_bundle`, resolving the
    # module symbol at call time — patch it on the source module.
    monkeypatch.setattr(_eb, "build_bundle", _snapping_build, raising=True)

    captured: dict = {}
    _real_run_cv = ans._run_claim_verify

    def _spy_run_cv(draft, sections, citations_map, cited_texts, chips, s, *a, **k):
        captured["cv"] = dict(cited_texts or {})
        return _real_run_cv(draft, sections, citations_map, cited_texts, chips, s, *a, **k)

    monkeypatch.setattr(ans, "_run_claim_verify", _spy_run_cv, raising=True)

    res = asyncio.run(ans.answer(_Q, method="cte"))
    assert res.evidence is not None and res.evidence.snap_map() == {"p1": "p1c"}
    # the V-CIT consumer keyed on the cited parent pid p1 received the NARROWER snapped child text.
    assert captured.get("cv", {}).get("p1") == _child_txt
    # the span invariant holds against the text the consumer actually read.
    t = captured["cv"]["p1"]
    assert t[t.index("15 dnů") : t.index("15 dnů") + 6] == "15 dnů"


def test_answer_vcit_reads_bundle_full_carrier_when_no_snap(monkeypatch):
    """ON path, no clause-snap: V-CIT reads the FULL bundle carrier (text_for_cited == text_for == the full
    text), NOT the 700-slice — the C5 one-carrier gain, distinguishable from the served text_md 700-slice."""
    settings = PythiaSettings(evidence_bundle_enabled=True, claim_verify_enabled=True)
    _answer_harness(monkeypatch, settings, draft=_DRAFT, full_text=_FULL)

    captured: dict = {}
    _real_run_cv = ans._run_claim_verify

    def _spy_run_cv(draft, sections, citations_map, cited_texts, chips, s, *a, **k):
        captured["cv"] = dict(cited_texts or {})
        return _real_run_cv(draft, sections, citations_map, cited_texts, chips, s, *a, **k)

    monkeypatch.setattr(ans, "_run_claim_verify", _spy_run_cv, raising=True)
    res = asyncio.run(ans.answer(_Q, method="cte"))
    assert res.evidence is not None and res.evidence.snap_map() == {}
    assert captured.get("cv", {}).get("p1") == _FULL  # the FULL carrier, not the 700-slice
    assert len(captured["cv"]["p1"]) > 700
