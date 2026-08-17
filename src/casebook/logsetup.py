"""Central logging configuration for the FalconFox server process.

One process serves the whole UI, so logging is configured once at server
startup (see ``web.server.serve``). Everything goes through a stream handler on
stderr; where that ends up depends on the mode:

  - the daemon has no terminal, so the parent redirects its stdout/stderr into a
    single ``falconfox.log`` (see ``state.log_path``) — the stream handler's
    output lands there alongside raw crash/uvicorn output, in one ordered file;
  - a user-run foreground instance is for development: stderr is its terminal,
    and it adds a rotating file handler only when ``FALCONFOX_LOG_PATH`` names one;
  - ``FALCONFOX_LOG_PATH`` persists a log at a fixed, findable path (the daemon's
    redirect target, or a foreground file handler).

Call sites fetch a child logger with ``get_logger("coordinator")`` so
each line carries its origin.

Conventions (keep the log useful for debugging a user's report without drowning
it in noise):

  - Get a module logger once at import: ``log = get_logger("<component>")``
    (e.g. ``"server"``, ``"config"``, ``"engine.session"``). The coordinator is
    global: ``get_logger("coordinator")``.
  - **INFO** — the lifecycle/audit trail worth seeing by default: process and
    coordinator start/stop, a connection opening/closing, a session being
    created/removed, and a user action's outcome. One line per meaningful event.
  - **DEBUG** — the detail you only want when reproducing a bug: the exact
    command spawned, a filesystem write, per-request tracing, streamed chunks,
    and the full traceback of an error that was *also* surfaced to the user.
  - **WARNING** — a handled problem the user can't see but a maintainer should:
    a corrupt cache/config file, a rejected connection, a skipped bad record.
  - **ERROR / .exception()** — an unexpected failure with no other surfacing.
  - The golden rule: **never swallow an exception silently.** Every ``except``
    that recovers must leave a line — WARNING/ERROR if it's the only trace, or
    ``log.debug(..., exc_info=True)`` when a user-facing notice already carries
    the summary and the traceback is just for ``FALCONFOX_LOG_LEVEL=DEBUG``.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

LOGGER_NAME = "falconfox"

# Rotate at ~5 MiB, keeping a few generations. Enough to cover a long working
# session without growing unbounded.
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_logger(name: str = "") -> logging.Logger:
    """The FalconFox logger, or a named child of it (``falconfox.<name>``)."""
    root = logging.getLogger(LOGGER_NAME)
    return root.getChild(name) if name else root


def configure(
    log_file: Optional[Path], level: str = "INFO", *, console: bool = True
) -> Optional[Path]:
    """Attach a rotating file handler (when ``log_file`` is given) and/or console.

    ``log_file`` may be None (foreground instances log to the console only).
    Idempotent: repeated calls only adjust the level and leave the existing
    handlers in place, so importing the app in tests can't stack handlers.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(_resolve_level(level))
    logger.propagate = False  # our own handlers; don't double-log via the root
    if logger.handlers:
        return log_file

    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    return log_file


def _resolve_level(level: str) -> int:
    """Map a level name (e.g. ``"debug"``) to its numeric value, INFO on miss."""
    resolved = getattr(logging, str(level).upper(), None)
    return resolved if isinstance(resolved, int) else logging.INFO
