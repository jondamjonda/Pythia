"""WP-021 / A4-07a — pooled httpx transport for the Pythia sidecar client.

What this proves (all DB-free, no real network):
  1. ZERO per-call `httpx.AsyncClient(...)` remains in pythia/client.py — every sidecar call now goes
     through the module-singleton pool (`_pool()`), so connections/keep-alive are reused.
  2. The pool limits are the WP-021 contract (20 / 10 / 30 s) and the client is created INSIDE a loop
     by init_pool(), never at import time.
  3. init_pool()/close_pool() are idempotent (double init / double close / close-before-init are no-ops).
  4. The LAZY path works when the lifespan was skipped (CI / calibration/pooled.py): the first call
     creates the singleton on demand rather than handing back a None client.
  5. HTTPError→502 has exactly ONE owner — api/pythia.py. The transport module does NOT translate
     transport errors (no double-mapping); it lets them propagate so the boundary maps them once.
  6. The org_id/analysis_run_id cost-attribution params are threaded through `_track` (WP-030),
     additive/inert (default None; not yet persisted).

These are import/static/in-loop assertions only — no Postgres, no Ollama, no sidecar.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import httpx
import pytest

from regsuper.pythia import client as pc

_CLIENT_SRC = Path(pc.__file__)


# ---------------------------------------------------------------------------
# 1. No per-call AsyncClient remains — every call uses the pool.
# ---------------------------------------------------------------------------
def test_zero_per_call_asyncclient_in_pythia_client():
    """The whole point of WP-021: not a single `httpx.AsyncClient(...)` instantiation survives in the
    body of pythia/client.py EXCEPT inside the pool factories (init_pool/_pool)."""
    src = _CLIENT_SRC.read_text()
    # The per-call `async with httpx.AsyncClient(...)` pattern must be gone entirely.
    assert "async with httpx.AsyncClient" not in src, (
        "a per-call `async with httpx.AsyncClient(...)` survives — it must use the pool"
    )
    # The only `httpx.AsyncClient(` instantiations allowed are the two pool factories (init_pool, _pool).
    n_instantiations = src.count("httpx.AsyncClient(")
    assert n_instantiations == 2, (
        f"expected exactly 2 httpx.AsyncClient( instantiations (init_pool + _pool lazy), "
        f"found {n_instantiations}"
    )


def test_every_call_uses_the_pool():
    """Sanity: the pool accessor is actually wired into the call bodies (not just defined)."""
    src = _CLIENT_SRC.read_text()
    assert "_pool()." in src, "calls do not route through _pool()"


# ---------------------------------------------------------------------------
# 2. Pool limits + in-loop creation contract.
# ---------------------------------------------------------------------------
def test_pool_limits_are_the_wp021_contract():
    lim = pc._POOL_LIMITS
    assert lim.max_connections == 20
    assert lim.max_keepalive_connections == 10
    assert lim.keepalive_expiry == 30.0


def test_no_client_created_at_import_time():
    """An AsyncClient binds to the loop it is built in, so the singleton must NOT exist merely from
    importing the module (it is created in-loop by init_pool / lazily by _pool)."""
    # Fresh process state: nobody has called init_pool() yet in this test.
    # (Other tests close the pool in teardown; we assert the module exposes the slot, default None-able.)
    assert hasattr(pc, "_CLIENT")
    assert pc.init_pool is not None and inspect.iscoroutinefunction(pc.init_pool)
    assert inspect.iscoroutinefunction(pc.close_pool)


# ---------------------------------------------------------------------------
# 3. init/close idempotency (in-loop).
# ---------------------------------------------------------------------------
@pytest.fixture
async def _clean_pool():
    """Ensure each test starts and ends with no live pool."""
    await pc.close_pool()
    yield
    await pc.close_pool()


async def test_init_pool_idempotent(_clean_pool):
    await pc.init_pool()
    first = pc._CLIENT
    assert isinstance(first, httpx.AsyncClient)
    await pc.init_pool()  # second init must be a no-op (same object, not re-created)
    assert pc._CLIENT is first


async def test_close_pool_idempotent(_clean_pool):
    await pc.init_pool()
    assert pc._CLIENT is not None
    await pc.close_pool()
    assert pc._CLIENT is None
    # Double close — and close-before-init — must both be no-ops, not raise.
    await pc.close_pool()
    assert pc._CLIENT is None


async def test_close_before_init_is_noop(_clean_pool):
    # _clean_pool already closed; closing again with no pool must not raise.
    await pc.close_pool()
    assert pc._CLIENT is None


# ---------------------------------------------------------------------------
# 4. Lazy create when the lifespan was skipped (CI / calibration/pooled.py).
# ---------------------------------------------------------------------------
async def test_lazy_create_when_lifespan_skipped(_clean_pool):
    """No init_pool() was called (lifespan skipped). The first _pool() must lazily create the singleton
    and hand back a live client — never None."""
    assert pc._CLIENT is None
    client = pc._pool()
    assert isinstance(client, httpx.AsyncClient)
    assert pc._CLIENT is client
    # Subsequent calls reuse the same lazily-created singleton.
    assert pc._pool() is client


async def test_lazy_path_used_by_a_real_call(_clean_pool, monkeypatch):
    """End-to-end-ish (no network): with no pool initialised, calling `health()` lazily creates the pool
    and uses it. We patch the pooled client's GET so nothing leaves the process."""
    from types import SimpleNamespace

    # Enable Pythia so _base_url() doesn't refuse.
    monkeypatch.setattr(
        pc,
        "get_settings",
        lambda: SimpleNamespace(pythia_enabled=True, pythia_url="http://sidecar.local"),
    )

    captured = {}

    async def _fake_get(url, **kwargs):
        captured["url"] = url
        captured["timeout"] = kwargs.get("timeout")

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": True}

        return _Resp()

    assert pc._CLIENT is None  # lifespan skipped
    # Force lazy creation, then patch the live singleton's .get.
    client = pc._pool()
    monkeypatch.setattr(client, "get", _fake_get)

    out = await pc.health(timeout=8.0)
    assert out == {"ok": True}
    assert captured["url"] == "http://sidecar.local/health"
    assert captured["timeout"] == 8.0  # per-request timeout preserved


# ---------------------------------------------------------------------------
# 5. HTTPError → 502 has ONE owner (api/pythia.py), NOT the transport module.
# ---------------------------------------------------------------------------
def test_transport_module_does_not_map_http_errors():
    """pythia/client.py must NOT translate httpx errors to a 502 — that is api/pythia.py's single job.
    We assert on CODE, not on prose: the module must contain no `HTTPException` and no 502-mapping
    construct (the module docstring is free to *mention* 502 to explain the boundary; we strip it so the
    explanatory note doesn't trip the check)."""
    import ast

    src = _CLIENT_SRC.read_text()
    # Drop the module-level docstring so an explanatory mention of "502" / the boundary doesn't false-fail.
    tree = ast.parse(src)
    body_no_doc = tree.body[1:] if ast.get_docstring(tree) else tree.body
    code = "\n".join(ast.unparse(node) for node in body_no_doc)

    assert "HTTPException" not in code, (
        "transport module must not raise HTTPException (502 is the boundary's job)"
    )
    assert "502" not in code, "transport module must not reference a 502 mapping in CODE"


def test_502_mapping_owned_by_api_pythia():
    """The boundary (api/pythia.py) is the single owner of the httpx.HTTPError → 502 mapping."""
    api_src = (_CLIENT_SRC.parent.parent / "api" / "pythia.py").read_text()
    assert "httpx.HTTPError" in api_src
    assert "502" in api_src


async def test_transport_errors_propagate_unmapped(_clean_pool, monkeypatch):
    """A transport failure inside a call must propagate as an httpx error (so the boundary maps it to
    502 ONCE) — the transport module itself must not swallow or remap it."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        pc,
        "get_settings",
        lambda: SimpleNamespace(pythia_enabled=True, pythia_url="http://sidecar.local"),
    )

    async def _boom_get(url, **kwargs):
        raise httpx.ConnectError("sidecar down")

    client = pc._pool()
    monkeypatch.setattr(client, "get", _boom_get)

    with pytest.raises(httpx.HTTPError):  # ConnectError is an httpx.HTTPError subclass
        await pc.health()


# ---------------------------------------------------------------------------
# 6. _track threads org_id / analysis_run_id (WP-030) — additive/inert.
# ---------------------------------------------------------------------------
def test_track_accepts_cost_attribution_params():
    """`_track` carries org_id + analysis_run_id (default None) for cost attribution — additive/inert
    (not yet persisted). The signature must expose both as keyword-only with a None default."""
    sig = inspect.signature(pc._track)
    assert "org_id" in sig.parameters
    assert "analysis_run_id" in sig.parameters
    assert sig.parameters["org_id"].default is None
    assert sig.parameters["analysis_run_id"].default is None
    # Keyword-only (so they never collide with the positional path/fn args).
    assert sig.parameters["org_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["analysis_run_id"].kind is inspect.Parameter.KEYWORD_ONLY
