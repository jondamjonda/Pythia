"""Async engine and sessions for the app database, with per-request tenant scoping.

Sessions handed out by ``session_scope()`` and ``get_db()`` stamp the active org into
transaction-local Postgres settings that the row-level-security policies read. This happens
only when ``rls_enforced`` is on; before the first enforced session the process also verifies
that enforcement would actually isolate tenants (see ``RlsSafetyError``).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from regsuper.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# ---------------------------------------------------------------------------
# Tenancy context
#
# These context variables carry the active org for the current request or task, so the session
# setup below can stamp the Postgres row-level-security settings. The tenancy middleware and
# auth layer set them (current_org_var.set(str(org)), is_platform_admin_var.set(...)); this
# module reads them.
#
# The settings are issued only when settings.rls_enforced is True. With the default (False) the
# variables are ignored and no extra statement is executed.
# ---------------------------------------------------------------------------
current_org_var: ContextVar[str | None] = ContextVar("current_org", default=None)
is_platform_admin_var: ContextVar[bool] = ContextVar("is_platform_admin", default=False)


class RlsSafetyError(RuntimeError):
    """Enforcing row-level security is unsafe under the current configuration.

    Raised when ``rls_enforced=True`` would make RLS either ineffective or a total lockout.
    Two configurations are refused loudly rather than failing silently:

    * The connection role bypasses RLS: it is a superuser, has the BYPASSRLS attribute, or owns
      the tables without ``FORCE ROW LEVEL SECURITY``. Postgres exempts such a role from every
      policy, so enforcing RLS would isolate nothing and tenants could read each other's rows.
    * ``rls_enforced=True`` while ``tenancy_enabled=False``. The org context variable is then
      never set (``TenancyMiddleware`` does nothing), so the RLS settings are never issued and
      every policy denies, locking out all reads.
    """


# Set once the RLS safety checks have passed. The checks run at most once per process, and only
# after rls_enforced has been confirmed True (see _apply_org_guc), so the cheap database probe
# is a one-off cost rather than a per-session one.
_rls_safety_checked: bool = False


def _assert_tenancy_invariant() -> None:
    """Refuse ``rls_enforced`` without ``tenancy_enabled`` (a configuration-only check).

    With RLS enforced but tenancy off, the org context variable is never set because
    ``TenancyMiddleware`` does nothing, so the RLS settings are never issued and every policy
    denies. Failing loudly at the first enforced session beats silently locking out every read.
    """
    s = get_settings()
    if s.rls_enforced and not s.tenancy_enabled:
        raise RlsSafetyError(
            "refusing rls_enforced=True while tenancy_enabled=False: the org RLS "
            "GUC is published only by TenancyMiddleware (tenancy_enabled=True), so "
            "enforcing RLS without tenancy locks out every read (fail-closed). "
            "Set TENANCY_ENABLED=true together with RLS_ENFORCED=true (LEDGER-M1)."
        )


async def _assert_connection_not_rls_exempt(session: AsyncSession) -> None:
    """Refuse ``rls_enforced=True`` when the connection role is exempt from RLS.

    A superuser or BYPASSRLS role is not subject to any policy, so RLS would isolate nothing
    and tenants could silently read each other's rows. The table-owner exemption is closed by
    ``FORCE ROW LEVEL SECURITY`` in migration 015, but a superuser or BYPASSRLS login bypasses
    even forced RLS, which is what this check catches.

    The app should connect as ``regsuper_app`` (NOBYPASSRLS, not the table owner).
    """
    row = (
        await session.execute(
            text(
                "SELECT current_user, rolsuper, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
        )
    ).first()
    if row is None:
        # current_user missing from pg_roles is itself anomalous under enforced RLS; refuse
        # rather than assume safety.
        raise RlsSafetyError(
            "refusing rls_enforced=True: could not resolve the current DB role "
            "from pg_roles to verify it is not RLS-exempt (LEDGER-C1)."
        )
    current_user, is_super, is_bypass = row[0], bool(row[1]), bool(row[2])
    if is_super or is_bypass:
        attr = "superuser" if is_super else "BYPASSRLS"
        raise RlsSafetyError(
            f"refusing rls_enforced=True: the app connects as DB role "
            f"{current_user!r}, which is {attr} and is therefore EXEMPT from row "
            f"level security — enforcing RLS would isolate nothing (silent "
            f"cross-tenant reads). Connect as a NOBYPASSRLS, non-owner role "
            f"(regsuper_app) before flipping RLS_ENFORCED (LEDGER-C1)."
        )


async def _check_rls_safety_once(session: AsyncSession) -> None:
    """Run the RLS safety checks once per process.

    Called from ``_apply_org_guc`` only after ``rls_enforced`` has been confirmed True. The
    configuration check runs first, so an obvious misconfiguration fails before any database
    probe; then the role check runs on the same session. On success a module flag is set so
    later enforced sessions skip both.
    """
    global _rls_safety_checked
    if _rls_safety_checked:
        return
    _assert_tenancy_invariant()
    await _assert_connection_not_rls_exempt(session)
    _rls_safety_checked = True


async def _apply_org_guc(session: AsyncSession) -> None:
    """Stamp the active org into the session's RLS settings, when ``rls_enforced`` is on.

    Does nothing when ``settings.rls_enforced`` is False (the default). When it is True and an
    org is set in the context variable, sets two transaction-scoped settings:

        SELECT set_config('app.current_org', '<org>', true)
        SELECT set_config('app.is_platform_admin', 'on'|'off', true)

    ``set_config(name, value, is_local=true)`` is used instead of ``SET LOCAL name = :v``
    because SET is a utility statement that does not accept bound parameters ("syntax error at
    or near $1"), whereas ``set_config()`` is a regular function, so the org value is passed as
    a bound parameter with no SQL-injection surface. ``is_local=true`` makes the setting
    transaction-scoped: it resets when the transaction ends, so nothing leaks across pooled
    connections. The first execute begins the transaction, ahead of any later
    ``SET LOCAL hnsw.ef_search``.
    """
    if not get_settings().rls_enforced:
        return
    # Runs at most once per process and fails loudly if enforcement would be unsafe (a
    # superuser or BYPASSRLS connection means no isolation; tenancy off means total lockout).
    await _check_rls_safety_once(session)
    org = current_org_var.get()
    if org is None:
        # With no org set, emit nothing rather than a bogus setting that would silently scope
        # to an empty or invalid org. RLS then denies because the setting is absent.
        return
    await session.execute(
        text("SELECT set_config('app.current_org', :org, true)"), {"org": str(org)}
    )
    admin = "on" if is_platform_admin_var.get() else "off"
    await session.execute(
        text("SELECT set_config('app.is_platform_admin', :adm, true)"), {"adm": admin}
    )


async def apply_ef_search(session: AsyncSession, ef_search: int) -> None:
    """Issue ``SET LOCAL hnsw.ef_search``, guaranteeing the org RLS setting is applied first.

    The org setting (``set_config('app.current_org', ..., true)``) must be the first statement
    of a transaction so the RLS predicate sees a non-NULL ``app.current_org`` on the very first
    read. If ``SET LOCAL hnsw.ef_search`` ran first on a fresh transaction, it would open the
    transaction with the org still unset. ``session_scope`` and ``get_db`` already apply the
    setting before yielding, so on the normal path this is extra safety; the helper makes the
    ordering explicit for retrieval code instead of issuing the raw SET itself.

    When ``rls_enforced`` is True, the org setting is re-applied before the ef_search SET.
    Re-applying is idempotent and cheap, and it guarantees the ordering even if the caller
    entered the transaction some other way. When ``rls_enforced`` is False, this issues only
    the ``SET LOCAL hnsw.ef_search``.

    ``ef_search`` is interpolated as an integer (at least 1), never a string, so there is no
    injection surface; SET does not accept bound parameters.
    """
    # Org setting first (idempotent; does nothing unless rls_enforced).
    await _apply_org_guc(session)
    ef = max(1, int(ef_search))
    await session.execute(text(f"SET LOCAL hnsw.ef_search = {ef}"))


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            get_settings().database_url,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
            future=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(), expire_on_commit=False, autoflush=False
        )
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        # Apply the org RLS setting before any caller statement, including the retrieval
        # layer's SET LOCAL hnsw.ef_search on this session. Does nothing unless
        # settings.rls_enforced is True (see _apply_org_guc).
        await _apply_org_guc(session)
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        # Apply the org RLS setting before the session reaches the route. Does nothing unless
        # settings.rls_enforced is True.
        await _apply_org_guc(session)
        yield session


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
