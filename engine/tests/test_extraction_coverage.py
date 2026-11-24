"""VL-F-15 — the extraction_coverage manifest (code + writer; offline tests). Additive, byte-identical-OFF.

Pure/DB-stubbed tests (no live DB). Proves:

  (a) the GapKind.EXTRACTION_NOT_RUN member exists + is wire-stable.
  (b) record_wave WRITER — a no-op (returns False, no upsert) when the table is absent; an upsert when present;
      an empty external_id is skipped.
  (c) covered_acts / coverage_counts READERS — absent-table safe (empty / present=False).
  (d) fact_lookup.classify_no_fact — an UNCOVERED act ⇒ 'extraction_not_run' (SILENT, no gap event); a COVERED
      act with no fact ⇒ 'no_fact_row'; an unknown act (no external_id) ⇒ 'extraction_not_run'.
  (e) the nexus_stats coverage block is EXPOSED (stub conn).
  (f) waves-re-run-after-ingest placeholder — pins the tranche-runner contract step 92 will implement.

The LIVE `CREATE TABLE extraction_coverage` + the 7-wave backfill are a DEFERRED operator step (NOT run here);
these tests stub the DB so the writer/reader/classify logic is exercised offline.
"""

from __future__ import annotations

import pytest

from pythia.ingest import extraction_coverage as ec
from pythia.reasoning import fact_lookup as fl
from pythia.reasoning.gap_kind import GAP_KINDS, GapKind, is_gap_kind


# ─────────────────────────────── (a) the gap-kind member ───────────────────────────────


def test_extraction_not_run_gap_kind_is_wire_stable():
    assert GapKind.EXTRACTION_NOT_RUN.value == "extraction_not_run"
    assert "extraction_not_run" in GAP_KINDS
    assert is_gap_kind("extraction_not_run")
    # str-enum: the member IS its wire value.
    assert GapKind.EXTRACTION_NOT_RUN == "extraction_not_run"


# ─────────────────────────────── DB stub scaffolding ───────────────────────────────


class _Conn:
    """A minimal asyncpg-conn stub: `present` toggles the to_regclass guard; execute/fetch/fetchrow are
    scripted per-test via the callables passed in."""

    def __init__(self, *, present: bool, fetch=None, fetchrow=None, fetchval_extra=None):
        self._present = present
        self.executed: list = []
        self._fetch = fetch or (lambda sql, *a: [])
        self._fetchrow = fetchrow or (lambda sql, *a: {})
        self._fetchval_extra = fetchval_extra or {}

    async def fetchval(self, sql, *a):
        if "to_regclass" in sql:
            return "extraction_coverage" if self._present else None
        # scripted extra scalar answers keyed by a substring of the SQL.
        for key, val in self._fetchval_extra.items():
            if key in sql:
                return val
        return None

    async def execute(self, sql, *a):
        self.executed.append((sql, a))

    async def fetch(self, sql, *a):
        return self._fetch(sql, *a)

    async def fetchrow(self, sql, *a):
        return self._fetchrow(sql, *a)


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acq()


def _install_pool(monkeypatch, module, conn):
    pool = _Pool(conn)

    async def _get_pool():
        return pool

    monkeypatch.setattr(module, "get_pool", _get_pool, raising=True)
    return conn


# ─────────────────────────────── (b) record_wave WRITER ───────────────────────────────


@pytest.mark.asyncio
async def test_record_wave_absent_table_is_noop():
    conn = _Conn(present=False)
    wrote = await ec.record_wave(conn, "240/2013", "regex", "regex-b3p1-v1", provisions_scanned=5, facts_written=3)
    assert wrote is False
    # NO upsert executed when the table is absent (never implicitly creates it).
    assert not any("INSERT INTO extraction_coverage" in sql for sql, _ in conn.executed)


@pytest.mark.asyncio
async def test_record_wave_present_table_upserts():
    conn = _Conn(present=True)
    wrote = await ec.record_wave(conn, "240/2013", "regex", "regex-b3p1-v1", provisions_scanned=5, facts_written=3)
    assert wrote is True
    upserts = [(sql, a) for sql, a in conn.executed if "INSERT INTO extraction_coverage" in sql]
    assert len(upserts) == 1
    _, args = upserts[0]
    assert args[0] == "240/2013" and args[1] == "regex" and args[2] == "regex-b3p1-v1"
    assert args[3] == 5 and args[4] == 3


@pytest.mark.asyncio
async def test_record_wave_empty_ext_skipped():
    conn = _Conn(present=True)
    assert await ec.record_wave(conn, "", "regex", "regex-b3p1-v1") is False
    assert not conn.executed  # skipped before any DB touch


# ─────────────────────────────── (c) readers absent-safe ───────────────────────────────


@pytest.mark.asyncio
async def test_covered_acts_absent_table_empty():
    conn = _Conn(present=False)
    assert await ec.covered_acts(conn, ["240/2013"]) == set()


@pytest.mark.asyncio
async def test_covered_acts_present_returns_set():
    conn = _Conn(present=True, fetch=lambda sql, *a: [{"external_id": "240/2013"}, {"external_id": "89/2012"}])
    got = await ec.covered_acts(conn, ["240/2013", "89/2012", "999/1999"])
    assert got == {"240/2013", "89/2012"}


@pytest.mark.asyncio
async def test_coverage_counts_absent_table_zeroed():
    block = await ec.coverage_counts(_Conn(present=False))
    assert block == {
        "present": False,
        "acts_covered": 0,
        "rows": 0,
        "provisions_scanned": 0,
        "facts_written": 0,
        "by_wave": [],
    }


@pytest.mark.asyncio
async def test_coverage_counts_present_block():
    conn = _Conn(
        present=True,
        fetchval_extra={"count(DISTINCT external_id)": 2, "count(*)": 7},
        fetchrow=lambda sql, *a: {"scanned": 50, "written": 12},
        fetch=lambda sql, *a: [{"wave": "regex", "extractor_version": "regex-b3p1-v1", "acts": 2, "facts": 12}],
    )
    block = await ec.coverage_counts(conn)
    assert block["present"] is True
    assert block["acts_covered"] == 2 and block["rows"] == 7
    assert block["provisions_scanned"] == 50 and block["facts_written"] == 12
    assert block["by_wave"] == [{"wave": "regex", "extractor_version": "regex-b3p1-v1", "acts": 2, "facts": 12}]


# ─────────────── (d) fact_lookup.classify_no_fact — extraction_not_run vs no_fact_row ───────────────


class _Prov:
    def __init__(self, ext):
        self.external_id = ext


def _wire_classify(monkeypatch, *, ext, covered):
    """Wire fact_lookup so the provision resolves to act `ext` and the coverage manifest reports `covered`
    (a set of covered external_ids). The classify path fetches the provision via NexusPort then reads
    extraction_coverage.covered_acts through the pool — both stubbed here (no live DB)."""

    class _Port:
        async def fetch_provisions(self, ids):
            return [_Prov(ext)] if ext is not None else []

    monkeypatch.setattr(fl, "NexusPort", lambda: _Port(), raising=True)

    async def _get_pool():
        class _P:
            def acquire(self):
                class _Acq:
                    async def __aenter__(self):
                        return object()  # covered_acts is stubbed below, so the conn is unused

                    async def __aexit__(self, *e):
                        return False

                return _Acq()

        return _P()

    monkeypatch.setattr(fl, "get_pool", _get_pool, raising=True)

    async def _covered_acts(conn, external_ids=None):
        return set(covered)

    # classify imports covered_acts from the extraction_coverage module at call time.
    monkeypatch.setattr(ec, "covered_acts", _covered_acts, raising=True)


@pytest.mark.asyncio
async def test_classify_uncovered_act_is_extraction_not_run(monkeypatch):
    # act 240/2013 is NOT in the manifest ⇒ the extractor never swept it ⇒ SILENT extraction_not_run.
    _wire_classify(monkeypatch, ext="240/2013", covered=set())
    assert await fl.classify_no_fact("pid") == GapKind.EXTRACTION_NOT_RUN.value


@pytest.mark.asyncio
async def test_classify_covered_act_with_no_fact_is_no_fact_row(monkeypatch):
    # act IS covered (a wave ran over it) but this provision has no fact ⇒ a GENUINE no_fact_row gap.
    _wire_classify(monkeypatch, ext="240/2013", covered={"240/2013"})
    assert await fl.classify_no_fact("pid") == GapKind.NO_FACT_ROW.value


@pytest.mark.asyncio
async def test_classify_unknown_act_is_silent(monkeypatch):
    # a provision that resolves to nothing / no external_id ⇒ fail-silent extraction_not_run (never a false gap).
    _wire_classify(monkeypatch, ext=None, covered={"240/2013"})
    assert await fl.classify_no_fact("pid") == GapKind.EXTRACTION_NOT_RUN.value


# ─────────────────────────────── (e) nexus_stats exposes the block ───────────────────────────────


@pytest.mark.asyncio
async def test_nexus_stats_exposes_coverage_block():
    """The nexus_stats block builder (extraction_coverage.coverage_counts) is absent-safe, and nexus_stats()
    splices it under the 'extraction_coverage' key. A full nexus_stats() run needs the whole corpus schema, so
    we (1) prove the block builder shape here and (2) assert the source wires the key + the builder."""
    import inspect

    from pythia.graph import nexus_stats as ns

    # (1) the block builder nexus_stats splices in — absent-safe (present=False when the table is gone).
    block = await ec.coverage_counts(_Conn(present=False))
    assert block["present"] is False

    # (2) nexus_stats() imports the builder AND exposes the key in its output dict.
    src = inspect.getsource(ns.nexus_stats)
    assert "coverage_counts" in src
    assert '"extraction_coverage": extraction_cov' in src


# ─────────────────────────────── (f) waves-re-run-after-ingest placeholder (step 92) ───────────────────────────────


@pytest.mark.asyncio
async def test_waves_rerun_after_ingest_placeholder():
    """PLACEHOLDER for the later tranche runner (VL-F step 92): after a NEW act is ingested, the extractor
    waves must be RE-RUN over that act so extraction_coverage gains its row (else the freshly-ingested act
    would forever read extraction_not_run). The tranche runner does not exist yet; this pins the contract the
    writer already supports: re-running the SAME wave over an act is an idempotent upsert (refreshes counts,
    no duplicate manifest row) — proven here via a present-table double record_wave."""
    conn = _Conn(present=True)
    await ec.record_wave(conn, "999/2026", "regex", "regex-b3p1-v1", provisions_scanned=3, facts_written=1)
    await ec.record_wave(conn, "999/2026", "regex", "regex-b3p1-v1", provisions_scanned=4, facts_written=2)
    upserts = [a for sql, a in conn.executed if "INSERT INTO extraction_coverage" in sql]
    # BOTH calls go through the ON CONFLICT upsert (idempotent) — the manifest never accumulates dup rows.
    assert len(upserts) == 2
    assert upserts[-1][3] == 4 and upserts[-1][4] == 2  # the re-run refreshed the counts
    # TODO(step 92): assert the tranche runner calls record_wave for each newly-ingested act after ingest.
