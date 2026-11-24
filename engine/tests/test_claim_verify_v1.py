"""Track V / V1 FOUNDATION — the shared contract + atomizer + orchestrator + shadow wire.

Pins:
  • VL-D-01 flags exist beside metagate, all default-OFF (sub-flags inert unless master ON).
  • VL-D-02 atomizer INVARIANTS: verbatim span, disjoint+sorted tiling of the draft, chip-in-exactly-one-span,
    the abbreviation trap ('§ 6 odst. 1 č. 455/1991 Sb.' stays ONE claim), no split across a section boundary.
  • C3 ledger schema + build_ledger + to_audit_dict (shadow ⇒ every final_status='shadow').
  • VL-D-04 orchestrator: skip-stubs ⇒ every verdict status='skip'; every normative claim ≥1 verdict; a
    checker EXCEPTION ⇒ skip (never crash / silent pass).
  • VL-D-06 full_txt carrier: present ONLY when keep_full; text_md (700-slice) unaffected.
  • WIRE: OFF byte-identical (no claim_ledger key, no atomize); shadow ON ⇒ audit_trace['claim_ledger'] present,
    all verdicts skip, drafted_answer/sections/citations_map BYTE-IDENTICAL to OFF; enforce-without-MP-E
    degrades to shadow.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import pythia.reasoning.answer as ans
from pythia.config import get_settings
from pythia.contract import CitedProvision
from pythia.reasoning.claim_ledger import (
    FINAL_STATUS,
    VERDICT_STATUS,
    Claim,
    ClaimVerdict,
    build_ledger,
    to_audit_dict,
)
from pythia.reasoning.claim_verify import CHECK_ROUTING, verify_claims
from pythia.reasoning.claims import atomize
from pythia.reasoning.sections import split_into_sections
from pythia.reasoning.verify import verify as verify_answer

# ─────────────────────────────────────────── VL-D-01 flags ────────────────────────────────────────────────


def test_flags_exist_default_off():
    s = get_settings()
    assert s.claim_verify_enabled is False
    assert s.claim_verify_mode == "shadow"
    assert s.claim_typing_llm_enabled is False
    assert s.claim_entailment_llm_enabled is False
    assert s.claim_calc_worked_example_enabled is False


# ─────────────────────────────────────────── VL-D-02 atomizer ─────────────────────────────────────────────

_DRAFT = (
    "## Legislativní rámec\n"
    "Podnikatel musí ohlásit živnost podle § 6 odst. 1 č. 455/1991 Sb. [455/1991 § 6]. "
    "Oznámení podejte nejpozději do 15 dnů.\n"
    "## Hodnocení\n"
    "Ve vašem případě jde o volnou živnost."
)
# A retrieved set that grounds the bracketed cite so citations_map has one chip inside the first section.
_RET = [("p1", "455/1991", "§ 6")]


def _verdict(draft, ret):
    v = verify_answer(draft, ret)
    return v


def _claims_and_sections():
    v = _verdict(_DRAFT, _RET)
    secs = split_into_sections(
        _DRAFT, v.citations_map, [CitedProvision(provision_id="p1", label="§ 6", legal_hierarchy="STATUTE")]
    )
    claims = atomize(_DRAFT, secs, v.citations_map)
    return claims, secs, v


def test_atomize_verbatim_span():
    claims, _secs, _v = _claims_and_sections()
    for c in claims:
        assert _DRAFT[c.span[0] : c.span[1]] == c.text, c


def test_atomize_tiles_the_draft_disjoint_sorted():
    claims, _secs, _v = _claims_and_sections()
    spans = [c.span for c in claims]
    # sorted by start
    assert spans == sorted(spans, key=lambda s: s[0])
    # first starts at 0, last ends at len(draft), adjacent abut (no gap, no overlap)
    assert spans[0][0] == 0
    assert spans[-1][1] == len(_DRAFT)
    for a, b in zip(spans, spans[1:]):
        assert a[1] == b[0], (a, b)


def test_atomize_every_chip_in_exactly_one_claim():
    claims, _secs, v = _claims_and_sections()
    assigned = [ch for c in claims for ch in c.chips]
    # every flat chip is assigned exactly once (by identity of the (start,end,pid) tuple)
    flat_keys = sorted((ch["start"], ch["end"], ch["provision_id"]) for ch in v.citations_map)
    got_keys = sorted((ch["start"], ch["end"], ch["provision_id"]) for ch in assigned)
    assert got_keys == flat_keys
    assert len(assigned) == len(v.citations_map)


def test_atomize_abbreviation_trap_stays_one_claim():
    """'§ 6 odst. 1 č. 455/1991 Sb.' must NOT be split at the abbreviation dots — it stays inside one claim."""
    claims, _secs, _v = _claims_and_sections()
    holder = [c for c in claims if "§ 6 odst. 1 č. 455/1991 Sb." in c.text]
    assert len(holder) == 1, [c.text for c in claims]
    # and the WHOLE abbreviation phrase is contiguous inside that single claim's verbatim span
    assert "§ 6 odst. 1 č. 455/1991 Sb." in holder[0].text


def test_atomize_never_splits_across_section_boundary():
    claims, secs, _v = _claims_and_sections()
    # every claim must lie fully inside exactly one section range
    sec_ranges = [(s.range[0], s.range[1]) for s in secs]
    for c in claims:
        assert any(rs <= c.span[0] and c.span[1] <= re for rs, re in sec_ranges), c


def test_atomize_types_and_normative():
    claims, _secs, _v = _claims_and_sections()
    # the heading lines are non-normative
    headings = [c for c in claims if c.text.strip().startswith("##")]
    assert headings and all(not c.normative for c in headings)
    # the deadline sentence is typed 'deadline' by regex refinement
    deadline = [c for c in claims if "do 15 dnů" in c.text]
    assert deadline and deadline[0].type == "deadline" and deadline[0].type_source == "regex"
    # the assessment restatement is typed situation_application
    situ = [c for c in claims if "Ve vašem případě" in c.text]
    assert situ and situ[0].type == "situation_application"


def test_atomize_markerless_draft_still_tiles():
    """A draft with NO '##' markers → sections==[] → the whole draft is one 'answer' tile that still tiles."""
    draft = "Toto je odpověď bez nadpisů. Druhá věta zde."
    claims = atomize(draft, [], [])
    assert claims
    assert claims[0].span[0] == 0 and claims[-1].span[1] == len(draft)
    for c in claims:
        assert draft[c.span[0] : c.span[1]] == c.text


def test_atomize_empty_draft():
    assert atomize("", [], []) == []


# A spread of drafts covering: multi-sentence, abbreviation traps, blank lines, no markers, 7-slot-ish
# headings, amount/sanction/authority/procedure prose. The tiling + verbatim invariants must hold for ALL.
_PROPERTY_DRAFTS = [
    _DRAFT,
    "Bez nadpisů. Jen dvě věty.",
    "## Legislativní rámec\n\nPrvní.\n\nDruhá věta o § 12 odst. 3 č. 90/2012 Sb.\n## Hodnocení\nTřetí.",
    (
        "## Legislativní rámec\nPokutu až 500 000 Kč lze uložit za přestupek. "
        "ČNB je dohledový úřad. Žádost podejte do 30 dnů.\n## Hodnocení\nVe vašem případě s.r.o."
    ),
    "## Judikatura\nSoud potvrdil [5/2019 § 12].\n## Hodnocení\nPovinnost je striktní.",
    "",
    "Jedna věta bez tečky na konci",
    "## Legislativní rámec\n",  # heading-only section
]


@pytest.mark.parametrize("draft", _PROPERTY_DRAFTS)
def test_atomize_tiling_invariants_property(draft):
    """Property: for ANY draft, the claim spans are verbatim, disjoint, sorted, and TILE [0, len(draft)]."""
    # derive real sections + citations_map through the production path (no markers → sections==[])
    v = verify_answer(draft, [("p1", "5/2019", "§ 12"), ("p2", "455/1991", "§ 6")])
    secs = split_into_sections(
        draft, v.citations_map, [CitedProvision(provision_id="p1", label="§ 12", legal_hierarchy="CASE_LAW")]
    )
    claims = atomize(draft, secs, v.citations_map)
    if not draft:
        assert claims == []
        return
    spans = [c.span for c in claims]
    # verbatim
    for c in claims:
        assert draft[c.span[0] : c.span[1]] == c.text
    # sorted + tiling [0, len]
    assert spans == sorted(spans, key=lambda s: s[0])
    assert spans[0][0] == 0
    assert spans[-1][1] == len(draft)
    for a, b in zip(spans, spans[1:]):
        assert a[1] == b[0]
    # every chip assigned exactly once
    assigned = [ch for c in claims for ch in c.chips]
    assert len(assigned) == len(v.citations_map)


# ─────────────────────────────────────────── C3 ledger schema ─────────────────────────────────────────────


def test_build_ledger_shadow_all_shadow_status():
    claims = [
        Claim(
            id="c0",
            span=(0, 5),
            text="hello",
            slot="legislative",
            type="legal_rule",
            type_source="slot_default",
            normative=True,
        ),
        Claim(
            id="c1",
            span=(5, 10),
            text="world",
            slot="legislative",
            type="deadline",
            type_source="regex",
            normative=True,
        ),
    ]
    verdicts = {
        "c0": [ClaimVerdict(check="v_cit", status="skip", reason="not_implemented")],
        "c1": [ClaimVerdict(check="v_fact", status="skip", reason="not_implemented")],
    }
    ledger = build_ledger(claims, verdicts, mode="shadow")
    assert ledger["summary"]["claims_total"] == 2
    assert ledger["summary"]["mode"] == "shadow"
    assert all(r.final_status == "shadow" for r in ledger["rows"])
    assert ledger["summary"]["per_status"]["shadow"] == 2
    # every final_status key is from the frozen vocab
    assert set(ledger["summary"]["per_status"]).issubset(set(FINAL_STATUS))


def test_to_audit_dict_shape():
    claims = [
        Claim(
            id="c0",
            span=(0, 5),
            text="hello",
            slot="legislative",
            type="legal_rule",
            type_source="slot_default",
            chips=[{"provision_id": "p1", "start": 0, "end": 5}],
            normative=True,
        )
    ]
    verdicts = {"c0": [ClaimVerdict(check="v_cit", status="skip", reason="not_implemented")]}
    d = to_audit_dict(build_ledger(claims, verdicts, mode="shadow"))
    assert d["schema"] == "claim_ledger_v1"
    row = d["rows"][0]
    assert row["span"] == [0, 5]
    assert row["citations"] == ["p1"]
    assert row["checks"][0]["status"] == "skip"
    assert row["final"] == "shadow"
    # fully JSON-serializable
    import json

    json.dumps(d)


# ─────────────────────────────────────────── VL-D-04 orchestrator ─────────────────────────────────────────


def test_verify_claims_returns_valid_verdicts():
    # With the real (filled) verifiers, an empty-evidence run yields valid typed verdicts (pass/fail/
    # skip/advisory per check) — never an invalid status, never a crash. (The skip-stub era is over.)
    claims, _secs, _v = _claims_and_sections()
    out = verify_claims(claims, {"cited_texts": {}, "chips": []}, get_settings())
    for c in claims:
        for v in out[c.id]:
            assert v.status in VERDICT_STATUS


def test_every_normative_claim_gets_at_least_one_verdict():
    claims, _secs, _v = _claims_and_sections()
    out = verify_claims(claims, {}, get_settings())
    for c in claims:
        if c.normative and c.type != "gap":
            assert len(out[c.id]) >= 1, c
        # V-SAFE routes onto every normative claim
        if c.normative and c.type != "gap":
            assert any(v.check == "v_safe" for v in out[c.id]), c


def test_routing_table():
    # deadline/amount get v_cit+v_fact+v_calc (+v_safe appended by the orchestrator)
    assert CHECK_ROUTING["deadline"] == ["v_cit", "v_fact", "v_calc"]
    assert CHECK_ROUTING["amount"] == ["v_cit", "v_fact", "v_calc"]
    assert CHECK_ROUTING["legal_rule"] == ["v_cit"]
    assert CHECK_ROUTING["procedure_step"] == ["v_cit"]
    assert CHECK_ROUTING["situation_application"] == ["v_prof"]
    assert CHECK_ROUTING["gap"] == []


def test_checker_exception_becomes_skip(monkeypatch):
    """A checker that RAISES ⇒ a skip verdict (reason='checker_exception'), never a crash, never a pass."""
    import pythia.reasoning.claim_verify as cv

    def _boom(claim, evidence, settings):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(cv._REGISTRY, "v_safe", _boom)
    claim = Claim(
        id="c0",
        span=(0, 4),
        text="test",
        slot="legislative",
        type="legal_rule",
        type_source="slot_default",
        normative=True,
    )
    out = cv.verify_claims([claim], {}, get_settings())
    safe = [v for v in out["c0"] if v.check == "v_safe"]
    assert safe and safe[0].status == "skip" and safe[0].reason == "checker_exception"


def test_gap_and_nonnormative_route_to_nothing():
    gap = Claim(id="g", span=(0, 3), text="mez", slot="assessment", type="gap", type_source="regex", normative=True)
    nonnorm = Claim(
        id="n", span=(3, 6), text="## H", slot="legislative", type="gap", type_source="slot_default", normative=False
    )
    out = verify_claims([gap, nonnorm], {}, get_settings())
    assert out["g"] == []
    assert out["n"] == []


# ─────────────────────────────────────────── VL-D-06 full_txt carrier ─────────────────────────────────────

_LONG = "A" * 880 + " nejpozději do 30 dnů " + "B" * 1500


def _fake_row():
    return SimpleNamespace(
        id="p1",
        label="§ 1",
        external_id="1/2000",
        source_title="T",
        jurisdiction="CZ",
        text=_LONG,
        bindingness="binding",
        is_authoritative=True,
        temporal_status=None,
        legal_hierarchy="STATUTE",
        pit_status=None,
        stale_vector=None,
    )


class _FakePort:
    async def fetch_provisions(self, ids):
        return [_fake_row()]


@pytest.mark.asyncio
async def test_full_txt_present_only_when_keep_full(monkeypatch):
    monkeypatch.setattr(ans, "NexusPort", lambda: _FakePort())
    # keep_full False (default / loop OFF): no full_txt key, meta shape byte-identical
    off = await ans._fetch(["p1"], keep_full=False)
    assert "full_txt" not in off["p1"]
    assert off["p1"]["txt"] == _LONG[:700]
    # keep_full True: full_txt carries the FULL text; txt (served grain) still the 700-slice
    on = await ans._fetch(["p1"], keep_full=True)
    assert on["p1"]["full_txt"] == _LONG  # full raw text for the verifiers
    assert on["p1"]["txt"] == _LONG[:700]  # text_md grain UNCHANGED
    assert "do 30 dnů" in on["p1"]["full_txt"]  # deadline beyond 700 reachable
    assert "do 30 dnů" not in on["p1"]["txt"]


# ─────────────────────────────────────────── WIRE: shadow fragment builder ────────────────────────────────


def test_run_claim_verify_off_returns_empty():
    s = SimpleNamespace(claim_verify_enabled=False, claim_verify_mode="shadow")
    assert ans._run_claim_verify(_DRAFT, [], [], {}, [], s) == {}


def test_run_claim_verify_shadow_attaches_ledger():
    claims, secs, v = _claims_and_sections()
    s = SimpleNamespace(claim_verify_enabled=True, claim_verify_mode="shadow")
    frag = ans._run_claim_verify(_DRAFT, secs, v.citations_map, {}, v.citations_map, s)
    assert "claim_ledger" in frag
    led = frag["claim_ledger"]
    assert led["schema"] == "claim_ledger_v1"
    assert led["summary"]["mode"] == "shadow"
    # SHADOW ⇒ every row's final is 'shadow' (no demotion applied); each check carries a valid typed status.
    for row in led["rows"]:
        assert row["final"] == "shadow"
        for chk in row["checks"]:
            assert chk["status"] in VERDICT_STATUS


def test_run_claim_verify_is_shadow_only_builder():
    """`_run_claim_verify` is the SHADOW-ONLY fragment builder (MP-E): the OUTPUT-CHANGING enforce assembly is
    owned by `_run_claim_enforce` (answer() routes enforce there). An enforce request reaching THIS builder
    still degrades to a shadow ledger (ledger attached, output unchanged) — never a live enforce here."""
    claims, secs, v = _claims_and_sections()
    s = SimpleNamespace(claim_verify_enabled=True, claim_verify_mode="enforce")
    frag = ans._run_claim_verify(_DRAFT, secs, v.citations_map, {}, v.citations_map, s)
    assert frag["claim_ledger"]["summary"]["mode"] == "shadow"  # shadow-only builder never mutates output


# ─────────────────────────────────────── WIRE: end-to-end through answer() ────────────────────────────────


def _install_answer_harness(monkeypatch, settings):
    """Fully stub answer.py's LLM / engine / fetch so answer() runs with no LLM/DB/model. The draft is a
    section-headed statute answer; the retrieved provision grounds the bracketed cite."""
    from pythia.config import PythiaSettings  # noqa: F401 — type hint only

    monkeypatch.setattr(ans, "get_settings", lambda: settings, raising=True)

    class _LLM:
        async def available(self):
            return True

        async def json(self, prompt, schema, *, tier="cheap", stage=None):
            if "planning step" in prompt:
                return {"sub_queries": ["q"], "jurisdiction": ["CZ"], "matter_type": "x"}
            return {"unsupported": []}

        async def text(self, prompt, *, system=None, tier="generate", stage=None):
            return (
                "## Legislativní rámec\nPodle [240/2013 § 297] platí povinnost. "
                "Oznámení podejte do 15 dnů.\n## Hodnocení\nOK."
            )

    monkeypatch.setattr(ans, "get_llm", lambda tier: _LLM(), raising=True)

    class _Eng:
        async def retrieve(self, q, *, method="cte"):
            from pythia.contract import CitedProvision as _CP

            return type("R", (), {"cited_provisions": [_CP(provision_id="p1", label="§ 297", score=0.82)]})()

    monkeypatch.setattr(ans, "PythiaEngine", lambda: _Eng(), raising=True)

    _FULL = "Depozitář musí " + "X" * 900 + " oznámit do 15 dnů " + "Y" * 400

    async def _fetch_stub(ids, *, as_of=None, render_asof=False, ctx_window=False, terms=None, keep_full=False):
        m = {
            "id": "p1",
            "label": "§ 297",
            "ext": "240/2013",
            "title": "ZISIF",
            "jurisdiction": "CZ",
            "txt": _FULL[:700],
            "ctx_txt": _FULL[:700],
            "bindingness": "hard-law",
            "is_authoritative": True,
            "temporal_status": "in-force",
            "legal_hierarchy": "STATUTE",
            "pit_status": None,
            "vector_stale": None,
        }
        if keep_full:
            m["full_txt"] = _FULL
        return {"p1": m}

    monkeypatch.setattr(ans, "_fetch", _fetch_stub, raising=True)


async def test_wire_off_byte_identical(monkeypatch):
    """claim_verify_enabled=False ⇒ NO audit_trace['claim_ledger'] key, and atomize is NEVER called."""
    from pythia.config import PythiaSettings

    called = {"atomize": 0}
    _orig = ans.atomize

    def _spy(*a, **k):
        called["atomize"] += 1
        return _orig(*a, **k)

    monkeypatch.setattr(ans, "atomize", _spy, raising=True)

    s = PythiaSettings()
    assert s.claim_verify_enabled is False
    _install_answer_harness(monkeypatch, s)
    res = await ans.answer("Jaké povinnosti má depozitář podle § 297?", method="cte")
    assert "claim_ledger" not in res.audit_trace
    assert called["atomize"] == 0  # OFF ⇒ never atomized


async def test_wire_shadow_additive_and_byte_identical(monkeypatch):
    """SHADOW ON ⇒ audit_trace['claim_ledger'] present with all-skip verdicts, and drafted_answer / sections /
    citations_map are BYTE-IDENTICAL to the OFF run (the loop is a pure audit annotation)."""
    from pythia.config import PythiaSettings

    # OFF baseline
    s_off = PythiaSettings()
    _install_answer_harness(monkeypatch, s_off)
    off = await ans.answer("Jaké povinnosti má depozitář podle § 297?", method="cte")

    # SHADOW ON
    s_on = PythiaSettings(claim_verify_enabled=True, claim_verify_mode="shadow")
    _install_answer_harness(monkeypatch, s_on)
    on = await ans.answer("Jaké povinnosti má depozitář podle § 297?", method="cte")

    # additive key present, all verdicts skip, all rows shadow
    assert "claim_ledger" not in off.audit_trace
    assert "claim_ledger" in on.audit_trace
    led = on.audit_trace["claim_ledger"]
    assert led["schema"] == "claim_ledger_v1"
    assert led["summary"]["mode"] == "shadow"
    assert led["summary"]["claims_total"] >= 1
    for row in led["rows"]:
        assert row["final"] == "shadow"  # shadow never demotes/drops
        for chk in row["checks"]:
            assert chk["status"] in VERDICT_STATUS

    # BYTE-IDENTICAL output (the loop touched nothing the consumer sees) — the core V1 shadow invariant
    assert on.drafted_answer == off.drafted_answer
    assert [s.model_dump() for s in on.sections] == [s.model_dump() for s in off.sections]
    assert on.citations_map == off.citations_map
    assert [c.model_dump() for c in on.cited_provisions] == [c.model_dump() for c in off.cited_provisions]
    assert on.confidence == off.confidence
    assert on.verdict == off.verdict and on.refused == off.refused


# ─────────────────────────────────── MP-A ledger provenance (additive, C3) ─────────────────────────────────


def test_provenance_populated_from_a_verified_claim():
    """MP-A: each serialized row carries a `provenance` block sourced PURELY from the atomizer (slot + chips)
    and the verifiers' already-computed evidence — source_slot + grounding_provision_ids + per-verdict
    {verifier, reason, evidence_provision_ids}. No re-run, no new signal."""
    claim = Claim(
        id="c0",
        span=(0, 20),
        text="Lhůta do 15 dnů.",
        slot="legislative",
        type="deadline",
        type_source="regex",
        chips=[{"provision_id": "p1", "start": 0, "end": 20}],
        normative=True,
    )
    verdicts = {
        "c0": [
            # a v_cit PASS carrying its scored provision_ids (as the real verifier stashes them)
            ClaimVerdict(
                check="v_cit",
                status="pass",
                reason="cited_and_supported",
                evidence={"score": 0.8, "provision_ids": ["p1"]},
            ),
            # a v_fact PASS carrying the searched_pids key
            ClaimVerdict(
                check="v_fact",
                status="pass",
                reason="fact_tokens_grounded",
                evidence={"tokens": ["do 15 dnů"], "searched_pids": ["p1"]},
            ),
            # a v_safe PASS with no provision grounding → evidence_provision_ids == []
            ClaimVerdict(check="v_safe", status="pass", reason="lawful_lane_ok", evidence={"pattern_version": "abc"}),
        ]
    }
    d = to_audit_dict(build_ledger([claim], verdicts, mode="shadow"))
    prov = d["rows"][0]["provenance"]

    # source slot the claim came from
    assert prov["source_slot"] == "legislative"
    # the chips' grounding provision ids
    assert prov["grounding_provision_ids"] == ["p1"]

    by_check = {c["check"]: c for c in prov["checks"]}
    assert by_check["v_cit"]["verifier"] == "v_cit"
    assert by_check["v_cit"]["reason"] == "cited_and_supported"
    assert by_check["v_cit"]["evidence_provision_ids"] == ["p1"]
    assert by_check["v_fact"]["evidence_provision_ids"] == ["p1"]  # from searched_pids
    assert by_check["v_safe"]["evidence_provision_ids"] == []  # no pid grounding → honest empty

    # fully JSON-serializable (nothing engine-object leaks into the trace)
    import json

    json.dumps(d)


def test_provenance_total_on_missing_evidence():
    """PURE + total: a claim with no chips and verdicts with empty evidence ⇒ empty provenance lists / None
    span — never raises, never invents a pid."""
    claim = Claim(
        id="c1",
        span=(0, 3),
        text="ok.",
        slot="hodnoceni",
        type="gap",
        type_source="slot_default",
        chips=[],
        normative=True,
    )
    verdicts = {"c1": [ClaimVerdict(check="v_cit", status="skip", reason="citation_exempt_slot", evidence={})]}
    d = to_audit_dict(build_ledger([claim], verdicts, mode="shadow"))
    prov = d["rows"][0]["provenance"]
    assert prov["source_slot"] == "hodnoceni"
    assert prov["grounding_provision_ids"] == []
    assert prov["checks"][0]["evidence_provision_ids"] == []
    assert prov["checks"][0]["span"] is None
    # a row with no verdicts → checks == []
    claim2 = Claim(
        id="c2", span=(0, 1), text="#", slot="x", type="gap", type_source="slot_default", chips=[], normative=False
    )
    d2 = to_audit_dict(build_ledger([claim2], {}, mode="shadow"))
    assert d2["rows"][0]["provenance"]["checks"] == []


def test_provenance_span_from_entailment_quote():
    """The per-verdict `span` carries the verbatim quote a check used when it recorded one (v_cit's entailment
    lane stashes 'quote')."""
    claim = Claim(
        id="c3",
        span=(0, 5),
        text="hello",
        slot="legislative",
        type="legal_rule",
        type_source="slot_default",
        chips=[{"provision_id": "p1"}],
        normative=True,
    )
    verdicts = {
        "c3": [
            ClaimVerdict(
                check="v_cit",
                status="pass",
                reason="entailment_supported",
                evidence={"provision_ids": ["p1"], "quote": "hello world"},
            )
        ]
    }
    d = to_audit_dict(build_ledger([claim], verdicts, mode="shadow"))
    chk = d["rows"][0]["provenance"]["checks"][0]
    assert chk["span"] == "hello world"
    assert chk["evidence_provision_ids"] == ["p1"]


def test_provenance_additive_does_not_change_existing_row_keys():
    """The `provenance` field is PURELY additive: every pre-existing serialized row key is byte-identical to a
    row built WITHOUT reading provenance (the annotation adds a key, it never mutates final/checks/citations)."""
    claim = Claim(
        id="c0",
        span=(0, 5),
        text="hello",
        slot="legislative",
        type="legal_rule",
        type_source="slot_default",
        chips=[{"provision_id": "p1", "start": 0, "end": 5}],
        normative=True,
    )
    verdicts = {"c0": [ClaimVerdict(check="v_cit", status="skip", reason="not_implemented")]}
    d = to_audit_dict(build_ledger([claim], verdicts, mode="shadow"))
    row = d["rows"][0]
    # the pre-provenance contract keys are all present + unchanged
    assert row["span"] == [0, 5]
    assert row["citations"] == ["p1"]
    assert row["checks"][0]["status"] == "skip"
    assert row["final"] == "shadow"
    # and provenance is a NEW key alongside them
    assert "provenance" in row
    dropped = {k: v for k, v in row.items() if k != "provenance"}
    assert set(dropped) == {
        "claim_id",
        "span",
        "text",
        "slot",
        "type",
        "type_source",
        "normative",
        "citations",
        "checks",
        "final",
        "verification_class",
        "sq_ids",
        "intake_fact_ids",
    }


async def test_wire_shadow_provenance_present_and_output_byte_identical(monkeypatch):
    """Through answer(): SHADOW ON ⇒ every claim_ledger row gains a populated `provenance` block, while
    drafted_answer / sections / citations_map / cited_provisions stay BYTE-IDENTICAL to the OFF run."""
    from pythia.config import PythiaSettings

    s_off = PythiaSettings()
    _install_answer_harness(monkeypatch, s_off)
    off = await ans.answer("Jaké povinnosti má depozitář podle § 297?", method="cte")

    s_on = PythiaSettings(claim_verify_enabled=True, claim_verify_mode="shadow")
    _install_answer_harness(monkeypatch, s_on)
    on = await ans.answer("Jaké povinnosti má depozitář podle § 297?", method="cte")

    led = on.audit_trace["claim_ledger"]
    for row in led["rows"]:
        prov = row["provenance"]
        # source_slot mirrors the row's slot (annotation, not a new derivation)
        assert prov["source_slot"] == row["slot"]
        # one provenance-check per verdict, verifier == the check name
        assert len(prov["checks"]) == len(row["checks"])
        for pchk, vchk in zip(prov["checks"], row["checks"]):
            assert pchk["verifier"] == vchk["check"]
            assert pchk["reason"] == vchk["reason"]
    # ≥1 normative claim carried grounding provision ids from its chips
    assert any(row["provenance"]["grounding_provision_ids"] for row in led["rows"])

    # BYTE-IDENTICAL served output — provenance is annotation-only
    assert on.drafted_answer == off.drafted_answer
    assert [x.model_dump() for x in on.sections] == [x.model_dump() for x in off.sections]
    assert on.citations_map == off.citations_map
    assert [c.model_dump() for c in on.cited_provisions] == [c.model_dump() for c in off.cited_provisions]
