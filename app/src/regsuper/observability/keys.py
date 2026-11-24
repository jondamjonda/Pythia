"""API-key identity for per-key budgets.

Secrets are never stored. A key is identified as `provider:sha256(secret)[:12]`, so rotating a
secret yields a new key id and, intentionally, a fresh weekly budget line. An operator-editable
`data/api_keys.json` maps key ids to a label and provider so the UI can show friendly names. The
Pythia engine gets the fixed pseudo-key `pythia:local`, so its (zero-cost) call volume appears
on the budget grid without any secret.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from regsuper.config import get_settings

_REGISTRY = "api_keys.json"
_TRACKED_PROVIDERS = ("openai", "cohere", "anthropic")


def _secret_for(provider: str) -> str | None:
    s = get_settings()
    return {
        "openai": s.openai_api_key,
        "cohere": s.cohere_api_key,
        "anthropic": s.anthropic_api_key,
    }.get(provider)


def key_id_for(provider: str, secret: str | None) -> str:
    if not secret:
        return f"legacy:{provider}"
    return f"{provider}:{hashlib.sha256(secret.encode('utf-8')).hexdigest()[:12]}"


def active_key_id(provider: str) -> str:
    """Return the key id that new calls to `provider` count against.

    Pythia always uses the fixed local pseudo-key.
    """
    if provider == "pythia":
        return "pythia:local"
    return key_id_for(provider, _secret_for(provider))


def registry_path() -> Path:
    return get_settings().data_dir / _REGISTRY


def get_registry() -> dict[str, Any]:
    p = registry_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_registry(reg: dict[str, Any]) -> None:
    p = registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, indent=2), encoding="utf-8")


def label_for(key_id: str) -> str:
    reg = get_registry()
    if key_id in reg and reg[key_id].get("label"):
        return reg[key_id]["label"]
    if key_id.startswith("legacy:"):
        return f"{key_id.split(':', 1)[1]} — historical (pre-key-tracking)"
    if key_id == "pythia:local":
        return "Pythia sidecar (local · $0)"
    return key_id


def ensure_registered() -> dict[str, Any]:
    """Register the configured (non-legacy) keys and the Pythia pseudo-key. Idempotent.

    This makes them appear on the budget grid even before their first call.
    """
    reg = get_registry()
    changed = False
    for prov in _TRACKED_PROVIDERS:
        kid = active_key_id(prov)
        if not kid.startswith("legacy:") and kid not in reg:
            reg[kid] = {"label": f"{prov} key", "provider": prov}
            changed = True
    if "pythia:local" not in reg:
        reg["pythia:local"] = {"label": "Pythia sidecar (local)", "provider": "pythia"}
        changed = True
    if changed:
        _write_registry(reg)
    return reg


def set_label(key_id: str, label: str) -> dict[str, Any]:
    reg = get_registry()
    entry = reg.setdefault(key_id, {"provider": key_id.split(":", 1)[0]})
    entry["label"] = label
    _write_registry(reg)
    return reg
