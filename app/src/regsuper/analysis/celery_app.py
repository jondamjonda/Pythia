"""The Celery app for running the analysis cascade on a background worker, eager by default.

* ``task_always_eager`` is True by default: every task runs inline in the calling process and no
  broker (Redis, RabbitMQ) is ever contacted. Real queueing needs the operator to provision a
  broker and turn on ``analyze_async``.
* The base install does not depend on Celery; it lives behind the optional ``worker`` extra
  (``pip install regsuper[worker]``). When Celery is absent this module installs a small
  in-process shim with the same eager semantics, so importing this module, and everything that
  imports ``tasks.py``, works on the base install.

A shim rather than a hard dependency: with ``analyze_async`` off the app must behave exactly as the
synchronous path, and adding Celery to the base dependencies would enlarge the install for no
runtime benefit. The shim keeps the interface present and unit-testable.

The shim and the real Celery app expose the same minimal contract that ``tasks.py`` relies on:

* ``@app.task`` (and ``@app.task(...)``) registers a task. The decorated object stays directly
  callable and also exposes ``.delay(*a, **k)`` and ``.apply(args=..., kwargs=...)``, both of
  which run the function inline in eager mode and return a result handle.
* ``app.conf.task_always_eager`` is True in both implementations by default.

To run against a real broker, set ``CELERY_TASK_ALWAYS_EAGER=0`` (and the broker URLs below) and
start a ``celery worker``; the same ``tasks.py`` then dispatches over the broker.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

# Broker and backend URLs come from the environment so a worker deployment can
# point at Redis without a code change. They are never contacted in eager mode
# (the default), so a missing or bogus value is harmless until eager mode is off.
_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "memory://")
_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "cache+memory://")

# Eager unless the operator explicitly opts into a real broker run. Any value
# other than the usual falsy spellings keeps eager mode on.
_EAGER = os.environ.get("CELERY_TASK_ALWAYS_EAGER", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
    "",
)


# ── Eager shim, used when Celery is not installed (the base install) ─────────────
class _EagerResult:
    """A minimal stand-in for a Celery ``AsyncResult`` in eager mode.

    The work has already run inline, so the value is available immediately.
    """

    __slots__ = ("_error", "_value")

    def __init__(self, value: Any = None, error: BaseException | None = None) -> None:
        self._value = value
        self._error = error

    def get(self, *_a: Any, **_k: Any) -> Any:
        if self._error is not None:
            raise self._error
        return self._value

    @property
    def result(self) -> Any:
        return self._value if self._error is None else self._error

    def ready(self) -> bool:
        return True

    def successful(self) -> bool:
        return self._error is None


class _EagerTask:
    """Wrap a plain function so it is callable directly and via ``.delay`` / ``.apply``.

    This is exactly the surface ``tasks.py`` uses. Always runs inline. Async
    callables dispatched through ``.delay`` or ``.apply`` run on a fresh event loop
    (so a sync caller can fire and collect), while a direct ``await task(...)`` stays
    a normal coroutine. This mirrors Celery's eager behaviour closely enough for the
    inline path.
    """

    def __init__(self, fn: Callable[..., Any], name: str | None = None) -> None:
        self._fn = fn
        self.name = name or getattr(fn, "__name__", "task")
        # Preserve identity/metadata so the wrapped object looks like the function.
        self.__name__ = getattr(fn, "__name__", self.name)
        self.__doc__ = getattr(fn, "__doc__", None)
        self.__wrapped__ = fn

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # Direct call: return whatever the function returns (a coroutine for an
        # async function). The caller decides how to run it.
        return self._fn(*args, **kwargs)

    def _run_inline(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> _EagerResult:
        import asyncio
        import inspect

        try:
            if inspect.iscoroutinefunction(self._fn):
                value = asyncio.run(self._fn(*args, **kwargs))
            else:
                value = self._fn(*args, **kwargs)
            return _EagerResult(value=value)
        except BaseException as exc:  # surfaced on .get(), like Celery's eager mode
            return _EagerResult(error=exc)

    def delay(self, *args: Any, **kwargs: Any) -> _EagerResult:
        return self._run_inline(args, kwargs)

    def apply(
        self,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        **_options: Any,
    ) -> _EagerResult:
        return self._run_inline(tuple(args or ()), dict(kwargs or {}))

    # Celery names the synchronous variant ``apply``; ``apply_async`` is the
    # broker dispatch. In eager mode both run inline; provide it for parity.
    apply_async = apply


class _EagerConf(dict):
    """Attribute-access config dict mirroring ``celery.app.conf``.

    Covers the keys that ``tasks.py`` and the tests read.
    """

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value

    def update(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        dict.update(self, *args, **kwargs)


class _EagerCeleryShim:
    """A minimal Celery-app stand-in used when Celery is not installed.

    Registers eager tasks and never touches a broker.
    """

    def __init__(self, main: str) -> None:
        self.main = main
        self.conf = _EagerConf(
            task_always_eager=True,
            task_eager_propagates=True,
            broker_url=_BROKER_URL,
            result_backend=_RESULT_BACKEND,
        )
        self._tasks: dict[str, _EagerTask] = {}

    def task(self, *dargs: Any, **dkwargs: Any) -> Any:
        """Register a task; supports both ``@app.task`` and ``@app.task(name=..., ...)``."""

        def _register(fn: Callable[..., Any]) -> _EagerTask:
            name = dkwargs.get("name") or f"{self.main}.{getattr(fn, '__name__', 'task')}"
            t = _EagerTask(fn, name=name)
            self._tasks[name] = t
            return t

        # Bare @app.task usage: dargs == (fn,)
        if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
            return _register(dargs[0])
        return _register


def _build_app() -> Any:
    """Return a real Celery app when Celery is installed, else the eager shim.

    The real app is also eager by default, so importing this module never connects
    to a broker on either install. ``task_always_eager`` only becomes False when
    the operator explicitly sets ``CELERY_TASK_ALWAYS_EAGER=0`` for a broker run.
    """
    try:
        from celery import Celery  # type: ignore
    except Exception:
        return _EagerCeleryShim("regsuper")

    celery_app = Celery(
        "regsuper",
        broker=_BROKER_URL,
        backend=_RESULT_BACKEND,
    )
    celery_app.conf.update(
        task_always_eager=_EAGER,
        # In eager mode, let task exceptions propagate to the caller (matches the
        # synchronous legacy path's error surface).
        task_eager_propagates=True,
        # Defensive: even with a real broker configured, never silently retry a
        # broker connection at import or inline-execution time.
        broker_connection_retry_on_startup=False,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
    )
    return celery_app


# The single app instance the task module registers against. Eager by default, so
# importing it has no broker side effects.
app = _build_app()

CELERY_AVAILABLE = type(app).__module__.startswith("celery")

__all__ = ["CELERY_AVAILABLE", "app"]
