"""WP-021 / A4-07b — process-singleton AsyncOpenAI client.

What this proves (all DB-free, no real OpenAI call):
  1. ZERO per-call `AsyncOpenAI(...)` instantiations remain anywhere under src/regsuper EXCEPT the
     singleton factory (analysis/llm_clients.py) and admin.py (Agent 6 converts that one). Every other
     call-site now goes through `get_openai()`, so one HTTP transport is reused process-wide.
  2. init_openai()/get_openai()/close_openai() behave: init is idempotent, get returns the SAME object
     init created, close drops it, and the whole cycle is clean.
  3. The LAZY bypass works when the lifespan was skipped (CI / a CLI ingest / calibration): get_openai()
     creates the singleton on first use rather than handing back None.
  4. Fail-loud: a missing OPENAI_API_KEY raises (never a half-built / None client).

Static greps + in-loop lifecycle assertions only — no Postgres, no network.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from openai import AsyncOpenAI

from regsuper.analysis import llm_clients as lc

_SRC_ROOT = Path(lc.__file__).resolve().parent.parent  # …/src/regsuper


# ---------------------------------------------------------------------------
# 1. No per-call AsyncOpenAI anywhere outside the singleton factory.
# ---------------------------------------------------------------------------
def test_zero_per_call_asyncopenai_outside_owners():
    """Grep every .py under src/regsuper for an `AsyncOpenAI(` instantiation. After A4-07b (the 11
    non-admin sites) + A4-06 (admin's replay site), the ONLY file allowed to construct one is the
    singleton factory (analysis/llm_clients.py). Any other hit means a call-site was missed and
    would open its own un-pooled client."""
    offenders: list[str] = []
    for path in _SRC_ROOT.rglob("*.py"):
        if path.name == "llm_clients.py":
            continue
        text = path.read_text()
        if "AsyncOpenAI(" in text:
            offenders.append(str(path.relative_to(_SRC_ROOT)))
    assert not offenders, (
        f"per-call AsyncOpenAI(...) still present outside llm_clients.py: {offenders}"
    )


def test_admin_replay_uses_get_openai():
    """Boundary check: A4-06 converted admin.py's live-spend replay site from a per-call
    `AsyncOpenAI(...)` to the shared `get_openai()` singleton. Assert the conversion stuck — admin.py
    constructs zero AsyncOpenAI clients and routes through get_openai()."""
    admin_src = (_SRC_ROOT / "api" / "admin.py").read_text()
    assert "AsyncOpenAI(" not in admin_src, (
        "admin.py still constructs a per-call AsyncOpenAI (A4-06 not applied)"
    )
    assert "get_openai(" in admin_src, "admin.py's replay site should route through get_openai()"


def test_factory_is_the_single_constructor():
    """The singleton module constructs exactly one AsyncOpenAI (in `_new_client`). We assert on CODE,
    not prose — the module docstring is free to *mention* the old `AsyncOpenAI(api_key=...)` pattern, so
    we strip it before counting."""
    import ast

    src = Path(lc.__file__).read_text()
    tree = ast.parse(src)
    body_no_doc = tree.body[1:] if ast.get_docstring(tree) else tree.body
    code = "\n".join(ast.unparse(node) for node in body_no_doc)
    assert code.count("AsyncOpenAI(") == 1, (
        "llm_clients.py should construct AsyncOpenAI in exactly one place (_new_client)"
    )


# ---------------------------------------------------------------------------
# 2/3/4. Lifecycle: init / get / close + lazy bypass + fail-loud.
# ---------------------------------------------------------------------------
@pytest.fixture
async def _clean_openai():
    """Each test starts and ends with no live singleton."""
    await lc.close_openai()
    yield
    await lc.close_openai()


def _patch_key(monkeypatch, key: str | None):
    monkeypatch.setattr(lc, "get_settings", lambda: SimpleNamespace(openai_api_key=key))


async def test_init_get_close_cycle(monkeypatch, _clean_openai):
    _patch_key(monkeypatch, "sk-test-dummy")
    assert lc._CLIENT is None
    await lc.init_openai()
    created = lc._CLIENT
    assert isinstance(created, AsyncOpenAI)
    # get_openai returns the SAME object init created (no second client).
    assert lc.get_openai() is created
    await lc.close_openai()
    assert lc._CLIENT is None


async def test_init_openai_idempotent(monkeypatch, _clean_openai):
    _patch_key(monkeypatch, "sk-test-dummy")
    await lc.init_openai()
    first = lc._CLIENT
    await lc.init_openai()  # no-op
    assert lc._CLIENT is first


async def test_close_openai_idempotent(monkeypatch, _clean_openai):
    _patch_key(monkeypatch, "sk-test-dummy")
    await lc.init_openai()
    await lc.close_openai()
    assert lc._CLIENT is None
    # Double close + close-before-init must not raise.
    await lc.close_openai()
    assert lc._CLIENT is None


async def test_lazy_bypass_when_lifespan_skipped(monkeypatch, _clean_openai):
    """No init_openai() (lifespan skipped). get_openai() must lazily create the singleton and return a
    live client — never None."""
    _patch_key(monkeypatch, "sk-test-dummy")
    assert lc._CLIENT is None
    client = lc.get_openai()
    assert isinstance(client, AsyncOpenAI)
    assert lc._CLIENT is client
    # Reuses the same lazily-created singleton.
    assert lc.get_openai() is client


async def test_fail_loud_when_key_missing(monkeypatch, _clean_openai):
    """Missing OPENAI_API_KEY → RuntimeError (never a None / half-built client). Same error text the
    per-call sites used, so callers asserting on it keep working."""
    _patch_key(monkeypatch, None)
    with pytest.raises(RuntimeError) as ei:
        lc.get_openai()
    assert "OPENAI_API_KEY" in str(ei.value)
    assert lc._CLIENT is None  # nothing was created


async def test_init_is_boot_safe_when_key_missing(monkeypatch, _clean_openai):
    """Boot-safe contract: the eager lifespan pre-warm is a NO-OP when no key is configured (so a
    keyless local/CI deploy still boots) — it does NOT fail-loud at boot. The fail-loud contract is
    enforced lazily by get_openai() (test_fail_loud_when_key_missing) only if an LLM call is attempted."""
    _patch_key(monkeypatch, None)
    await lc.init_openai()  # must not raise
    assert lc._CLIENT is None  # nothing pre-warmed without a key
    # get_openai() still fail-louds if the keyless client is actually demanded.
    with pytest.raises(RuntimeError):
        lc.get_openai()


# ---------------------------------------------------------------------------
# Public interface present (the names the lifespan + call-sites read).
# ---------------------------------------------------------------------------
def test_public_interface():
    assert inspect.iscoroutinefunction(lc.init_openai)
    assert inspect.iscoroutinefunction(lc.close_openai)
    assert callable(lc.get_openai)
    assert not inspect.iscoroutinefunction(lc.get_openai)  # get is sync (returns the client)
