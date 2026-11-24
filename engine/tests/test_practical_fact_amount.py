"""AQ-B1 — the `amount` practical_fact type: bare Kč / % / hour-week quantity / min-wage coefficient.

Pure unit tests (no DB, no LLM). Proves, on the VERIFIED-in-corpus gold figures (475/2024 §1/§2 diet rates,
285/2024 §1 min-wage coefficient, 262/2006 §79/§195 working-time/leave, 586/1992 §16 income-tax rates):
  (A) extract_practical_facts() returns an `amount` fact whose value_raw is an EXACT substring and whose
      (char_start,char_end) slices back to value_raw;
  (B) the offline insert gate accepts each `amount` row (type-agnostic verbatim-substring gate);
  (C) PRECISION GUARD — a "pokut* … Kč" fine still yields a `penalty` fact and NO overlapping duplicate
      `amount` fact (the money guard suppresses penalty/fee-covered Kč amounts); a "poplatek … Kč" fee is
      likewise not duplicated as an amount;
  (D) `amount` is a registered fact type (runtime FACT_TYPES + DB-ingest allow-list).
"""

from __future__ import annotations

from pythia.ingest import enrich_practical as ep
from pythia.ingest import practical_check as pc
from pythia.reasoning.practical_facts import FACT_TYPES, extract_practical_facts


# ===== helpers =============================================================================================


def _by_type(text: str) -> dict[str, list]:
    out: dict[str, list] = {}
    for f in extract_practical_facts(text):
        out.setdefault(f.fact_type, []).append(f)
    return out


def _assert_verbatim_and_gated(text: str, facts) -> None:
    for f in facts:
        # exact-slice identity …
        assert text[f.char_start : f.char_end] == f.value_raw
        # … AND the offline insert gate (the single chokepoint the ingest wave uses) agrees.
        cand = ep._Candidate(
            provision_id="00000000-0000-0000-0000-000000000001",
            fact_type=f.fact_type,
            value_raw=f.value_raw,
            char_start=f.char_start,
            char_end=f.char_end,
            value_norm=None,
            extraction_method="regex",
            extractor_version="aq-b1",
        )
        assert ep.gate_row(text, cand) is True


def _amount_values(text: str) -> set[str]:
    return {f.value_raw for f in _by_type(text).get("amount", [])}


# ===== (A) the verified-in-corpus gold figures each yield an exact-slice `amount` ==========================


def test_amount_czk_diet_rate_475_2024_p2():
    # 475/2024 §2 — verbatim in provision text
    text = "Sazba zahraničního stravného činí 148 Kč, trvá-li pracovní cesta 5 až 12 hodin, a poloviční částku jinak."
    facts = _by_type(text).get("amount", [])
    assert facts, "expected amount facts"
    _assert_verbatim_and_gated(text, facts)
    assert "148 Kč" in _amount_values(text)


def test_amount_czk_decimal_rate_475_2024_p1():
    # 475/2024 §1 — a decimal-comma Kč rate "5,80 Kč"
    text = "Základní náhrada za používání silničních motorových vozidel činí 5,80 Kč za kilometr jízdy."
    facts = _by_type(text).get("amount", [])
    assert facts
    _assert_verbatim_and_gated(text, facts)
    assert "5,80 Kč" in _amount_values(text)


def test_amount_min_wage_coefficient_285_2024_p1():
    # 285/2024 §1 — the bare-decimal coefficient clause
    text = "Koeficient pro výpočet minimální mzdy pro rok 2025 činí 0,422."
    facts = _by_type(text).get("amount", [])
    assert facts
    _assert_verbatim_and_gated(text, facts)
    vals = _amount_values(text)
    assert any("0,422" in v and v.lower().startswith("koeficient") for v in vals), vals


def test_amount_working_time_hours_262_2006_p79():
    # 262/2006 §79 — the "40 hodin" weekly working-time threshold
    text = "Délka stanovené týdenní pracovní doby činí 40 hodin týdně."
    facts = _by_type(text).get("amount", [])
    assert facts
    _assert_verbatim_and_gated(text, facts)
    assert "40 hodin" in _amount_values(text)


def test_amount_leave_weeks_262_2006_p195():
    # 262/2006 §195-style — a "28 týdnů" week threshold
    text = "Mateřská dovolená přísluší zaměstnankyni po dobu 28 týdnů."
    facts = _by_type(text).get("amount", [])
    assert facts
    _assert_verbatim_and_gated(text, facts)
    assert "28 týdnů" in _amount_values(text)


def test_amount_income_tax_rates_586_1992_p16():
    # 586/1992 §16 — the "15 %" and "23 %" income-tax rate clause
    text = "Daň ze základu daně činí 15 %; pro část základu přesahující limit činí sazba daně 23 %."
    facts = _by_type(text).get("amount", [])
    assert facts
    _assert_verbatim_and_gated(text, facts)
    vals = _amount_values(text)
    assert "15 %" in vals and "23 %" in vals, vals


# ===== (C) PRECISION GUARD — penalty/fee amounts are NOT duplicated as `amount` ============================


def test_penalty_led_czk_stays_penalty_no_duplicate_amount():
    # A pokut* fine string must still yield a penalty fact and NO overlapping duplicate amount fact.
    text = "Za porušení této povinnosti lze uložit pokutu do výše 500 000 Kč."
    by = _by_type(text)
    assert by.get("penalty"), "expected a penalty fact"
    # the money amount 500 000 Kč is COVERED by a penalty span → suppressed from `amount`
    penalty_spans = [(f.char_start, f.char_end) for f in by["penalty"]]
    for a in by.get("amount", []):
        covered = any(cs <= a.char_start and a.char_end <= ce for cs, ce in penalty_spans)
        assert not (a.value_raw.strip().endswith(("Kč", "kč", "Kc", "kc")) and covered), (
            f"penalty-covered money amount {a.value_raw!r} should not be duplicated as `amount`"
        )


def test_fee_led_czk_not_duplicated_as_amount():
    # A správní-poplatek fee amount is likewise not duplicated as an amount fact.
    text = "Za podání žádosti se platí správní poplatek ve výši 1 000 Kč."
    by = _by_type(text)
    assert by.get("fee"), "expected a fee fact"
    fee_spans = [(f.char_start, f.char_end) for f in by["fee"]]
    for a in by.get("amount", []):
        covered = any(cs <= a.char_start and a.char_end <= ce for cs, ce in fee_spans)
        assert not (a.value_raw.strip().endswith(("Kč", "kč")) and covered), (
            f"fee-covered money amount {a.value_raw!r} should not be duplicated as `amount`"
        )


def test_non_penalty_pct_amount_kept_even_near_fine_context():
    # A percentage rate carries no Kč token, so it is NEVER a penalty/fee collision and is always kept.
    text = "Sazba daně činí 15 %."
    assert "15 %" in _amount_values(text)


# ===== (D) `amount` is a registered fact type =============================================================


def test_amount_is_registered_fact_type():
    assert "amount" in FACT_TYPES
    assert "amount" in pc._VALID_FACT_TYPES
