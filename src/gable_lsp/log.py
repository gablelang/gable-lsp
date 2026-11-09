"""Logging for gable-lsp.

Stdio transport servers must never write protocol bytes to stdout, so all
logs go to stderr (or a file with ``--log-file``). One line per event, with
an ISO-8601 timestamp and level.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

_CONFIGURED = False


def configure_logging(level: str = "warning", log_file: Optional[str] = None) -> None:
    global _CONFIGURED
    numeric = getattr(logging, level.upper(), logging.WARNING)
    handler: logging.Handler
    if log_file:
        handler = logging.FileHandler(log_file, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger("gable_lsp")
    root.handlers[:] = [handler]
    root.setLevel(numeric)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)
