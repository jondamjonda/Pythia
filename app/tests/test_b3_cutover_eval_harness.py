"""B3-05 off-flip eval harness — the sanctioned, scoped way to GENERATE the B3-06a lawyer-review sample under
law_source='nexus' WITHOUT relaxing the live gate, plus the cardinal-rule guard that the findings are written
ONLY to an isolated database (never the live regsuper / pythia corpus).

Three layers under test, all DB-free / no-network:
  1. the gate's eval-mode exception — refuses nexus for live traffic; permits it ONLY inside cutover_eval_mode()
     which is DOUBLE-LOCKED (a ContextVar AND a process-level REGSUPER_CUTOVER_EVAL flag), fail-closed + reset.
  2. assert_isolated_sink — refuses, by PHYSICAL-DB identity (not raw strings), the live regsuper/sync/pythia
     DSNs and any spelling that aliases them; requires an eval-looking DB name unless --i-understand-isolated.
  3. _run_eval rebinding the engine to the isolated sink + collecting ONLY this run's nexus findings.
"""

from __future__ import annotations

import importlib.util
import pathlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from regsuper import analysis
from regsuper.analysis import (
    LawSourceNotSupported,
    cutover_eval_active,
    cutover_eval_mode,
    require_cascade_law_source,
)

_HARNESS_PATH = Path(__file__).resolve().parents[1] / "scripts" / "cutover_eval_harness.py"
_spec = importlib.util.spec_from_file_location("cutover_eval_harness", _HARNESS_PATH)
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)

_LIVE = "postgresql://regsuper:regsuper@localhost:5432/regsuper"
_EVAL = "postgresql://regsuper:regsuper@localhost:5432/regsuper_eval"


def _nexus():
    return SimpleNamespace(law_source="nexus")


def _regsuper():
    return SimpleNamespace(law_source="regsuper")


@pytest.fixture
def eval_flag(monkeypatch):
    """The process-level double-lock the harness sets; eval-mode refuses to activate without it."""
    monkeypatch.setenv("REGSUPER_CUTOVER_EVAL", "1")


# --------------------------------------------------------------------------- the gate's eval-mode exception
@pytest.mark.parametrize("layer", ["L2", "L3", "L4"])
def test_gate_refuses_nexus_for_live_traffic(layer):
    with patch.object(analysis, "get_settings", _nexus):
        assert cutover_eval_active() is False
        with pytest.raises(LawSourceNotSupported):
            require_cascade_law_source(layer)


@pytest.mark.parametrize("layer", ["L2", "L3", "L4"])
def test_gate_permits_nexus_inside_eval_mode(layer, eval_flag):
    with patch.object(analysis, "get_settings", _nexus):
        with cutover_eval_mode():
            assert cutover_eval_active() is True
            require_cascade_law_source(layer)  # must NOT raise — the sanctioned off-flip exception


def test_cutover_eval_mode_refuses_without_process_flag(monkeypatch):
    """The double-lock: without REGSUPER_CUTOVER_EVAL=1, entering the context manager RAISES — so an accidental
    use on a serving path cannot silently bypass the gate."""
    monkeypatch.delenv("REGSUPER_CUTOVER_EVAL", raising=False)
    with pytest.raises(RuntimeError):
        with cutover_eval_mode():
            pass
    assert cutover_eval_active() is False


def test_eval_mode_is_scoped_and_fail_closed_again_on_exit(eval_flag):
    with patch.object(analysis, "get_settings", _nexus):
        with cutover_eval_mode():
            require_cascade_law_source("L2")  # allowed inside
        assert cutover_eval_active() is False
        with pytest.raises(LawSourceNotSupported):  # refused again outside
            require_cascade_law_source("L2")


def test_eval_mode_resets_even_on_exception(eval_flag):
    with pytest.raises(ValueError):
        with cutover_eval_mode():
            assert cutover_eval_active() is True
            raise ValueError("boom")
    assert cutover_eval_active() is False  # the ContextVar token was reset


def test_gate_regsuper_is_unaffected_by_eval_mode(eval_flag):
    with patch.object(analysis, "get_settings", _regsuper):
        require_cascade_law_source("L2")
        with cutover_eval_mode():
            require_cascade_law_source("L2")  # eval-mode changes nothing for regsuper


def test_unknown_source_refused_even_inside_eval_mode(eval_flag):
    # eval-mode is for 'nexus' ONLY, not a blanket bypass.
    with patch.object(
        analysis, "get_settings", lambda: SimpleNamespace(law_source="something-else")
    ):
        with cutover_eval_mode():
            with pytest.raises(LawSourceNotSupported):
                require_cascade_law_source("L2")


def test_eval_hook_is_not_in_public_api_and_not_imported_by_any_route():
    assert "cutover_eval_mode" not in getattr(analysis, "__all__", [])
    api_dir = pathlib.Path(analysis.__file__).resolve().parent.parent / "api"
    offenders = [
        str(p.relative_to(api_dir))
        for p in api_dir.rglob("*.py")
        if "cutover_eval_mode" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        f"the off-flip eval hook must never be imported on a serving path: {offenders}"
    )


# --------------------------------------------------------------------------- assert_isolated_sink (DB safety)
def test_guard_refuses_missing_arg():
    with pytest.raises(SystemExit):
        harness.assert_isolated_sink(None, [_LIVE])


def test_guard_refuses_non_eval_db_name():
    # A live-looking prod DB name (not eval/test/sandbox/scratch) is refused even if its identity isn't listed.
    with pytest.raises(SystemExit):
        harness.assert_isolated_sink("postgresql://u:p@otherhost:5432/regsuper_prod", [_LIVE])


@pytest.mark.parametrize(
    "alias",
    [
        "postgresql://u:p@127.0.0.1:5432/sample_eval",  # localhost alias
        "postgresql+asyncpg://u:p@localhost/sample_eval",  # driver tag + omitted port
        "postgres://u:p@0.0.0.0:5432/sample_eval?sslmode=require",  # scheme variant + query
        "postgresql://u:p@localhost:5432/sample_eval/",  # trailing slash
        "postgresql://DIFFERENT:CREDS@localhost:5432/sample_eval",  # different credentials
    ],
)
def test_guard_refuses_every_alias_of_a_forbidden_live_db(alias):
    # A live DB that happens to be eval-NAMED — only the PHYSICAL-identity check can catch it, and it must,
    # through every spelling (host alias / port / driver / scheme / query / slash / creds).
    forbidden = ["postgresql+asyncpg://live:live@localhost:5432/sample_eval"]
    with pytest.raises(SystemExit):
        harness.assert_isolated_sink(alias, forbidden)


def test_guard_refuses_the_live_pythia_corpus():
    pythia = "postgresql://pythia:pythia@localhost:5432/pythia_eval"  # eval-named but it's the live pythia
    with pytest.raises(SystemExit):
        harness.assert_isolated_sink("postgresql+asyncpg://x@127.0.0.1/pythia_eval", [None, pythia])


def test_guard_allows_a_genuinely_isolated_sink():
    harness.assert_isolated_sink(_EVAL, [_LIVE])  # same host, DIFFERENT db name → permitted


def test_guard_allow_any_name_still_enforces_identity():
    # --i-understand-isolated skips the NAME check but the live-identity refusal still stands.
    with pytest.raises(SystemExit):
        harness.assert_isolated_sink(
            "postgresql+asyncpg://x@127.0.0.1:5432/regsuper", [_LIVE], allow_any_name=True
        )
    harness.assert_isolated_sink(
        "postgresql://x@otherhost/regsuper_prod", [_LIVE], allow_any_name=True
    )  # not live → OK


def test_db_identity_collapses_localhost_aliases_and_default_port():
    assert harness._db_identity("postgresql://u@127.0.0.1/db") == harness._db_identity(
        "postgresql+asyncpg://u@localhost:5432/db"
    )
    assert harness._db_identity("postgres://u@0.0.0.0:5432/db?x=1") == ("localhost", 5432, "db")
    assert (
        harness._db_identity("postgresql://u@host/DBNAME")[2] == "DBNAME"
    )  # db name is case-SENSITIVE


# --------------------------------------------------------------------------- dry-run (writes nothing)
def test_dry_run_validates_and_writes_nothing(capsys):
    rc = harness.main(
        ["--project-id", "11111111-1111-1111-1111-111111111111", "--eval-database-url", _EVAL]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "ISOLATED" in out and "--apply" in out


def test_dry_run_still_refuses_a_live_sink():
    with pytest.raises(SystemExit):
        harness.main(
            ["--project-id", "11111111-1111-1111-1111-111111111111", "--eval-database-url", _LIVE]
        )


# --------------------------------------------------------------------------- _run_eval binds to the isolated sink
async def test_run_eval_binds_to_isolated_sink_and_runs_in_eval_mode(monkeypatch):
    """The cardinal no-write-to-live property: _run_eval disposes the cached engine, asserts the runtime DB is
    the eval sink, runs the cascade inside eval-mode, and (here) never touches a live DB."""
    eval_dsn = _EVAL
    monkeypatch.setenv("DATABASE_URL", eval_dsn)
    monkeypatch.setenv("REGSUPER_CUTOVER_EVAL", "1")

    from regsuper.config import get_settings

    get_settings.cache_clear()

    import regsuper.analysis.l2_applicability as l2
    import regsuper.analysis.l3_obligations as l3
    import regsuper.analysis.l4_gaps as l4
    import regsuper.storage.database as db

    rec: dict = {}

    async def fake_run_l2(pid, **k):
        rec["eval_active"] = cutover_eval_active()
        rec["db"] = get_settings().database_url  # the DSN the cascade would write to
        return SimpleNamespace(findings=[])

    async def fake_noop(*_a, **_k):
        return SimpleNamespace(findings=[])

    async def fake_dispose():
        rec["disposed"] = True

    class _Scalars:
        def all(self):
            return []

    class _Exec:
        def scalars(self):
            return _Scalars()

    class _Session:
        async def execute(self, *_a, **_k):
            return _Exec()

    import contextlib

    @contextlib.asynccontextmanager
    async def fake_scope():
        yield _Session()

    monkeypatch.setattr(l2, "run_l2", fake_run_l2)
    monkeypatch.setattr(l3, "run_l3", fake_noop)
    monkeypatch.setattr(l4, "run_l4_gaps", fake_noop)
    monkeypatch.setattr(db, "dispose_engine", fake_dispose)
    monkeypatch.setattr(db, "session_scope", fake_scope)

    out = await harness._run_eval(uuid4(), eval_dsn=eval_dsn, limit=None, scope="full")

    assert out == []
    assert rec["disposed"] is True  # the pre-built engine was dropped
    assert rec["eval_active"] is True  # the cascade ran inside eval-mode
    assert harness._db_identity(rec["db"]) == harness._db_identity(
        eval_dsn
    )  # bound to the eval sink, never live


async def test_run_eval_crosswalk_scope_threads_scoped_candidates_into_l2(monkeypatch):
    """scope='crosswalk' (the default + the B3-06a sample mode): _run_eval computes crosswalk-scoped NEXUS
    candidates and passes them to run_l2(candidates=...), so the cascade analyses the app's own acts (every
    cited_document resolves) instead of falling back to the broad nexus L1 that hits the coverage ceiling."""
    eval_dsn = _EVAL
    monkeypatch.setenv("DATABASE_URL", eval_dsn)
    monkeypatch.setenv("REGSUPER_CUTOVER_EVAL", "1")

    from regsuper.config import get_settings

    get_settings.cache_clear()

    import regsuper.analysis.l1_candidates as l1
    import regsuper.analysis.l2_applicability as l2
    import regsuper.analysis.l3_obligations as l3
    import regsuper.analysis.l4_gaps as l4
    import regsuper.storage.database as db

    scoped = [SimpleNamespace(provision_id=uuid4())]
    rec: dict = {}

    async def fake_scoped(_pid, *, top_k=200, as_of=None):
        rec["top_k"] = top_k
        return scoped

    async def fake_run_l2(_pid, **k):
        rec["l2_candidates"] = k.get("candidates")
        return SimpleNamespace(findings=[])

    async def fake_noop(*_a, **_k):
        return SimpleNamespace(findings=[])

    async def fake_dispose():
        return None

    class _Scalars:
        def all(self):
            return []

    class _Exec:
        def scalars(self):
            return _Scalars()

    class _Session:
        async def execute(self, *_a, **_k):
            return _Exec()

    import contextlib

    @contextlib.asynccontextmanager
    async def fake_scope():
        yield _Session()

    monkeypatch.setattr(l1, "crosswalk_scoped_nexus_candidates", fake_scoped)
    monkeypatch.setattr(l2, "run_l2", fake_run_l2)
    monkeypatch.setattr(l3, "run_l3", fake_noop)
    monkeypatch.setattr(l4, "run_l4_gaps", fake_noop)
    monkeypatch.setattr(db, "dispose_engine", fake_dispose)
    monkeypatch.setattr(db, "session_scope", fake_scope)

    out = await harness._run_eval(
        uuid4(), eval_dsn=eval_dsn, limit=None, scope="crosswalk", top_k=123
    )

    assert out == []
    assert rec["top_k"] == 123  # --top-k threaded into the scoped L1
    assert rec["l2_candidates"] is scoped  # the scoped candidates were handed to run_l2, not None


async def test_run_eval_aborts_if_runtime_db_is_not_the_eval_sink(monkeypatch):
    """Defense: if the env override did NOT take (the runtime DB is something else), _run_eval refuses before
    running the cascade — it never writes to a DB that isn't the declared isolated sink."""
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://u@otherhost:5432/regsuper"
    )  # NOT the eval sink
    monkeypatch.setenv("REGSUPER_CUTOVER_EVAL", "1")
    from regsuper.config import get_settings

    get_settings.cache_clear()

    import regsuper.storage.database as db

    async def fake_dispose():
        return None

    monkeypatch.setattr(db, "dispose_engine", fake_dispose)
    with pytest.raises(SystemExit):
        await harness._run_eval(uuid4(), eval_dsn=_EVAL, limit=None)
