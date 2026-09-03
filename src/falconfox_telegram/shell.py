"""Detached shell commands, run for the owner from the chat.

Every command runs in its own **tmux** session rather than as a child of the
bot. Three things follow from that, and they are the whole reason for it:

* a command outlives the bot, so ``/sh`` can restart the daemon, or the bot
  itself, without killing the thing that was asked to do the restarting;
* a command that hangs can be *attached to* later from a terminal
  (``tmux attach -t <session>``), which is the only way to see where something
  actually got stuck rather than guessing from truncated output;
* a runaway is killable by name without hunting a pid.

Output is teed to a file as it is produced, so the chat can show a tail while
the job is still running and the whole thing survives for later reading.
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# tmux session names cannot contain a period or a colon, and are the handle
# the user types at a terminal, so they are kept short and predictable.
SESSION_PREFIX = "ff-"
DEFAULT_TIMEOUT = 45.0
POLL_SECONDS = 0.25


@dataclass
class ShellJob:
    job_id: str
    command: str
    cwd: Path
    log_path: Path
    status_path: Path
    script_path: Path
    started: float
    session: str

    #: Set once the job has been reaped, so a finished job keeps its status
    #: after the status file is read rather than being re-read forever.
    status: Optional[int] = field(default=None)

    def read_status(self) -> Optional[int]:
        """The exit status, or None while the command is still running."""
        if self.status is not None:
            return self.status
        try:
            raw = self.status_path.read_text().strip()
        except OSError:
            return None
        if not raw:
            return None
        try:
            self.status = int(raw)
        except ValueError:
            self.status = -1
        return self.status

    def read_output(self) -> str:
        try:
            return self.log_path.read_text(errors="replace")
        except OSError:
            return ""

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


class TmuxMissing(RuntimeError):
    """tmux is not installed, so there is nothing to run commands in."""


class ShellRunner:
    """Starts commands in tmux and keeps track of the ones it started."""

    def __init__(self, state_dir: Path, tmux: str = "tmux") -> None:
        self.directory = Path(state_dir).joinpath("shell")
        self.tmux = tmux
        self.jobs: dict[str, ShellJob] = {}

    def available(self) -> bool:
        return shutil.which(self.tmux) is not None

    async def start(self, command: str, cwd: Path) -> ShellJob:
        if not self.available():
            raise TmuxMissing(f"{self.tmux} is not installed")
        self.directory.mkdir(parents=True, exist_ok=True)
        job_id = secrets.token_hex(3)
        log_path = self.directory.joinpath(f"{job_id}.log")
        status_path = self.directory.joinpath(f"{job_id}.status")
        script_path = self.directory.joinpath(f"{job_id}.sh")
        # The command goes into a script rather than onto the tmux command
        # line: it is arbitrary text, and quoting it through two shells is a
        # way to mangle exactly the commands that most need to run verbatim.
        script_path.write_text(
            "#!/usr/bin/env bash\n"
            f"cd {_quote(str(cwd))} || exit 1\n"
            "{\n"
            f"{command}\n"
            f"}} 2>&1 | tee -a {_quote(str(log_path))}\n"
            "status=${PIPESTATUS[0]}\n"
            f"printf '%s' \"$status\" > {_quote(str(status_path))}\n"
            'printf "\\n[exit %s] the pane stays open; detach with ctrl-b d\\n" "$status"\n'
            "exec bash\n"
        )
        script_path.chmod(0o700)
        session = f"{SESSION_PREFIX}{job_id}"
        process = await asyncio.create_subprocess_exec(
            self.tmux, "new-session", "-d", "-s", session, "-c", str(cwd),
            "bash", str(script_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "TMUX": ""},   # never nest inside a caller's tmux
        )
        output, _ = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError((output or b"").decode(errors="replace").strip()
                               or f"tmux exited {process.returncode}")
        job = ShellJob(job_id=job_id, command=command, cwd=Path(cwd),
                       log_path=log_path, status_path=status_path,
                       script_path=script_path, started=time.monotonic(),
                       session=session)
        self.jobs[job_id] = job
        return job

    async def wait(self, job: ShellJob, timeout: float = DEFAULT_TIMEOUT) -> Optional[int]:
        """Wait a bounded time for the command to finish.

        Returning None is not a failure: the job keeps running detached and
        stays readable. Only the *waiting* is bounded, because a chat reply
        that never comes is worse than one that says "still going".
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = job.read_status()
            if status is not None:
                return status
            await asyncio.sleep(POLL_SECONDS)
        return job.read_status()

    async def kill(self, job: ShellJob) -> bool:
        process = await asyncio.create_subprocess_exec(
            self.tmux, "kill-session", "-t", job.session,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await process.wait()
        return process.returncode == 0

    async def live_sessions(self) -> set[str]:
        """tmux session names this runner started, as tmux currently has them."""
        if not self.available():
            return set()
        process = await asyncio.create_subprocess_exec(
            self.tmux, "list-sessions", "-F", "#{session_name}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        output, _ = await process.communicate()
        return {line for line in (output or b"").decode(errors="replace").split()
                if line.startswith(SESSION_PREFIX)}


def _quote(value: str) -> str:
    """Single-quote for the shell, the way shlex.quote does."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


_CONTROL = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\r")


def tail(text: str, limit: int, measure=len) -> tuple[str, bool]:
    """The longest ending that fits `limit`, and whether anything was dropped.

    The *tail* rather than the head: a command that failed says why at the
    end, and that is the line worth spending the message on.

    `measure` exists because the budget is in *sent* characters, not source
    ones. Output goes to Telegram HTML-escaped, where a single `<` becomes
    four characters, so measuring the raw text would overrun the message limit
    on exactly the output most likely to contain markup.
    """
    cleaned = _CONTROL.sub("", text)
    if measure(cleaned) <= limit:
        return cleaned, False
    # Binary search the suffix length: the measure only grows with it.
    shortest, longest = 0, len(cleaned)
    while shortest < longest:
        candidate = (shortest + longest + 1) // 2
        if measure(cleaned[-candidate:]) <= limit:
            shortest = candidate
        else:
            longest = candidate - 1
    return cleaned[-shortest:], True
