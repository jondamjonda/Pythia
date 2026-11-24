"""C3 (T-C3a / AS-1) — the eager Celery app imports + runs with NO broker.

These run in CI WITHOUT Redis/Celery (the base install). They prove:
  * `regsuper.analysis.celery_app` imports cleanly with no broker contacted;
  * `task_always_eager` is True by default (the dark inline path);
  * `tasks.py` imports against it without a broker;
  * a task runs EAGER (inline) and returns its result with no Redis;
  * an eager task surfaces the wrapped function's exception via .get();
  * importing the module never establishes a broker connection.
"""

from __future__ import annotations

import asyncio

import pytest


def test_celery_app_imports_without_broker():
    """Importing the app module is side-effect-free w.r.t. any broker."""
    from regsuper.analysis import celery_app

    assert celery_app.app is not None
    # Eager-by-default — no broker is ever contacted on import.
    assert celery_app.app.conf.task_always_eager is True


def test_tasks_import_without_broker():
    """tasks.py registers its task against the eager app with no broker."""
    from regsuper.analysis import tasks

    assert tasks.run_analysis_task is not None
    assert callable(tasks.run_analysis_job)


def test_task_runs_eager_inline_no_redis():
    """An eager task runs INLINE and returns the wrapped result — no Redis."""
    from regsuper.analysis.celery_app import app

    @app.task(name="test.echo_eager")
    def _echo(x):
        return ("eager", x)

    # Dispatch via the eager surface tasks.py uses (.apply / .delay) — both inline.
    res_apply = _echo.apply(args=(42,))
    assert res_apply.get() == ("eager", 42)
    res_delay = _echo.delay(7)
    assert res_delay.get() == ("eager", 7)
    # Direct call still works (the decorated object stays callable).
    assert _echo(99) == ("eager", 99)


def test_eager_async_task_runs_on_fresh_loop():
    """An async-function task is run to completion on a fresh loop in eager mode
    (the bridge tasks.run_analysis_task relies on)."""
    from regsuper.analysis.celery_app import app

    @app.task(name="test.async_eager")
    async def _afn(a, b):
        await asyncio.sleep(0)
        return a + b

    res = _afn.apply(args=(2, 3))
    assert res.get() == 5


def test_eager_task_exception_surfaces_via_get():
    """An exception raised by the wrapped function in eager mode is captured and
    re-raised on .get() (matches Celery's eager-propagation surface)."""
    from regsuper.analysis.celery_app import app

    @app.task(name="test.boom")
    def _boom():
        raise ValueError("kaboom")

    res = _boom.apply()
    with pytest.raises(ValueError, match="kaboom"):
        res.get()


def test_app_is_eager_regardless_of_celery_presence():
    """Whether Celery is installed (real app) or not (shim), the contract holds:
    eager default + a usable @task decorator that yields a directly-callable task."""
    from regsuper.analysis import celery_app

    @celery_app.app.task
    def _bare(x):
        return x * 2

    assert _bare(10) == 20
    assert _bare.apply(args=(10,)).get() == 20


def test_run_analysis_task_runs_body_inline_for_sync_caller():
    """AS-2 worker seam: the registered run_analysis_task bridges the async body to a
    SYNCHRONOUS caller (no running loop, like a Celery worker) via asyncio.run, with
    no broker. Patch the body so this is DB-free."""
    from unittest.mock import patch

    from regsuper.analysis import tasks

    captured = {"job": None}

    async def fake_body(job):
        captured["job"] = job
        return ("ran", job)

    with patch.object(tasks, "run_analysis_job", fake_body):
        # apply() = eager inline dispatch; we are in a plain sync test (no running loop),
        # so the task's asyncio.run bridge works exactly as it would on a worker.
        result = tasks.run_analysis_task.apply(args=("JOB",))
        assert result.get() == ("ran", "JOB")
    assert captured["job"] == "JOB"
