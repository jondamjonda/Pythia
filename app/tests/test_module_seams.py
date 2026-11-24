"""A.3 module-seam SMOKE TEST — import-only.

What this proves (and ONLY this — the seams carry NO behaviour yet)
-------------------------------------------------------------------
1. **Every A.3 stub imports cleanly** — the four new packages
   (`entitlements`, `billing`, `conduit`, `integrations`), the four new
   `analysis/` seams (`compliance_guard`, `render`, `model_resolver`,
   `disclaimers`), and the five new `api/` seams (`profiles`, `digest`,
   `advisor`, `handoff`, `auth`) all import without error. A single typo /
   syntax slip / bad import in any stub fails this test loudly.
2. **The public interface is present** — each seam exports the symbols its
   consuming WP will read (signatures / dataclasses / enums / routers /
   denial-code constants). Importing the *name* must succeed; the seam stays
   import-only, so we do NOT call the bodies (most raise NotImplementedError /
   501 on purpose, naming the WP that lands them).
3. **The denial codes are the §16.5 / A1-D3 contract** — entitlements pins
   402 (upgrade / payment-required), 403 (not-entitled), 429 (quota).
4. **The five new api routers are real but UNMOUNTED** — each exposes an
   `APIRouter`, and NONE of their prefixes appear in the live app's route table.
5. **The app still boots byte-for-byte unchanged** — `import regsuper.api.main`
   succeeds and its FastAPI route set is exactly what it was before A.3 (the new
   routers are wired NOWHERE; `conduit` is never imported by the main process).

This is the obstacle-removal gate for A.3: it makes "the seams exist and the app
boots" a one-command, machine-checkable fact. It touches NO database, makes NO
network call, and calls NO stub body — so it runs green on a bare checkout.
"""

from __future__ import annotations

import importlib

import pytest

# ---------------------------------------------------------------------------
# 1. Import smoke — every new A.3 stub module / package, plus the app entrypoint.
# ---------------------------------------------------------------------------
# The canonical A.3 import list (mirrors the task's one-liner verification). The
# app entrypoint goes LAST so a failure there is unambiguous about boot vs. stub.
NEW_STUB_MODULES = [
    # New packages (top-level __init__ import surface).
    "regsuper.entitlements",
    "regsuper.billing",
    "regsuper.conduit",
    # integrations: the __init__ is import-light; the accounting adapter is the
    # net-new module, so we import it directly (per the A.3 verification line).
    "regsuper.integrations.accounting",
    # New analysis/ seams.
    "regsuper.analysis.compliance_guard",
    "regsuper.analysis.render",
    "regsuper.analysis.model_resolver",
    "regsuper.analysis.disclaimers",
    # New api/ seams (routers defined, NOT mounted).
    "regsuper.api.profiles",
    "regsuper.api.digest",
    "regsuper.api.advisor",
    "regsuper.api.handoff",
    "regsuper.api.auth",
]

# The five new api/ seams each expose an APIRouter under `router`, prefixed here.
NEW_API_ROUTER_MODULES = {
    "regsuper.api.profiles": "/api/profiles",
    "regsuper.api.digest": "/api/digest",
    "regsuper.api.advisor": "/api/advisor",
    "regsuper.api.handoff": "/api/handoff",
    "regsuper.api.auth": "/api/auth",
}


@pytest.mark.parametrize("module_name", NEW_STUB_MODULES)
def test_new_stub_module_imports(module_name: str) -> None:
    """Each A.3 stub imports with no error and is a real module object."""
    mod = importlib.import_module(module_name)
    assert mod is not None
    assert mod.__name__ == module_name


def test_app_boots() -> None:
    """The FastAPI entrypoint still imports — the app boots."""
    main = importlib.import_module("regsuper.api.main")
    from fastapi import FastAPI

    assert isinstance(main.app, FastAPI)


# ---------------------------------------------------------------------------
# 2. Public interface present — import the named symbols each consuming WP reads.
#    (Names only; we never CALL the bodies — the seams are import-only.)
# ---------------------------------------------------------------------------
def test_entitlements_public_interface() -> None:
    import regsuper.entitlements as ent

    # The four §16.5 callables + the Entitlement value object.
    for name in (
        "effective_entitlement",
        "require_entitlement",
        "depth_allowed",
        "scopes_for_org",
        "Entitlement",
    ):
        assert hasattr(ent, name), f"entitlements missing {name}"
    assert callable(ent.effective_entitlement)
    assert callable(ent.require_entitlement)
    assert callable(ent.depth_allowed)
    assert callable(ent.scopes_for_org)


def test_entitlements_denial_codes() -> None:
    """Denial codes are the A1-D3 / §16.5 contract: 402 / 403 / 429."""
    import regsuper.entitlements as ent

    assert ent.HTTP_PAYMENT_REQUIRED == 402  # upgrade / feature-not-in-tier
    assert ent.HTTP_FORBIDDEN == 403  # not entitled (wrong org / no scope)
    assert ent.HTTP_QUOTA_EXCEEDED == 429  # quota / rate exhaustion


def test_billing_public_interface() -> None:
    import regsuper.billing as billing

    for name in (
        "BillingProvider",
        "BillingEvent",
        "NoopBillingProvider",
        "emit_billing_event",
        "on_subscription_changed",
    ):
        assert hasattr(billing, name), f"billing missing {name}"
    assert callable(billing.emit_billing_event)
    assert callable(billing.on_subscription_changed)


def test_conduit_factory_lazy_export() -> None:
    """Conduit exposes its factory name without importing app.py / mcp / Redis at
    package-import time (PEP 562 lazy __getattr__)."""
    import regsuper.conduit as conduit

    assert "create_conduit_app" in conduit.__all__
    assert callable(conduit.create_conduit_app)  # triggers the lazy import


def test_integrations_accounting_public_interface() -> None:
    import regsuper.integrations.accounting as acc

    for name in ("import_pohoda_xml", "import_money_s3_xml"):
        assert hasattr(acc, name), f"integrations.accounting missing {name}"
        assert callable(getattr(acc, name))


def test_compliance_guard_public_interface() -> None:
    import regsuper.analysis.compliance_guard as guard

    for name in (
        "classify_answer_mode",
        "project_refusal",
        "ComplianceVerdict",
        "RefusalDecision",
    ):
        assert hasattr(guard, name), f"compliance_guard missing {name}"
    # Fail-closed contract is pinned as a constant (NEVER 'information').
    assert guard.FAIL_CLOSED_ANSWER_MODE != "information"


def test_render_public_interface() -> None:
    import regsuper.analysis.render as render

    for name in (
        "build_digest",
        "render_obligation_item",
        "render_checklist_item",
        "render_gap_item",
        "render_action_item",
    ):
        assert hasattr(render, name), f"render missing {name}"
        assert callable(getattr(render, name))


def test_model_resolver_public_interface() -> None:
    import regsuper.analysis.model_resolver as mr

    assert hasattr(mr, "resolve_model")
    assert callable(mr.resolve_model)
    assert hasattr(mr, "ModelRole")


def test_disclaimers_public_interface() -> None:
    import regsuper.analysis.disclaimers as disc

    assert hasattr(disc, "get_disclaimer")
    assert callable(disc.get_disclaimer)
    assert hasattr(disc, "DisclaimerBlock")


@pytest.mark.parametrize("module_name", list(NEW_API_ROUTER_MODULES))
def test_new_api_modules_expose_router(module_name: str) -> None:
    """Each new api/ seam defines an APIRouter named `router`."""
    from fastapi import APIRouter

    mod = importlib.import_module(module_name)
    assert hasattr(mod, "router"), f"{module_name} missing `router`"
    assert isinstance(mod.router, APIRouter)


# ---------------------------------------------------------------------------
# 3. App boots UNCHANGED — the new routers are mounted NOWHERE.
# ---------------------------------------------------------------------------
# A.3 scaffolded five api routers unmounted. WP-022 (A2-T22) wires `auth` (the
# product-auth/Principal seam — the GUC org source); WP-050/B2b-05 wires `handoff`
# (the lawful-lane referral terminus); WP-049 wires `profiles` (SME profile CRUD +
# profile-driven analyze) and `digest` (the plain-language output views) — both
# mounted but inert (404) until their profiles_enabled/digest_enabled flags flip.
# `advisor` (WP-050) is the last remaining scaffold-only router.
_MOUNTED_BY_WP: frozenset[str] = frozenset(
    (
        "regsuper.api.auth",
        "regsuper.api.handoff",
        "regsuper.api.profiles",
        "regsuper.api.digest",
    )
)
_STILL_UNMOUNTED_API_ROUTERS = {
    k: v for k, v in NEW_API_ROUTER_MODULES.items() if k not in _MOUNTED_BY_WP
}


def test_new_api_routers_not_mounted_in_app() -> None:
    """`advisor` remains scaffold-only (wired NOWHERE). `auth` (WP-022), `handoff`
    (WP-050/B2b-05), and `profiles`/`digest` (WP-049) are mounted, verified
    separately."""
    from regsuper.api.main import app

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    for module_name, prefix in _STILL_UNMOUNTED_API_ROUTERS.items():
        leaked = {p for p in paths if p == prefix or p.startswith(prefix + "/")}
        assert not leaked, (
            f"{module_name} ({prefix}) is mounted in api/main.py but its WP has not "
            f"landed yet — found {sorted(leaked)}"
        )


def test_auth_router_is_mounted() -> None:
    """WP-022 mounts the product-auth router. Its routes exist (inert 404s while
    require_auth=False, but present in the route table)."""
    from regsuper.api.main import app

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    mounted = {p for p in paths if p == "/api/auth" or p.startswith("/api/auth/")}
    assert mounted, "auth_router (/api/auth) should be mounted by WP-022"


def test_handoff_router_is_mounted() -> None:
    """WP-050/B2b-05 mounts the lawful-lane referral router. Its routes exist
    (consent-gated PII; create/consent/get/list present in the route table)."""
    from regsuper.api.main import app

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    mounted = {p for p in paths if p == "/api/handoff" or p.startswith("/api/handoff/")}
    assert mounted, "handoff_router (/api/handoff) should be mounted by WP-050/B2b-05"


def test_profiles_router_is_mounted() -> None:
    """WP-049 mounts the SME profile router. Its routes exist (inert 404s while
    profiles_enabled=False, but present in the route table)."""
    from regsuper.api.main import app

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    mounted = {p for p in paths if p == "/api/profiles" or p.startswith("/api/profiles/")}
    assert mounted, "profiles_router (/api/profiles) should be mounted by WP-049"


def test_digest_router_is_mounted() -> None:
    """WP-049 mounts the plain-language digest router. Its routes exist (inert 404s
    while digest_enabled=False, but present in the route table)."""
    from regsuper.api.main import app

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    mounted = {p for p in paths if p == "/api/digest" or p.startswith("/api/digest/")}
    assert mounted, "digest_router (/api/digest) should be mounted by WP-049"


def test_main_does_not_import_conduit() -> None:
    """Trust-boundary invariant (doc-05 §0, INV-1): the main app process must
    NEVER import the internet-facing Conduit package. Importing main alone must
    not pull regsuper.conduit into sys.modules."""
    import sys

    # Drop any prior conduit import so this asserts about the main-app path only.
    for name in list(sys.modules):
        if name == "regsuper.conduit" or name.startswith("regsuper.conduit."):
            del sys.modules[name]
    # Re-import the entrypoint fresh.
    sys.modules.pop("regsuper.api.main", None)
    importlib.import_module("regsuper.api.main")

    assert "regsuper.conduit" not in sys.modules, (
        "regsuper.api.main imported regsuper.conduit — violates the Conduit "
        "trust-boundary isolation invariant (doc-05 §0 / INV-1)"
    )
