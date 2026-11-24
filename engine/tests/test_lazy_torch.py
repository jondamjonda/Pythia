"""Wave-3 efficiency guards — torch/sentence-transformers MUST stay out of the sidecar import path
(LEDGER eng-service-04), the Ollama keep_alive memory knob is wired, and the asyncpg pool sizing is
config-driven. None of these change engine OUTPUT; they bound the memory footprint / object lifetime.

The import-laziness check runs in a FRESH subprocess so the assertion holds regardless of test ordering
(another test in the same process may already have imported torch). It proves that merely importing the
sidecar — what `uvicorn pythia.service:app` does at boot — does NOT pull in the ~GB torch stack: the
bge-m3 encoder + cross-encoder load LAZILY on first use (the default cte /v1/retrieve path still uses
them at RUNTIME — that is unchanged; what is asserted here is only that they are not loaded at IMPORT).
"""

from __future__ import annotations

import subprocess
import sys

from pythia.config import get_settings
from pythia.db import _dsn  # noqa: F401 (kept importable; sanity that db imports without a DB)
from pythia.llm import OllamaLLMAdapter

# A tiny program run in a clean interpreter: import the sidecar (+ engine + the NexusPort door + answer
# pipeline), construct the engine, and report whether torch / sentence_transformers got imported. Nothing
# here CALLS a model() loader, so a green run proves import-time laziness — not that retrieval is model-free.
_PROBE = (
    "import sys;"
    "import pythia.service;"  # the uvicorn entrypoint module
    "from pythia.engine import PythiaEngine;"
    "from pythia.ports import NexusPort;"
    "import pythia.reasoning.answer;"  # the /v1/answer pipeline
    "PythiaEngine(); NexusPort();"  # construction must not warm any torch model
    "print('TORCH' if 'torch' in sys.modules else 'NO_TORCH');"
    "print('ST' if 'sentence_transformers' in sys.modules else 'NO_ST')"
)


def test_sidecar_import_does_not_load_torch() -> None:
    """Importing + constructing the sidecar/engine must NOT import torch or sentence_transformers
    (eng-service-04 — keep the heavy stack off the boot/import path; it loads only on first model use)."""
    r = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"probe crashed:\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    out = r.stdout.split()
    assert "NO_TORCH" in out, f"torch was imported at sidecar import time:\n{r.stdout}\n{r.stderr}"
    assert "NO_ST" in out, f"sentence_transformers imported at import time:\n{r.stdout}\n{r.stderr}"


def test_ollama_keep_alive_default_is_memory_friendly() -> None:
    """The keep_alive knob defaults to a short, finite resident window (not Ollama's lingering default,
    and not the keep-forever '-1') so the heavy 14B model unloads soon after an answer turn."""
    a = OllamaLLMAdapter()
    assert a.keep_alive == get_settings().ollama_keep_alive == "30s"
    assert a.keep_alive not in ("-1", -1), "default must not pin the model in memory forever"


def test_ollama_keep_alive_in_chat_payload_not_options() -> None:
    """keep_alive rides as a TOP-LEVEL /api/chat field (a lifetime control), never inside `options`
    (which would make it look like a generation parameter). We assert the payload SHAPE without any
    network call by monkeypatching httpx — proving the field is sent and that options stays output-only
    (temperature+seed), so generation is byte-identical to pre-Wave-3."""
    import asyncio

    import pythia.llm as llm_mod

    captured: dict = {}

    class _FakeResp:
        def raise_for_status(self):  # noqa: D401
            return None

        def json(self):
            return {"message": {"content": "ok"}}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):  # noqa: A002 — match httpx signature
            captured["url"] = url
            captured["json"] = json
            return _FakeResp()

    orig = llm_mod.httpx.AsyncClient
    llm_mod.httpx.AsyncClient = _FakeClient
    try:
        a = OllamaLLMAdapter(keep_alive="7s")
        asyncio.run(a.text("hi"))
    finally:
        llm_mod.httpx.AsyncClient = orig

    body = captured["json"]
    assert body["keep_alive"] == "7s"  # top-level lifetime field is sent
    assert "keep_alive" not in body["options"]  # NOT a generation option
    assert set(body["options"]) == {"temperature", "seed"}  # options stays output-only → tokens unchanged


def test_pool_size_config_knobs_default_to_legacy_literals() -> None:
    """The asyncpg pool sizing is config-driven but defaults to the pre-Wave-3 literals (min 1 / max 5),
    so committing the knob is byte-identical to today. (We assert the config, not a live pool — no DB.)"""
    s = get_settings()
    assert s.db_pool_min_size == 1
    assert s.db_pool_max_size == 5
    assert s.db_pool_min_size >= 1, "the min floor keeps acquire() (and the live-DB tests) working"
