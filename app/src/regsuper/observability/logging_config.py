"""Structured logging: structlog rendering JSON lines to a log file and to stderr.

The JSON log file doubles as the event history: `events.replay_recent` reads it back so the
frontend's log panels can show recent events after a reconnect or restart.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog

from regsuper.config import get_settings


def configure_logging() -> None:
    settings = get_settings()
    log_path: Path = settings.log_dir / "app.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Plain stdlib handlers — structlog renders the event to JSON.
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(settings.log_level)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console_handler)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, settings.log_level)),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
