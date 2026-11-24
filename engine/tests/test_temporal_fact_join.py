"""VL-F-06 (WS6 step 59) — temporal ground-truth fact-row join (SHADOW, FAIL-OPEN).

`fact_lookup.lookup(pid, with_temporal=True)` attaches each provision's temporal status + a FAIL-OPEN
`temporal_superseded` flag to every returned FactRow:

  • IN_FORCE / 'in-force' / '' / None (unknown) ⇒ NOT flagged (temporal_superseded is False),
  • SUPERSEDED / NOT_YET_IN_FORCE / repealed / annulled ⇒ flagged (temporal_superseded is True).

The fail-open direction: a NULL / unknown status is KEPT UNFLAGGED (never a false temporal fail). With
`with_temporal=False` (the default) the FactRow is BYTE-IDENTICAL — no temporal key in as_dict().

V-FACT consumes the flag via the WS3 step-21 evidence wire in SHADOW: a superseded fact row is recorded in
the verdict evidence, but the pass/fail STATUS is unchanged (enforce-consumption stays flip-gated).

No live DB — get_pool and NexusPort are stubbed.
"""

from __future__ import annotations

import pytest

from pythia.reasoning import fact_lookup
from pythia.reasoning.fact_lookup import _CURRENT_ONLY_PRED, FactRow

# pyproject asyncio_mode="auto" ⇒ coroutine tests run without an explicit mark; sync tests run as sync.


# ── DB / Nexus stubs ─────────────────────────────────────────────────────────────────────────────────────
class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, sql, provision_id, fact_type):
        return self._rows


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, rows):
        self._conn = _FakeConn(rows)

    def acquire(self):
        return _FakeAcquire(self._conn)


class _Prov:
    def __init__(self, pit_status, temporal_status, text="Depozitář musí oznámit do 30 dnů."):
        self.pit_status = pit_status
        self.temporal_status = temporal_status
        self.text = text


def _stub(monkeypatch, *, rows, pit_status, temporal_status, prov_text="Depozitář musí do 30 dnů."):
    """Stub the table rows (get_pool) + the provision's temporal facets/text (NexusPort)."""

    async def _get_pool():
        return _FakePool(rows)

    monkeypatch.setattr(fact_lookup, "get_pool", _get_pool, raising=True)

    class _Port:
        async def fetch_provisions(self, ids):
            return [_Prov(pit_status, temporal_status, prov_text)]

    monkeypatch.setattr(fact_lookup, "NexusPort", lambda: _Port(), raising=True)


def _table_row(value_raw="do 30 dnů", fact_type="deadline"):
    return {
        "fact_type": fact_type,
        "value_raw": value_raw,
        "char_start": 0,
        "char_end": len(value_raw),
        "value_norm": None,
        "extraction_method": "regex",
    }


# ── the IN_FORCE / SUPERSEDED / NULL matrix ──────────────────────────────────────────────────────────────
async def test_superseded_row_is_flagged(monkeypatch):
    _stub(monkeypatch, rows=[_table_row()], pit_status="SUPERSEDED", temporal_status=None)
    out = await fact_lookup.lookup("P1", with_temporal=True)
    assert len(out) == 1
    assert out[0].temporal_superseded is True
    assert out[0].pit_status == "SUPERSEDED"
    assert out[0].as_dict()["temporal_superseded"] is True


async def test_not_yet_in_force_row_is_flagged(monkeypatch):
    _stub(monkeypatch, rows=[_table_row()], pit_status="NOT_YET_IN_FORCE", temporal_status=None)
    out = await fact_lookup.lookup("P1", with_temporal=True)
    assert out[0].temporal_superseded is True


async def test_doc_level_repealed_is_flagged(monkeypatch):
    # doc-level temporal_status='repealed' with a NULL pit_status ⇒ still flagged (fail is the OR of both).
    _stub(monkeypatch, rows=[_table_row()], pit_status=None, temporal_status="repealed")
    out = await fact_lookup.lookup("P1", with_temporal=True)
    assert out[0].temporal_superseded is True
    assert out[0].temporal_status == "repealed"


async def test_in_force_row_is_not_flagged(monkeypatch):
    _stub(monkeypatch, rows=[_table_row()], pit_status="IN_FORCE", temporal_status="in-force")
    out = await fact_lookup.lookup("P1", with_temporal=True)
    assert out[0].temporal_superseded is False  # current law ⇒ not a temporal fail


async def test_null_status_is_not_flagged_fail_open(monkeypatch):
    # THE fail-open case: an unknown/NULL status is KEPT UNFLAGGED (never a false temporal fail).
    _stub(monkeypatch, rows=[_table_row()], pit_status=None, temporal_status=None)
    out = await fact_lookup.lookup("P1", with_temporal=True)
    assert out[0].temporal_superseded is False
    assert out[0].pit_status is None and out[0].temporal_status is None


async def test_unknown_string_status_is_not_flagged_fail_open(monkeypatch):
    # An unrecognised status string is treated as in-force (fail-open), not a temporal fail.
    _stub(monkeypatch, rows=[_table_row()], pit_status="WHATEVER", temporal_status="mystery")
    out = await fact_lookup.lookup("P1", with_temporal=True)
    assert out[0].temporal_superseded is False


# ── byte-identical when OFF (default) ────────────────────────────────────────────────────────────────────
async def test_with_temporal_false_is_byte_identical(monkeypatch):
    _stub(monkeypatch, rows=[_table_row()], pit_status="SUPERSEDED", temporal_status="repealed")
    off = await fact_lookup.lookup("P1")  # default with_temporal=False
    assert len(off) == 1
    d = off[0].as_dict()
    # NO temporal keys in the emitted dict when the join was not requested.
    assert "temporal_superseded" not in d
    assert "pit_status" not in d
    assert "temporal_status" not in d
    # The FactRow's temporal attrs stay at their inert defaults.
    assert off[0].temporal_superseded is None and off[0]._with_temporal is False
    # And the dict is EXACTLY the pre-VL-F-06 shape.
    assert set(d) == {"fact_type", "value_raw", "char_start", "char_end", "value_norm", "extraction_method", "source"}


async def test_off_and_on_share_the_same_core_fields(monkeypatch):
    _stub(monkeypatch, rows=[_table_row()], pit_status="SUPERSEDED", temporal_status=None)
    off = (await fact_lookup.lookup("P1"))[0]
    on = (await fact_lookup.lookup("P1", with_temporal=True))[0]
    # The verbatim core is untouched by the join — only additive temporal keys differ.
    for f in ("fact_type", "value_raw", "char_start", "char_end", "value_norm", "extraction_method", "source"):
        assert getattr(off, f) == getattr(on, f)


# ── the regex-fallback path also carries the join ────────────────────────────────────────────────────────
async def test_regex_fallback_path_carries_temporal(monkeypatch):
    # No table rows ⇒ the live-regex fallback runs over the provision text; the join still stamps the flag
    # (reusing the ProvisionRow the fallback already fetched — no second DB read).
    _stub(
        monkeypatch,
        rows=[],
        pit_status="SUPERSEDED",
        temporal_status=None,
        prov_text="Oznámení se podá do 30 dnů ode dne rozhodnutí.",
    )
    out = await fact_lookup.lookup("P1", with_temporal=True)
    assert out, "regex fallback should extract at least the deadline"
    assert all(r.temporal_superseded is True for r in out)
    assert all(r.source == "regex" for r in out)


async def test_regex_fallback_off_is_byte_identical(monkeypatch):
    _stub(
        monkeypatch,
        rows=[],
        pit_status="SUPERSEDED",
        temporal_status=None,
        prov_text="Oznámení se podá do 30 dnů ode dne rozhodnutí.",
    )
    out = await fact_lookup.lookup("P1")  # OFF
    assert out
    assert all("temporal_superseded" not in r.as_dict() for r in out)


# ── the predicate itself ─────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "pit,doc,expect",
    [
        ("SUPERSEDED", None, True),
        ("superseded", None, True),  # case-insensitive
        ("NOT_YET_IN_FORCE", None, True),
        ("not-yet-in-force", None, True),  # hyphen-insensitive
        (None, "repealed", True),
        (None, "annulled_by_cc", True),
        (None, "future_effective", True),
        ("IN_FORCE", None, False),
        (None, "in-force", False),
        (None, None, False),  # NULL ⇒ fail-open (not flagged)
        ("", "", False),
        ("UNKNOWN", "mystery", False),  # unknown ⇒ fail-open
    ],
)
def test_current_only_pred_fail_open_matrix(pit, doc, expect):
    assert _CURRENT_ONLY_PRED(pit, doc) is expect


# ── V-FACT SHADOW consumption via the step-21 wire ───────────────────────────────────────────────────────
def _claim(text, ctype, pid):
    from pythia.reasoning.claim_ledger import Claim

    return Claim(
        id="c1",
        span=(0, len(text)),
        text=text,
        slot="rule",
        type=ctype,
        type_source="regex",
        chips=[{"provision_id": pid}],
        normative=True,
    )


def test_vfact_records_superseded_in_shadow_without_changing_status():
    """A SUPERSEDED fact row (with_temporal join) is recorded in the V-FACT verdict evidence in SHADOW; the
    pass STATUS is unchanged (enforce-consumption stays flip-gated)."""
    from pythia.reasoning import v_fact

    # A deadline claim whose token 'do 30 dnů' IS grounded in the supplied (superseded) fact row ⇒ PASS.
    sup_row = FactRow(
        fact_type="deadline",
        value_raw="do 30 dnů",
        char_start=0,
        char_end=9,
        value_norm=None,
        extraction_method="regex",
        source="table",
        pit_status="SUPERSEDED",
        temporal_status=None,
        temporal_superseded=True,
        _with_temporal=True,
    )
    evidence = {"cited_texts": {"P1": "neutrální text"}, "fact_rows": {"P1": [sup_row]}}
    verdicts = v_fact.verify(_claim("Podejte to do 30 dnů.", "deadline", "P1"), evidence, settings=None)
    assert len(verdicts) == 1
    v = verdicts[0]
    assert v.status == "pass"  # STATUS unchanged — shadow only
    assert v.reason == "fact_tokens_grounded"
    assert v.evidence.get("temporal_superseded") is True  # but the superseded row IS recorded
    assert "P1" in v.evidence.get("temporal_pids", [])


def test_vfact_no_temporal_annotation_when_rows_lack_flag():
    """Fact rows WITHOUT the temporal join (no flag) ⇒ no temporal annotation ⇒ byte-identical verdict."""
    from pythia.reasoning import v_fact

    plain_row = FactRow(
        fact_type="deadline",
        value_raw="do 30 dnů",
        char_start=0,
        char_end=9,
        value_norm=None,
        extraction_method="regex",
        source="table",
    )
    evidence = {"cited_texts": {"P1": "neutrální text"}, "fact_rows": {"P1": [plain_row]}}
    v = v_fact.verify(_claim("Podejte to do 30 dnů.", "deadline", "P1"), evidence, settings=None)[0]
    assert v.status == "pass"
    assert "temporal_superseded" not in v.evidence
    assert "temporal_pids" not in v.evidence
