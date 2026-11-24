"""VL-F-10 — tests for the annual-instrument staleness checker (pure; no DB, no LLM, no clock).

Proves:
  (A) SEEDED-STALE ⇒ one alert: a family whose latest valid_to is past + no successor in the supplied rows.
  (B) FRESH / COVERED ⇒ no alert: (b1) within the reissue horizon; (b2) horizon past BUT a successor present.
  (C) FAIL-OPEN: a family with a missing/invalid valid_to raises NO alert; no as_of ⇒ no alert.
  (D) The shipped data/annual_instruments.json loads + validates and seeds the live 475/2024 travel family.
  (E) The VL-F-10 valid_to AUDIT note is recorded in the module (guards against a naive REPEALED regression).
"""

from __future__ import annotations

from datetime import date

from pythia.monitoring import annual_check as ac
from pythia.monitoring.annual_check import load_registry, run_annual_check

# A crafted registry — one family, past horizon — used for the stale/fresh/successor cases.
_STALE_REG = {
    "families": [
        {
            "family": "cestovni-nahrady",
            "domain": "labour-employment-osh",
            "latest_external_id": "475/2024",
            "valid_to": "2026-12-31",
            "cadence": "annual",
        }
    ]
}

_AFTER = date(2027, 1, 5)  # one horizon-crossing past 2026-12-31
_BEFORE = date(2026, 6, 30)  # comfortably within the horizon


# ===== (A) seeded-stale fires exactly one alert =============================================================


def test_stale_family_no_successor_alerts():
    alerts = run_annual_check(_STALE_REG, _AFTER)
    assert len(alerts) == 1
    a = alerts[0]
    assert a["family"] == "cestovni-nahrady"
    assert a["latest_ext"] == "475/2024"
    assert a["valid_to"] == "2026-12-31"
    assert "reason" in a and a["reason"]


# ===== (B) fresh / covered families do NOT alert ===========================================================


def test_within_horizon_does_not_alert():
    # Horizon (2026-12-31) is still in the FUTURE relative to as_of ⇒ fresh ⇒ no alert.
    assert run_annual_check(_STALE_REG, _BEFORE) == []


def test_successor_present_does_not_alert():
    # Horizon past, BUT a successor instrument for the same family/domain is in the supplied rows ⇒ no alert.
    rows = [{"external_id": "999/2026", "domain": "labour-employment-osh"}]
    assert run_annual_check(_STALE_REG, rows, as_of=_AFTER) == []


def test_successor_via_explicit_allowlist_does_not_alert():
    reg = {
        "families": [
            {
                **_STALE_REG["families"][0],
                "successor_external_ids": ["300/2026"],
            }
        ]
    }
    rows = [{"external_id": "300/2026", "domain": "some-other-domain"}]
    assert run_annual_check(reg, rows, as_of=_AFTER) == []


# ===== (C) fail-open ========================================================================================


def test_missing_valid_to_fail_open():
    reg = {"families": [{**_STALE_REG["families"][0], "valid_to": None}]}
    assert run_annual_check(reg, _AFTER) == []


def test_invalid_valid_to_fail_open():
    reg = {"families": [{**_STALE_REG["families"][0], "valid_to": "not-a-date"}]}
    assert run_annual_check(reg, _AFTER) == []


def test_no_asof_fail_open():
    # No date to compare against ⇒ we cannot tell what is "past" ⇒ fail-open (no alerts).
    assert run_annual_check(_STALE_REG, None) == []


def test_malformed_family_skipped():
    reg = {"families": [{"family": "x"}]}  # missing required fields
    assert run_annual_check(reg, _AFTER) == []


# ===== (D) the shipped registry loads + validates ==========================================================


def test_shipped_registry_loads_and_validates():
    reg = load_registry()
    fams = reg["families"]
    assert isinstance(fams, list) and len(fams) >= 1
    ids = {f["latest_external_id"] for f in fams}
    assert "475/2024" in ids  # the live seeded travel-allowance example
    for f in fams:
        for field in ("family", "domain", "latest_external_id", "valid_to", "cadence"):
            assert f.get(field), f"family {f.get('family')} missing {field}"


def test_shipped_registry_runs_end_to_end():
    # A smoke end-to-end: with as_of past 2026-12-31 and no corpus rows, the seeded families alert.
    reg = load_registry()
    alerts = run_annual_check(reg, date(2027, 6, 1))
    assert any(a["latest_ext"] == "475/2024" for a in alerts)


# ===== (E) the VL-F-10 valid_to AUDIT note is recorded =====================================================


def test_valid_to_audit_note_recorded():
    doc = ac.__doc__ or ""
    assert "VL-F-10 AUDIT" in doc
    # The three acts that carry valid_to=2026-12-31 and must NOT be auto-marked REPEALED.
    for act in ("455/1991", "435/2004", "582/1991"):
        assert act in doc
    assert "2026-12-31" in doc
    assert "REPEALED" in doc and "EXPECTED-REISSUE" in doc
