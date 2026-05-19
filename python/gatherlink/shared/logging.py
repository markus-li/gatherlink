"""
Uniform logging support for Gatherlink.

All Python modules should import ``get_logger`` from this module instead of
constructing loggers directly. This keeps formatting, module names, JSON/file
redirection, and future diagnostics integration consistent.
"""

from __future__ import annotations

import logging
import sys
from typing import Final

DEFAULT_FORMAT: Final[str] = "%(asctime)s %(levelname)-8s %(name)s " "%(message)s"


def configure_logging(
    *,
    level: int | str = logging.INFO,
    log_file: str | None = None,
    force: bool = False,
) -> None:
    """
    Configure Gatherlink logging.

    Args:
        level: Logging level name or numeric value.
        log_file: Optional file path. When omitted, logs go to stderr.
        force: Replace existing handlers when true.

    """
    handlers: list[logging.Handler] = []

    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    else:
        handlers.append(logging.StreamHandler(sys.stderr))

    logging.basicConfig(
        level=level,
        format=DEFAULT_FORMAT,
        handlers=handlers,
        force=force,
    )


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger."""
    return logging.getLogger(name)


# TODO(logging-diagnostics):
# - Add optional JSON formatter for machine-readable logs.
# - Add log redaction helpers for secrets, tokens, endpoints, and keys.
# - Support runtime log-level reload from CLI/API.
