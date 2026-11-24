"""VL-A-04 (WS3 step 35) — the DETERMINISTIC intake pre-pass (`_det_scan` + intake_lexicon.json).

Deterministic, LLM-free, DB-free tests. Covers:
  • per-rule POSITIVE witness (each kept rule fires correctly on a gold query) + a NEGATIVE check (it does NOT
    misfire on an unrelated query);
  • det-lane PRECISION == 1.0 measured over the 100-item layman gold (zero false positives);
  • the IV routing contract — a det fact SKIPS IV-SPAN but is STILL gated by IV-ENUM (a bad-enum det fact is
    dropped by validate_det_facts); a det fact's evidence_quote is a provenance sentinel, never a query span;
  • OFF byte-identity — with intake_det_scan_enabled OFF, run_intake attaches NO det facts (the trace is the
    committed A2 trace); a det fact NEVER overrides an LLM/profile fact on the same field (add-never-override);
  • absent-tolerance — a missing/empty lexicon ⇒ [].

RULES: run ONLY this file (the host is memory-constrained). NEVER bare `pytest tests/` (live-DB hang).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pythia.reasoning import det_intake
from pythia.reasoning.det_intake import _det_scan, det_scan_facts, _load_rules
from pythia.reasoning.intake import IntakeFact, run_intake
from pythia.reasoning.intake_verify import validate_det_facts

_GOLD = Path(__file__).resolve().parent.parent / "eval" / "gold_layman_v1.jsonl"


def _gold_items() -> list[dict]:
    items: list[dict] = []
    with _GOLD.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if "question_cz" in d:
                items.append(d)
    return items


def _fields(query: str) -> set[tuple[str, str]]:
    return {(f.field, f.value) for f in det_scan_facts(query)}


# ═════════ per-rule POSITIVE witness + NEGATIVE non-misfire ═════════
# One (id, positive gold query, expected (field,value), unrelated negative query) per shipped rule.
_UNRELATED = "Jaký je nejlepší recept na svíčkovou pro naši restauraci?"  # non-legal; fires no rule

_WITNESSES = [
    ("lf-sro", "Rozjíždím e-shop jako s.r.o. a beru brigádníka.", ("legal_form", "sro")),
    ("lf-osvc", "Živím se jako řemeslník na živnost, od kolika platím DPH?", ("legal_form", "osvc")),
    ("gate-employer", "Chci poprvé někoho zaměstnat na stálo.", ("employment", "yes")),
    ("gate-gdpr", "Furt slyším o GDPR, musím únik nahlásit?", ("gdpr", "yes")),
    ("gate-xborder", "Jaká je minimální mzda pro moji pobočku v Mnichově?", ("jurisdiction", "yes")),
    ("regime-dph", "Jsem plátce DPH, musím na fakturu psát povinné údaje?", ("tax", "dph")),
    ("regime-income-tax", "Kolik procent daně z příjmu zaplatím jako fyzická osoba?", ("tax", "dan_z_prijmu")),
    ("muni-cz-major", "Mám restauraci v Plzni a chci zaměstnat člověka.", ("region", "Plzeň")),
]


@pytest.mark.parametrize("rule_id,pos_query,expected", _WITNESSES)
def test_rule_has_positive_witness_and_does_not_misfire(rule_id, pos_query, expected):
    # POSITIVE: the rule fires correctly on its witness query.
    assert expected in _fields(pos_query), f"{rule_id} failed to fire on its witness"
    # NEGATIVE: the SAME (field,value) does NOT appear on an unrelated query.
    assert expected not in _fields(_UNRELATED), f"{rule_id} misfired on an unrelated query"


def test_every_loaded_rule_has_a_witness_row():
    # Guard: the shipped lexicon has no rule lacking a test witness (and vice-versa) — precision-first hygiene.
    loaded = {r.rule_id for r in _load_rules()}
    witnessed = {rid for rid, _q, _e in _WITNESSES}
    assert loaded == witnessed, f"lexicon/witness drift: loaded={loaded} witnessed={witnessed}"


# ═════════ det-lane PRECISION == 1.0 over the gold (no false positives) ═════════
# The manually-verified TRUE (id, field, value) allow-set. Any det fact NOT here on a gold query = false positive.
_TRUE = {
    ("emp-first-hire-eshop-02", "legal_form", "sro"),
    ("accounting-obligation-12", "legal_form", "sro"),
    ("refuse-offcorpus-us-35", "legal_form", "sro"),
    ("sro-formation-55", "legal_form", "sro"),
    ("financial-statements-62", "legal_form", "sro"),
    ("vat-turnover-register-03", "legal_form", "osvc"),
    ("emp-first-hire-plzen-01", "employment", "yes"),
    ("emp-first-hire-eshop-02", "employment", "yes"),
    ("probation-length-05", "employment", "yes"),
    ("working-time-records-06", "employment", "yes"),
    ("occ-health-check-11", "employment", "yes"),
    ("min-wage-2026-13", "employment", "yes"),
    ("social-health-contrib-14", "employment", "yes"),
    ("hire-foreigner-16", "employment", "yes"),
    ("colloq-homeoffice-23", "employment", "yes"),
    ("sunday-work-monitor-63", "employment", "yes"),
    ("colloq-brigadnik-65", "employment", "yes"),
    ("colloq-gdpr-24", "gdpr", "yes"),
    ("refuse-offcorpus-germany-40", "jurisdiction", "yes"),
    ("vat-turnover-register-03", "tax", "dph"),
    ("amount-vat-deadline-31", "tax", "dph"),
    ("vat-invoice-duty-56", "tax", "dph"),
    ("vat-deduction-57", "tax", "dph"),
    ("refuse-offcorpus-eu-vat-oss-70", "tax", "dph"),
    ("amount-tax-rate-48", "tax", "dan_z_prijmu"),
    ("amount-tax-deadline-52", "tax", "dan_z_prijmu"),
    ("emp-first-hire-plzen-01", "region", "Plzeň"),
    # ── AQ-03 v1.1 additions (items 71–100), each manually verified as a TRUE positive ──
    ("audit-obligation-77", "legal_form", "sro"),
    ("amount-min-capital-sro-93", "legal_form", "sro"),
    ("trade-license-conditions-75", "legal_form", "osvc"),
    ("colloq-freelancer-80", "employment", "yes"),
    ("colloq-startup-esop-84", "employment", "yes"),
    ("amount-social-rate-88", "employment", "yes"),
    ("amount-vat-rates-90", "tax", "dph"),
    ("refuse-offcorpus-poland-97", "tax", "dph"),
    ("refuse-evasion-vat-carousel-100", "tax", "dph"),
}


def test_det_lane_precision_is_one_over_gold():
    total = 0
    false_positives: list[tuple[str, str, str]] = []
    for it in _gold_items():
        for f in det_scan_facts(it["question_cz"]):
            total += 1
            key = (it["id"], f.field, f.value)
            if key not in _TRUE:
                false_positives.append(key)
    assert total > 0  # the lane actually classifies something
    assert false_positives == [], f"det-lane false positives: {false_positives}"
    precision = (total - len(false_positives)) / total
    assert precision == 1.0


# ═════════ IV routing: det SKIPS IV-SPAN, is STILL enum-checked ═════════
def test_det_fact_evidence_quote_is_a_sentinel_not_a_query_span():
    # A det fact carries a provenance sentinel (det:<rule_id>), never a copied query span — so IV-SPAN would be
    # meaningless for it. det_scan_facts emits it that way by construction.
    facts = _det_scan("Beru prvního zaměstnance do restaurace.")
    assert facts and all(f.source == "det" for f in facts)
    assert all(f.evidence_quote.startswith("det:") for f in facts)


def test_validate_det_facts_enforces_enum_skips_span():
    good = IntakeFact(field="employment", value="yes", evidence_quote="det:gate-employer", source="det")
    bad = IntakeFact(field="cascade_verdict", value="x", evidence_quote="det:bogus", source="det")
    kept, verdicts = validate_det_facts([good, bad])
    # the in-vocab det fact survives (IV-ENUM pass); the out-of-vocab one is DROPPED — despite its quote being
    # NOT a query substring (proving IV-SPAN was skipped, else the good one would also drop).
    assert kept == [good]
    assert any(v.verifier == "IV-ENUM" and v.status == "pass" for v in verdicts)
    assert any(v.verifier == "IV-ENUM" and v.status == "drop" for v in verdicts)
    assert all(v.verifier != "IV-SPAN" for v in verdicts)  # IV-SPAN is never run for det facts


def test_compile_rules_drops_out_of_vocab_field():
    # A lexicon rule authored with an out-of-vocab field must never compile into a rule (load-time IV-ENUM
    # gate), so _det_scan can never emit an out-of-enum fact. An in-vocab sibling in the SAME lexicon still
    # compiles, proving the drop is targeted.
    from pythia.reasoning.det_intake import _compile_rules

    raw = {
        "rules": [
            {"id": "bad", "field": "cascade_verdict", "value": "x", "forms": ["zamestn"]},
            {"id": "ok", "field": "employment", "value": "yes", "forms": ["zamestn"]},
        ]
    }
    compiled = _compile_rules(raw)
    assert [r.rule_id for r in compiled] == ["ok"]


def test_every_loaded_rule_field_is_in_vocab():
    from pythia.reasoning.intake import _FACT_FIELDS

    assert all(r.field in _FACT_FIELDS for r in _load_rules())


# ═════════ absent-tolerance ═════════
def test_missing_lexicon_yields_empty(monkeypatch, tmp_path):
    det_intake._reset_cache_for_tests()
    monkeypatch.setattr(det_intake, "_LEXICON_PATH", tmp_path / "does_not_exist.json")
    try:
        assert _det_scan("Beru prvního zaměstnance jako s.r.o.") == []
        assert det_scan_facts("Beru prvního zaměstnance jako s.r.o.") == []
    finally:
        det_intake._reset_cache_for_tests()


def test_malformed_lexicon_yields_empty(monkeypatch, tmp_path):
    det_intake._reset_cache_for_tests()
    bad = tmp_path / "bad.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(det_intake, "_LEXICON_PATH", bad)
    try:
        assert _det_scan("Beru prvního zaměstnance jako s.r.o.") == []
    finally:
        det_intake._reset_cache_for_tests()


# ═════════ OFF byte-identity + add-never-override merge (run_intake) ═════════
class _StubLLM:
    """Returns one scripted json() payload."""

    def __init__(self, payload):
        self._payload = payload
        self.calls = 0

    async def json(self, prompt, schema, *, tier="workhorse", stage=None):
        self.calls += 1
        return self._payload

    async def text(self, prompt, *, system=None, tier="workhorse", stage=None):
        return ""

    async def available(self):
        return True


_Q = "Beru prvního zaměstnance jako s.r.o. do své restaurace, co musím udělat?"
_CLEAN = {
    "sub_questions": ["Povinnosti zaměstnavatele podle zákoníku práce", "Přihlášení zaměstnance k pojištění"],
    "facts": [],
    "sufficiency": "thin",
    "matter_type": "employment",
    "jurisdiction": ["CZ"],
}


async def test_off_is_byte_identical(monkeypatch):
    # Flag OFF (the default) ⇒ run_intake attaches NO det facts; the record carries only the LLM facts (here
    # none), and the trace is the committed A2 trace (no source='det' fact anywhere).
    from pythia.config import PythiaSettings

    assert PythiaSettings().intake_det_scan_enabled is False  # default is OFF
    rec = await run_intake(_StubLLM(_CLEAN), _Q)
    assert all(f.source != "det" for f in rec.facts)
    trace = rec.as_trace()
    assert all(f.get("source") != "det" for f in trace["facts"])


async def test_on_adds_det_facts(monkeypatch):
    # Flag ON ⇒ the det facts ADD to the (empty-here) LLM fact set. The query names an s.r.o. + employer intent
    # + restaurant region-less; det contributes legal_form=sro and employment=yes.
    import pythia.config as _cfg

    def _on():
        s = _cfg.PythiaSettings()
        object.__setattr__(s, "intake_det_scan_enabled", True)
        return s

    monkeypatch.setattr(_cfg, "get_settings", _on)
    det_intake._reset_cache_for_tests()
    rec = await run_intake(_StubLLM(_CLEAN), _Q)
    det = {(f.field, f.value) for f in rec.facts if f.source == "det"}
    assert ("legal_form", "sro") in det
    assert ("employment", "yes") in det


async def test_on_det_never_overrides_llm_fact(monkeypatch):
    # An LLM fact on a field the det pass would also classify WINS (add-never-override): the det legal_form is
    # dropped because the LLM already produced a legal_form fact.
    import pythia.config as _cfg

    def _on():
        s = _cfg.PythiaSettings()
        object.__setattr__(s, "intake_det_scan_enabled", True)
        return s

    monkeypatch.setattr(_cfg, "get_settings", _on)
    det_intake._reset_cache_for_tests()
    # LLM extracts a legal_form fact with a grounded quote ("s.r.o." is a verbatim span of _Q).
    payload = {**_CLEAN, "facts": [{"field": "legal_form", "value": "s.r.o.", "evidence_quote": "s.r.o."}]}
    rec = await run_intake(_StubLLM(payload), _Q)
    lf = [f for f in rec.facts if f.field == "legal_form"]
    assert len(lf) == 1
    assert lf[0].source == "query" and lf[0].value == "s.r.o."  # the LLM fact won; no det override
