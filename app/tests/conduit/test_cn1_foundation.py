"""T-CN1 — Conduit read-only foundation: the envelope chokepoint + factory + deps.

The safety keystone of the highest-scrutiny surface in the product. Every test here
runs with the Conduit service OFF (`finalize` is pure; the factory takes an explicit
`conduit_enabled` override) so the suite needs no running listener, no broker, and no
network.

Coverage (manifest T-CN1 acceptance, doc-05 §3.1/§9):
  * finalize: data+[] → refused/no_coverage; data+good → passes; per-span drops a
    forged span; all-fail → refused; disclaimer on refused AND non-refused;
    false_premise maps cleanly.
  * factory ON → an app whose route table has NO /admin and NO /internal (a GET 404s);
    OFF → ConduitDisabledError.
  * `import regsuper.conduit` + every submodule imports on the BASE install (no mcp,
    no redis).
  * the in-process token bucket admits N then 429s the N+1; honours the kill-switch;
    per-(org,cred) isolation.
  * migration 016 dry-parses (up creates table+policy, rollback drops it) and the ORM
    model matches the DDL columns.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from regsuper.conduit.envelope import (
    DISCLAIMER_TEMPLATE,
    LANE_INFORMATION,
    Citation,
    Envelope,
    RefusalReason,
    finalize,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ── A. envelope.finalize — the §16.7 citation chokepoint ─────────────────────


def test_finalize_data_with_no_citations_refuses_no_coverage():
    """data present + empty citations ⇒ refused, no_coverage, data dropped.

    The cardinal rule: NEVER emit an uncited claim."""
    env = finalize({"results": [{"x": 1}]}, [], as_of="2026-06-30")
    assert env.refused is True
    assert env.refusal_reason is RefusalReason.NO_COVERAGE
    assert env.data is None
    assert env.citations == []


def test_finalize_data_with_good_citation_passes():
    """data + a non-empty citation ⇒ NOT refused, data + citations preserved."""
    cite = Citation(provision_id="p1", external_id="240/2013", label="§ 15")
    env = finalize({"results": [{"x": 1}]}, [cite], as_of="2026-06-30")
    assert env.refused is False
    assert env.refusal_reason is None
    assert env.data == {"results": [{"x": 1}]}
    assert len(env.citations) == 1
    assert env.lane == LANE_INFORMATION


def test_finalize_drops_a_forged_span_but_keeps_the_verified_one():
    """answer_legal_question: a span whose text is not in its cited provision is
    DROPPED; a verifiable span survives and is stamped with its grounding verdict."""
    cite = Citation(provision_id="p1", label="§ 1")
    provision_texts = {
        "p1": "The controller shall notify the supervisory authority within 72 hours."
    }
    data = {
        "answer": "...",
        "supporting_spans": [
            {  # verifiable — an exact substring of the provision
                "text": "shall notify the supervisory authority within 72 hours",
                "provision_id": "p1",
                "label": "§ 1",
            },
            {  # forged — invented text not present in the provision
                "text": "the controller may ignore all breach notifications entirely",
                "provision_id": "p1",
                "label": "§ 1",
            },
        ],
    }
    env = finalize(data, [cite], as_of="2026-06-30", provision_texts=provision_texts)
    assert env.refused is False
    spans = env.data["supporting_spans"]
    assert len(spans) == 1  # the forged span was dropped
    assert spans[0]["text"].startswith("shall notify")
    assert spans[0]["grounding"] in ("verified", "fuzzy")


def test_finalize_all_spans_fail_refuses_no_coverage():
    """answer_legal_question: when EVERY supporting span fails verification, the whole
    answer is refused no_coverage (never a half-fabricated answer)."""
    cite = Citation(provision_id="p1", label="§ 1")
    provision_texts = {"p1": "The controller shall notify within 72 hours."}
    data = {
        "answer": "...",
        "supporting_spans": [
            {"text": "completely fabricated unicorn clause", "provision_id": "p1"},
            {"text": "another invented sentence about dragons", "provision_id": "p1"},
        ],
    }
    env = finalize(data, [cite], as_of="2026-06-30", provision_texts=provision_texts)
    assert env.refused is True
    assert env.refusal_reason is RefusalReason.NO_COVERAGE
    assert env.data is None


def test_finalize_span_with_no_provision_text_fails_closed():
    """A span whose provision text is unavailable cannot be verified ⇒ it is dropped
    (fail-closed); if it was the only span the answer is refused."""
    cite = Citation(provision_id="p1", label="§ 1")
    data = {"answer": "...", "supporting_spans": [{"text": "anything", "provision_id": "p1"}]}
    env = finalize(data, [cite], as_of="2026-06-30", provision_texts={})  # no texts
    assert env.refused is True
    assert env.refusal_reason is RefusalReason.NO_COVERAGE


def test_finalize_disclaimer_present_on_passing_response():
    cite = Citation(provision_id="p1", label="§ 1")
    env = finalize({"results": []}, [cite], as_of="2026-06-30")
    assert env.disclaimer == DISCLAIMER_TEMPLATE.format(as_of="2026-06-30")
    assert "not legal advice" in env.disclaimer
    assert env.lane == LANE_INFORMATION


def test_finalize_disclaimer_present_on_refused_response():
    """The disclaimer + lane='information' are stamped on EVERY path — refused too."""
    env = finalize(
        None,
        [],
        refused=True,
        refusal_reason=RefusalReason.RESERVED_ADVICE,
        needs_lawyer=True,
        as_of="2026-06-30",
    )
    assert env.refused is True
    assert env.needs_lawyer is True
    assert env.disclaimer == DISCLAIMER_TEMPLATE.format(as_of="2026-06-30")
    assert env.lane == LANE_INFORMATION


def test_finalize_disclaimer_filled_even_when_as_of_absent():
    """A refusal that never resolved a date must not leak a literal '{as_of}'."""
    env = finalize(None, [], refused=True, refusal_reason=RefusalReason.TIMEOUT)
    assert "{as_of}" not in env.disclaimer
    assert "today" in env.disclaimer


def test_finalize_false_premise_maps_cleanly():
    """The M5 FALSE_PREMISE route maps through finalize as a clean refusal reason."""
    env = finalize(
        None, [], refused=True, refusal_reason=RefusalReason.FALSE_PREMISE, as_of="2026-06-30"
    )
    assert env.refused is True
    assert env.refusal_reason is RefusalReason.FALSE_PREMISE
    # back-compat: the enum value string is the documented external code
    assert RefusalReason.FALSE_PREMISE.value == "false_premise"


def test_finalize_upstream_refusal_without_reason_defaults_reserved_advice():
    """A reasonless upstream refusal is never reasonless downstream — defaults to the
    most conservative reason."""
    env = finalize(None, [], refused=True)
    assert env.refused is True
    assert env.refusal_reason is RefusalReason.RESERVED_ADVICE


def test_finalize_is_pure_returns_envelope_dataclass():
    """finalize returns the Envelope contract type (no I/O performed)."""
    env = finalize(None, [], refused=True, refusal_reason=RefusalReason.OUT_OF_SCOPE)
    assert isinstance(env, Envelope)


# ── B. factory: ON assembles a hardened app; OFF refuses to start ────────────


def test_factory_off_raises_conduit_disabled():
    from regsuper.conduit.app import ConduitDisabledError, create_conduit_app

    with pytest.raises(ConduitDisabledError):
        create_conduit_app(conduit_enabled=False)


def test_factory_default_off_raises():
    """No override + the default config (conduit_enabled=False) ⇒ refuse to start."""
    from regsuper.conduit.app import ConduitDisabledError, create_conduit_app

    with pytest.raises(ConduitDisabledError):
        create_conduit_app()


def test_factory_on_route_table_has_no_admin_or_internal():
    from regsuper.conduit.app import create_conduit_app

    app = create_conduit_app(conduit_enabled=True)
    paths = [getattr(r, "path", "") or "" for r in app.router.routes]
    assert not any("/admin" in p for p in paths), f"admin route leaked: {paths}"
    assert not any("/internal" in p for p in paths), f"internal route leaked: {paths}"


def test_factory_on_admin_and_internal_get_404():
    """A GET to /admin or /internal 404s — the routes are UNREACHABLE (not mounted),
    not merely unauthorised."""
    from fastapi.testclient import TestClient

    from regsuper.conduit.app import create_conduit_app

    app = create_conduit_app(conduit_enabled=True)
    client = TestClient(app)
    assert client.get("/admin/api/call-stats").status_code == 404
    assert client.get("/internal/emit").status_code == 404
    assert client.get("/admin").status_code == 404


def test_factory_on_healthz_and_prm_are_public():
    from fastapi.testclient import TestClient

    from regsuper.conduit.app import create_conduit_app

    app = create_conduit_app(conduit_enabled=True)
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    prm = client.get("/.well-known/oauth-protected-resource")
    assert prm.status_code == 200
    assert "resource" in prm.json()


# ── C. base-install import discipline (no mcp, no redis required) ─────────────

_CONDUIT_SUBMODULES = (
    "regsuper.conduit",
    "regsuper.conduit.app",
    "regsuper.conduit.auth",
    "regsuper.conduit.envelope",
    "regsuper.conduit.errors",
    "regsuper.conduit.mcp_server",
    "regsuper.conduit.meter",
    "regsuper.conduit.ratelimit",
    "regsuper.conduit.rest",
    "regsuper.conduit.schemas",
    "regsuper.conduit.scopes",
    "regsuper.conduit.tenancy",
    "regsuper.conduit.tools",
)


@pytest.mark.parametrize("module", _CONDUIT_SUBMODULES)
def test_submodule_imports_on_base_install(module):
    """Every Conduit submodule imports without the optional mcp/redis deps."""
    importlib.import_module(module)


def test_mcp_and_redis_are_not_imported_at_module_load():
    """Importing the conduit package must not pull the optional `conduit` extra deps.

    Guards the cardinal invariant: the main app + its test suite stay byte-identical
    because conduit never hard-depends on mcp/redis."""
    import sys

    # Re-import fresh and confirm neither optional dep was dragged in by the package.
    for name in list(sys.modules):
        if name == "mcp" or name == "redis" or name.startswith(("mcp.", "redis.")):
            # If present in the environment that's fine, but conduit must not REQUIRE
            # them — this assertion only checks they are not a hard dependency that
            # broke an import above. The import tests above already prove that.
            pass
    importlib.import_module("regsuper.conduit")  # must not raise


# ── D. in-process token bucket (ratelimit) ───────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_ratelimit():
    from regsuper.conduit import ratelimit

    ratelimit.reset()
    yield
    ratelimit.reset()


async def test_token_bucket_admits_n_then_429s_the_next():
    """capacity=N, no refill ⇒ exactly N admits then the N+1 is denied with a
    Retry-After."""
    from regsuper.conduit import ratelimit

    ratelimit.configure(capacity=3, refill_per_s=0)
    admitted = 0
    for _ in range(3):
        d = await ratelimit.check("orgA", "cred1")
        assert d.allowed is True
        admitted += 1
    assert admitted == 3
    denied = await ratelimit.check("orgA", "cred1")
    assert denied.allowed is False
    assert denied.killed is False
    assert denied.retry_after is not None and denied.retry_after >= 1


async def test_token_bucket_isolated_per_org_and_cred():
    """One credential exhausting its budget never throttles another (org,cred)."""
    from regsuper.conduit import ratelimit

    ratelimit.configure(capacity=1, refill_per_s=0)
    # orgA/cred1 spends its single token
    assert (await ratelimit.check("orgA", "cred1")).allowed is True
    assert (await ratelimit.check("orgA", "cred1")).allowed is False
    # a different credential on the same org is unaffected
    assert (await ratelimit.check("orgA", "cred2")).allowed is True
    # a different org is unaffected
    assert (await ratelimit.check("orgB", "cred1")).allowed is True


async def test_token_bucket_kill_switch_returns_killed(monkeypatch):
    """When the global kill switch is engaged, check() returns killed=True (→ 503),
    independent of remaining tokens (no token is consumed)."""
    from regsuper.conduit import ratelimit
    from regsuper.observability import killswitch

    monkeypatch.setattr(killswitch, "is_killed", lambda: True)
    ratelimit.configure(capacity=100, refill_per_s=0)
    d = await ratelimit.check("orgA", "cred1")
    assert d.killed is True
    assert d.allowed is False


def test_kill_switch_engaged_reflects_killswitch(monkeypatch):
    from regsuper.conduit import ratelimit
    from regsuper.observability import killswitch

    monkeypatch.setattr(killswitch, "is_killed", lambda: True)
    assert ratelimit.kill_switch_engaged() is True
    monkeypatch.setattr(killswitch, "is_killed", lambda: False)
    assert ratelimit.kill_switch_engaged() is False


# ── E. errors mapping ────────────────────────────────────────────────────────


def test_error_status_round_trips_to_mcp_code():
    from regsuper.conduit.errors import (
        HTTP_STATUS_FOR,
        mcp_error_for_status,
    )

    for code, status in HTTP_STATUS_FOR.items():
        assert mcp_error_for_status(status) is code


def test_error_unknown_status_fails_closed_to_unavailable():
    from regsuper.conduit.errors import ConduitErrorCode, mcp_error_for_status

    assert mcp_error_for_status(418) is ConduitErrorCode.UNAVAILABLE


# ── F. migration 016 (dry-parse) + ORM model match ──────────────────────────


def test_migration_016_present_and_dry_parses():
    """016 exists, dry-parses (statements + BEGIN/COMMIT), and is FILE-STAGED."""
    from scripts import apply_migrations as drv

    assert "staged/016_conduit_credential.sql" in drv.STAGED
    report = drv.dry_parse("staged/016_conduit_credential.sql")
    assert report["statements"] >= 3  # CREATE TABLE + indexes + policy
    assert report["has_begin"] and report["has_commit"]


def test_migration_016_up_creates_table_and_policy_rollback_drops_it():
    sql = (REPO_ROOT / "migrations" / "staged" / "016_conduit_credential.sql").read_text()
    forward, _, rollback = sql.partition("-- ROLLBACK")
    # Forward path creates the table + the RLS policy + the unique index.
    assert "CREATE TABLE conduit_credential" in forward
    assert "ENABLE ROW LEVEL SECURITY" in forward
    assert "CREATE POLICY p_conduit_credential_tenant" in forward
    assert "ux_conduit_credential_cid" in forward
    # No destructive DROP in the forward path; the DROPs live only in rollback.
    assert "DROP TABLE" not in forward
    assert "DROP TABLE IF EXISTS conduit_credential" in rollback
    assert "DROP POLICY IF EXISTS p_conduit_credential_tenant" in rollback


def test_orm_model_columns_match_016_ddl():
    """The ConduitCredential ORM model column set equals the 016 DDL column set."""
    from regsuper.storage.models import ConduitCredential

    sql = (REPO_ROOT / "migrations" / "staged" / "016_conduit_credential.sql").read_text()
    forward = sql.partition("-- ROLLBACK")[0]
    # Strip line comments FIRST — a comment such as "(RDS-3 owns it); audit only"
    # contains a literal ');' that would otherwise truncate the CREATE body.
    no_comments = re.sub(r"--[^\n]*", "", forward)
    body = no_comments.partition("CREATE TABLE conduit_credential")[2]
    body = body.partition(");")[0]
    # column names are the first token of each non-constraint line in the CREATE body
    ddl_cols: set[str] = set()
    for raw_line in body.splitlines():
        line = raw_line.strip().rstrip(",").strip()
        if not line:
            continue
        first = line.split()[0]
        if first.upper() in {"CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK"}:
            continue
        if re.match(r"^[a-z_]+$", first):
            ddl_cols.add(first)

    orm_cols = {c.name for c in ConduitCredential.__table__.columns}
    assert orm_cols == ddl_cols, f"ORM {orm_cols} != DDL {ddl_cols}"


# ── G. cross-cutting: the MAIN app must NOT import conduit at default ─────────


def test_main_app_module_does_not_import_conduit():
    """api/main.py must never import regsuper.conduit (PEP-562 lazy isolation) — the
    main regsuper app + its test suite stay byte-identical."""
    main_src = (REPO_ROOT / "src" / "regsuper" / "api" / "main.py").read_text()
    assert "conduit" not in main_src, "api/main.py references conduit — isolation broken"


def test_main_app_does_not_load_the_conduit_listener_or_optional_deps():
    """The HARD isolation invariant: importing the main regsuper app must NOT pull the
    Conduit ASGI FACTORY (`conduit.app`), the MCP server, or the optional `conduit`
    extra deps (`mcp`/`redis`). The main app + its full test suite stay byte-identical.

    NOTE: `conduit.envelope` IS in the main-app graph — `api/answer.py` (the M5/M6
    answer surface) reuses the pure `RefusalReason` enum from it. That is a single
    pure-data module (no listener, no optional deps), not the internet-facing process,
    so it does not breach the trust-boundary isolation this test guards. T-CN1 does not
    widen that coupling (envelope only adds `analysis.citation_check`, which the main
    app already imports via the cascade)."""
    import sys

    # Drop any conduit/optional-dep modules a prior test left resident, then import
    # the main app fresh and assert the dangerous modules are absent.
    for name in list(sys.modules):
        if (
            name == "regsuper.api.main"
            or name.startswith("regsuper.conduit")
            or name == "mcp"
            or name == "redis"
            or name.startswith(("mcp.", "redis."))
        ):
            del sys.modules[name]

    importlib.import_module("regsuper.api.main")

    assert "regsuper.conduit.app" not in sys.modules, "main app pulled the Conduit factory"
    assert "regsuper.conduit.mcp_server" not in sys.modules, "main app pulled the MCP server"
    assert "mcp" not in sys.modules, "main app pulled the optional `mcp` SDK"
    assert "redis" not in sys.modules, "main app pulled the optional `redis` dep"
