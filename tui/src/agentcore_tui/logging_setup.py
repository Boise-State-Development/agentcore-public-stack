"""Logging setup.

A full-screen TUI owns stdout and stderr, so anything written there corrupts the
display. All diagnostics therefore go to a rotating file.

Defaults:

* Path — ``<user log dir>/agentcore-tui.log``, overridable with
  ``AGENTCORE_LOG_FILE`` or ``--log-file``.
* Level — ``INFO``, overridable with ``AGENTCORE_LOG_LEVEL`` or ``--log-level``.
* Rotation — 1 MB × 3 backups, so a long session cannot fill a disk.

Privacy: prompts and model output are **not** logged unless
``AGENTCORE_LOG_CONTENT=1``. Lengths and counts are always safe to log; the text
itself may be sensitive. The API key is never logged at any level — use
:func:`redact` for anything that might contain one.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from platformdirs import user_log_path

from .config import APP_NAME

ENV_LOG_LEVEL = "AGENTCORE_LOG_LEVEL"
ENV_LOG_FILE = "AGENTCORE_LOG_FILE"
ENV_LOG_CONTENT = "AGENTCORE_LOG_CONTENT"

DEFAULT_LEVEL = "INFO"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 3

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-34s %(message)s"

#: Set once by :func:`configure_logging` so callers can report the active path.
_active_path: Path | None = None


def default_log_path() -> Path:
    """Platform-correct log file location."""
    return user_log_path(APP_NAME, appauthor=False, ensure_exists=False) / "agentcore-tui.log"


def log_path(override: Path | str | None = None) -> Path:
    """Resolve the log path from an override, the environment, or the default."""
    if override:
        return Path(override)
    from_env = os.environ.get(ENV_LOG_FILE)
    if from_env:
        return Path(from_env)
    return default_log_path()


def active_log_path() -> Path | None:
    """The path logging was configured with, or None if not configured."""
    return _active_path


def content_logging_enabled() -> bool:
    """True when prompt/response text may be written to the log."""
    return os.environ.get(ENV_LOG_CONTENT, "").strip().lower() in {"1", "true", "yes"}


def redact(text: str) -> str:
    """Return text safe to log: content when opted in, a length summary otherwise."""
    if content_logging_enabled():
        return text
    return f"<{len(text)} chars redacted; set {ENV_LOG_CONTENT}=1 to log content>"


def configure_logging(*, level: str | None = None, path: Path | str | None = None) -> Path:
    """Attach a rotating file handler to this package's logger.

    Returns the path being written to. Safe to call more than once — handlers are
    replaced rather than stacked, so repeated calls do not duplicate lines.
    """
    global _active_path

    resolved_level = (level or os.environ.get(ENV_LOG_LEVEL) or DEFAULT_LEVEL).upper()
    numeric_level = getattr(logging, resolved_level, logging.INFO)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO

    target = log_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    package_logger = logging.getLogger("agentcore_tui")
    for handler in list(package_logger.handlers):
        package_logger.removeHandler(handler)
        handler.close()

    file_handler = RotatingFileHandler(target, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    file_handler.setLevel(numeric_level)

    package_logger.addHandler(file_handler)
    package_logger.setLevel(numeric_level)
    # Never let records escape to a stdout/stderr handler — that would paint
    # log lines over the running UI.
    package_logger.propagate = False

    _active_path = target
    package_logger.info(
        "logging configured level=%s file=%s content_logging=%s",
        resolved_level,
        target,
        content_logging_enabled(),
    )
    return target
