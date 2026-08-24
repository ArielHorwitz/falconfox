"""Runtime state: server info file and XDG state directory.

The server info file (``server.json``) lives in
``$XDG_STATE_HOME/falconfox/`` (falling back to
``~/.local/state/falconfox/``) and records the PID and port of the running
daemon so that subsequent CLI invocations can discover it.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dataclasses import dataclass

from . import logsetup

log = logsetup.get_logger("state")

SERVER_INFO_FILENAME = "server.json"
LOG_FILENAME = "falconfox.log"


def state_dir() -> Path:
    """``$XDG_STATE_HOME/falconfox``, or ``~/.local/state/falconfox`` if unset."""
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home().joinpath(".local", "state")
    return root.joinpath("falconfox")


def server_info_path() -> Path:
    return state_dir().joinpath(SERVER_INFO_FILENAME)


def log_path() -> Path:
    """Unified daemon log: structured events plus raw crash/uvicorn output."""
    return state_dir().joinpath(LOG_FILENAME)


@dataclass(frozen=True)
class ServerInfo:
    pid: int
    port: int
    started: str


def write_server_info(port: int) -> Path:
    """Write server.json with the current process's PID and the bound port."""
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory.joinpath(SERVER_INFO_FILENAME)
    info = {
        "pid": os.getpid(),
        "port": port,
        "started": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(info))
    return path


def read_server_info() -> Optional[ServerInfo]:
    """Read server.json, returning None if the file is missing or corrupt."""
    path = server_info_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return ServerInfo(pid=data["pid"], port=data["port"], started=data["started"])
    except (json.JSONDecodeError, KeyError, OSError) as error:
        log.debug("ignoring unreadable server info %s: %s", path, error)
        return None


def remove_server_info() -> None:
    """Remove server.json if it exists."""
    path = server_info_path()
    path.unlink(missing_ok=True)


def is_pid_alive(pid: int) -> bool:
    """Check whether a process with the given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it — still alive.
        return True


def is_port_responding(port: int, host: str = "127.0.0.1") -> bool:
    """Try connecting to a TCP port; return True if something is listening."""
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def find_running_server() -> Optional[ServerInfo]:
    """Return server info if a daemon is running, cleaning up stale state."""
    info = read_server_info()
    if info is None:
        return None
    if is_pid_alive(info.pid) and is_port_responding(info.port):
        return info
    # Stale — clean up.
    remove_server_info()
    return None


def wait_for_exit(
    pid: int,
    port: int,
    host: str = "127.0.0.1",
    timeout: float = 5.0,
    interval: float = 0.05,
) -> bool:
    """Wait until the process is gone and its port is released.

    Returns True if both happened within the timeout, False otherwise.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_pid_alive(pid) and not is_port_responding(port, host):
            return True
        time.sleep(interval)
    return False


def stop_server(wait: bool = False, timeout: float = 5.0) -> Optional[ServerInfo]:
    """Stop a running daemon. Returns the stopped server's info, or None.

    With ``wait=True``, blocks until the process has exited and released its
    port (up to ``timeout``) so a caller can immediately start a replacement.
    """
    info = read_server_info()
    if info is None:
        return None
    if is_pid_alive(info.pid):
        log.info("stopping daemon pid=%s port=%s", info.pid, info.port)
        os.kill(info.pid, signal.SIGTERM)
        if wait:
            wait_for_exit(info.pid, info.port, timeout=timeout)
    remove_server_info()
    return info


def find_available_port(host: str = "127.0.0.1", base_port: int = 9721) -> int:
    """Find an available port starting from base_port.

    The probe sets ``SO_REUSEADDR`` to match how uvicorn binds its listener, so a
    port left in ``TIME_WAIT`` by a just-stopped daemon isn't spuriously rejected
    (this lets ``--restart`` reclaim the same port).
    """
    for offset in range(100):
        port = base_port + offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((host, port))
                return port
        except OSError:
            continue
    raise RuntimeError(
        f"Could not find an available port in range {base_port}–{base_port + 99}"
    )
