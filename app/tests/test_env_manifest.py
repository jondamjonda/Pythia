"""AQ-06 (WS0 step 7) — boot-time env manifest (regsuper/config.write_env_manifest).

Pins: unset var ⇒ NO write (byte-identical default); set var ⇒ a redacted JSON manifest is written; secrets
never appear in it. Pure/offline (env + a settings dump + a temp dir).
"""

from __future__ import annotations

import json

from regsuper.config import _redact_settings, write_env_manifest


def test_unset_var_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("AQ_ENV_MANIFEST_DIR", raising=False)
    assert write_env_manifest(component="app") is None
    assert list(tmp_path.iterdir()) == []


def test_empty_var_writes_nothing(monkeypatch):
    monkeypatch.setenv("AQ_ENV_MANIFEST_DIR", "  ")
    assert write_env_manifest(component="app") is None


def test_set_var_writes_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("AQ_ENV_MANIFEST_DIR", str(tmp_path))
    path = write_env_manifest(component="app")
    assert path is not None
    p = tmp_path / "regsuper-app-env-manifest.json"
    assert p.exists()
    data = json.loads(p.read_text())
    assert isinstance(data, dict) and data


def test_secrets_are_redacted():
    dump = {
        "openai_api_key": "sk-live-SECRET",
        "cohere_api_key": None,
        "database_url": "postgresql+asyncpg://user:pass@localhost:5432/regsuper",
        "answer_endpoint_enabled": True,
    }
    red = _redact_settings(dump)
    assert red["openai_api_key"] == "***REDACTED***"
    assert red["cohere_api_key"] is None
    assert "pass" not in red["database_url"] and "user" not in red["database_url"]
    assert red["database_url"].endswith("/regsuper")
    assert red["answer_endpoint_enabled"] is True


def test_written_manifest_has_no_live_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("AQ_ENV_MANIFEST_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak-999")
    # get_settings is lru_cached; clear so the env override is read fresh.
    from regsuper.config import get_settings

    get_settings.cache_clear()
    write_env_manifest(component="app")
    get_settings.cache_clear()
    text = (tmp_path / "regsuper-app-env-manifest.json").read_text()
    assert "sk-should-not-leak-999" not in text
