"""Runtime state: server info file and XDG state directory.

The server info file (``server.json``) lives in
``$XDG_STATE_HOME/falconfox/`` (falling back to
``~/.local/state/falconfox/``) and records the PID and port of the running
daemon so that subsequent CLI invocations can discover it. A named instance
nests one level deeper, see ``instance_dir``.
"""

from __future__ import annotations

import json
import os
import shutil
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


INSTANCE_VARIABLE = "FALCONFOX_INSTANCE"


def instance_dir(base: Path) -> Path:
    """``base/falconfox``, or ``base/falconfox-<name>/falconfox`` when named.

    A named instance (``FALCONFOX_INSTANCE``) is a second copy of the stack on
    the same account, kept apart from the deployment by owning every directory
    FalconFox derives. It is FalconFox's own variable on purpose: the XDG
    variables would do the same job, but every agent session inherits the
    daemon's environment, and redirecting XDG there redirects every tool the
    agent runs along with it.
    """
    instance = os.environ.get(INSTANCE_VARIABLE)
    if instance:
        if not instance.replace("-", "").isalnum():
            raise ValueError(f"{INSTANCE_VARIABLE} must be letters, digits "
                             f"and dashes, not {instance!r}")
        base = base.joinpath(f"falconfox-{instance}")
    return base.joinpath("falconfox")


def state_dir() -> Path:
    """``$XDG_STATE_HOME/falconfox``, or ``~/.local/state/falconfox`` if unset."""
    base = os.environ.get("XDG_STATE_HOME")
    return instance_dir(Path(base) if base else Path.home().joinpath(".local", "state"))


def server_info_path() -> Path:
    return state_dir().joinpath(SERVER_INFO_FILENAME)


def runtime_dir() -> Path:
    """``$XDG_RUNTIME_DIR/falconfox``, falling back to the state directory.

    Only the fallback is durable, and that is the wrong property here: the run
    directory below wants to disappear on reboot, since nothing in it outlives
    the daemon that published it.
    """
    base = os.environ.get("XDG_RUNTIME_DIR")
    return instance_dir(Path(base)) if base else state_dir()


def clients_dir(pid: Optional[int] = None) -> Path:
    """Where clients write their orientation, for this daemon run.

    A directory per run, rather than one shared directory that has to be swept:
    when the daemon restarts, everything a departed client left behind stays in
    a directory nothing will read again. Staleness stops being a thing to
    manage and becomes a thing that cannot happen.

    The daemon publishes this path in server.json, which is what lets clients
    find a directory named after a process that did not exist when they were
    written.
    """
    return run_dir(pid).joinpath("clients")


def run_dir(pid: Optional[int] = None) -> Path:
    """This daemon run's directory: client registrations and help live under it."""
    return runtime_dir().joinpath(f"run-{pid or os.getpid()}")


def prepare_clients_dir() -> Path:
    """Create this run's client directory and drop every other run's.

    Removing siblings is safe because those runs are over -- a run directory is
    only ever written by clients of the daemon that published it, and that
    daemon is gone. It keeps a long-lived tmpfs from collecting one directory
    per restart.
    """
    directory = clients_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for sibling in directory.parent.parent.glob("run-*"):
        if sibling != directory.parent and sibling.is_dir():
            shutil.rmtree(sibling, ignore_errors=True)
    return directory


def log_path() -> Path:
    """Unified daemon log: structured events plus raw crash/uvicorn output."""
    return state_dir().joinpath(LOG_FILENAME)


@dataclass(frozen=True)
class ServerInfo:
    pid: int
    port: int
    started: str
    clients_dir: Optional[str] = None


def write_server_info(port: int) -> Path:
    """Write server.json with the current process's PID and the bound port."""
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory.joinpath(SERVER_INFO_FILENAME)
    info = {
        "pid": os.getpid(),
        "port": port,
        "started": datetime.now(timezone.utc).isoformat(),
        # Published rather than agreed in advance: the directory is named
        # after this process, so a client has no way to work it out alone.
        "clients_dir": str(clients_dir()),
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
        return ServerInfo(pid=data["pid"], port=data["port"],
                          started=data["started"],
                          clients_dir=data.get("clients_dir"))
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
