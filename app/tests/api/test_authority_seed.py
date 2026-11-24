"""ops-76 — the competent-authority SEED loader + the Pilsen resolve, ALL OFFLINE.

No live DB, no network. Two halves:

  1. seed_authorities — the ops-76 DRAFT seed step. Pure parsers + a fake capturing
     session pin: parse_topic_authority_rows normalisation/priority; the seed SQL is
     INSERT … ON CONFLICT DO NOTHING (idempotent, provenance-preserving — never a
     re-write); the topic seed is TABLE-ABSENCE-SAFE (no-ops when topic_authority
     is missing, as on a DB carrying only migration 022).

  2. the Pilsen resolve — a Plzeň municipality_code resolves ≥1 concrete authority
     END-TO-END through the W4 `/api/answer` resolver against the committed DRAFT
     fixtures (the SAME fixtures the seed loader loads). The sidecar is mocked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from regsuper.api import answer as answer_api
from regsuper.ingest import seed_authorities as seed

# Reuse the endpoint test's harness (bare-router client + settings/answer builders).
from tests.api.test_answer_endpoint import (
    _answer_with_facts,
    _client,
    _settings,
)

_AUTH_FIX = Path(__file__).resolve().parents[1] / "fixtures" / "authority"
_SEED = str(_AUTH_FIX / "authority_registry.sample.json")
_TOPICS = str(_AUTH_FIX / "topic_authority.sample.json")
_GAZ = str(_AUTH_FIX / "municipality_gazetteer.sample.json")


# ═══════════════════════════════════════════════════════════════════════════
# 1. seed_authorities — topic-row parsing (pure, DB-free)
# ═══════════════════════════════════════════════════════════════════════════
def test_parse_topic_authority_rows_priority_and_normalisation() -> None:
    rows = seed.load_topic_authority_rows(_TOPICS)
    # 12 DRAFT topics; 'zamestnavani' fans out to 3 types → total > 12 rows.
    by_topic: dict[str, list[dict]] = {}
    for r in rows:
        by_topic.setdefault(r["topic_key"], []).append(r)
    assert len(by_topic) == 12, "12 DRAFT topics seeded"
    # Single-type topic: priority 0, canonical tag.
    assert by_topic["dan_z_prijmu"] == [
        {"topic_key": "dan_z_prijmu", "authority_type": "financni_urad", "priority": 0}
    ]
    # Multi-type topic keeps list order as ascending priority.
    empl = by_topic["zamestnavani"]
    assert [(r["authority_type"], r["priority"]) for r in empl] == [
        ("ossz", 0),
        ("oip", 1),
        ("financni_urad", 2),
    ]


def test_parse_topic_authority_rows_drops_unknown_and_dedups() -> None:
    raw = {
        "topics": {
            "t1": ["finanční úřad", "financni_urad", "nonsense", "OSSZ"],  # label+dup+bad
            "t2": ["who-knows"],  # all invalid → topic contributes nothing
            "": ["financni_urad"],  # blank key → dropped
            "t3": "not-a-list",  # bad shape → dropped
        }
    }
    rows = seed.parse_topic_authority_rows(raw)
    t1 = [(r["authority_type"], r["priority"]) for r in rows if r["topic_key"] == "t1"]
    assert t1 == [("financni_urad", 0), ("ossz", 1)]  # deduped, label→tag, bad dropped
    assert not any(r["topic_key"] in ("t2", "", "t3") for r in rows)


def test_parse_topic_authority_rows_malformed_is_empty_not_raise() -> None:
    assert seed.parse_topic_authority_rows({}) == []
    assert seed.parse_topic_authority_rows({"topics": None}) == []
    assert seed.parse_topic_authority_rows({"nope": 1}) == []


# ═══════════════════════════════════════════════════════════════════════════
# 1b. seed_authorities — the DO NOTHING upserts (fake capturing session)
# ═══════════════════════════════════════════════════════════════════════════
class _FakeSession:
    """Captures (sql, rows) execute() calls; answers to_regclass probes from a set
    of tables it 'has'. No real DB."""

    def __init__(self, existing_tables: set[str] | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self._tables = existing_tables if existing_tables is not None else set()

    async def execute(self, sql, params=None):
        s = str(sql)
        self.calls.append((s, params))
        if "to_regclass" in s:
            name = (params or {}).get("n")
            return _ScalarResult(name if name in self._tables else None)
        return _ScalarResult(None)


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self):
        return self._value


class _FakeScope:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


async def test_seed_authorities_uses_do_nothing_and_orders_fk(monkeypatch) -> None:
    session = _FakeSession()
    monkeypatch.setattr(seed, "session_scope", lambda: _FakeScope(session))
    parsed = seed.load_seed(_SEED)

    n_auth, n_jur = await seed.upsert_authorities(parsed)

    assert n_auth == 5 and n_jur >= 5
    assert len(session.calls) == 2
    # authority INSERT first (FK), then jurisdiction; BOTH ON CONFLICT DO NOTHING.
    assert "INTO authority " in session.calls[0][0]
    assert "ON CONFLICT (code) DO NOTHING" in session.calls[0][0]
    assert "authority_jurisdiction" in session.calls[1][0]
    assert "DO NOTHING" in session.calls[1][0]
    # Provenance-preserving: no DO UPDATE that would rewrite source_uri/fetched_at.
    assert "DO UPDATE" not in session.calls[0][0]


async def test_seed_authorities_is_idempotent_same_rows_each_run(monkeypatch) -> None:
    session = _FakeSession()
    monkeypatch.setattr(seed, "session_scope", lambda: _FakeScope(session))
    parsed = seed.load_seed(_SEED)

    first = await seed.upsert_authorities(parsed)
    second = await seed.upsert_authorities(parsed)
    assert first == second
    # Identical authority rows presented both runs (converges, never duplicates).
    assert session.calls[0][1] == session.calls[2][1]


async def test_seed_topic_authority_noops_when_table_absent(monkeypatch) -> None:
    session = _FakeSession(existing_tables=set())  # topic_authority NOT present
    monkeypatch.setattr(seed, "session_scope", lambda: _FakeScope(session))
    rows = seed.load_topic_authority_rows(_TOPICS)

    n = await seed.upsert_topic_authority(rows)

    assert n == 0
    # Only the to_regclass probe ran — no INSERT against the missing table.
    assert len(session.calls) == 1
    assert "to_regclass" in session.calls[0][0]


async def test_seed_topic_authority_inserts_when_table_present(monkeypatch) -> None:
    session = _FakeSession(existing_tables={"topic_authority"})
    monkeypatch.setattr(seed, "session_scope", lambda: _FakeScope(session))
    rows = seed.load_topic_authority_rows(_TOPICS)

    n = await seed.upsert_topic_authority(rows)

    assert n == len(rows) > 0
    inserts = [c for c in session.calls if "INTO topic_authority" in c[0]]
    assert len(inserts) == 1
    assert "ON CONFLICT (topic_key, authority_type) DO NOTHING" in inserts[0][0]


def test_dry_run_reports_counts_without_db() -> None:
    # No session_scope patched — dry_run must never open a session.
    n_auth, n_jur, n_topic = seed.dry_run(_SEED, _TOPICS)
    assert n_auth == 5
    assert n_jur >= 5
    assert n_topic > 12  # 12 topics, one fanning out to 3 types


# ═══════════════════════════════════════════════════════════════════════════
# 2. the Pilsen resolve — END-TO-END through the W4 /api/answer resolver
# ═══════════════════════════════════════════════════════════════════════════
def _auth_settings(**kw):
    """Settings with the W4 authority slot ON + the DRAFT sample seed/map/gazetteer —
    the SAME fixtures the ops-76 seed loader loads into the DB."""
    return _settings(
        authority_registry_enabled=True,
        authority_seed_path=_SEED,
        authority_topic_map_path=_TOPICS,
        authority_gazetteer_path=_GAZ,
        **kw,
    )


def test_pilsen_municipality_resolves_authority_end_to_end() -> None:
    """A Plzeň (municipality_code 554791 → kraj CZ032) tax query resolves ≥1 concrete
    authority through the full W4 `/api/answer` path against the seeded DRAFT fixtures —
    FU-PLK-PLZEN, matched at the kraj grain, DRAFT posture. OFFLINE (sidecar mocked, no DB)."""
    answer_api._reset_authority_registry_cache()
    ans = _answer_with_facts(obligation="tax", municipality="554791")  # Plzeň
    sidecar = AsyncMock(return_value=ans)
    with (
        patch.object(answer_api, "get_settings", lambda: _auth_settings()),
        patch.object(answer_api.pythia, "answer", sidecar),
    ):
        resp = _client().post("/api/answer", json={"query": "Kam v Plzni podat daň z příjmu?"})
    assert resp.status_code == 200, resp.text
    auth = resp.json()["authorities"]
    assert len(auth) >= 1, "a Pilsen query resolves ≥1 authority"
    a = auth[0]
    assert a["authority_type"] == "financni_urad"
    assert a["match_kind"] == "kraj"  # Plzeň FÚ is stamped at kraj CZ032
    assert a["is_draft"] is True  # DRAFT / advocate-review-pending
    assert "Plzeň" in a["name"] or "Plzeňský" in a["name"]
    # WHITELISTED shape — no free-text address/databox/source_uri leaks to the surface.
    assert set(a.keys()) == {"authority_type", "name", "match_kind", "is_draft"}
    answer_api._reset_authority_registry_cache()


def test_as_date_coerces_iso_string_for_asyncpg():
    """VL-ops-76 regression (live-caught): asyncpg binds a DATE column from a date object, not a str — the
    fixture's ISO fetched_at must be coerced. Pins the coercion the offline mocked-session test can't reach."""
    from datetime import date, datetime

    from regsuper.ingest.seed_authorities import _as_date

    assert _as_date("2026-07-03") == date(2026, 7, 3)  # the fixture shape that broke the live seed
    assert _as_date(date(2026, 7, 3)) == date(2026, 7, 3)  # already a date → passthrough
    assert _as_date(datetime(2026, 7, 3, 12, 0)) == date(2026, 7, 3)
    assert _as_date(None) is None and _as_date("") is None and _as_date("not-a-date") is None
