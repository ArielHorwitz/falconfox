"""Event-loop stall watchdog: make a silent freeze leave a trace.

Born from the 2026-08-25 keepalive ping timeouts, where a websocket died
because *something* stopped answering pings for 20+ seconds and nothing
recorded what. Worse, the logs could not even attribute the stall: the daemon
and the bot went silent in overlapping windows, which on a 1-CPU / 1-GB host
deep in swap points at the machine as much as at either process.

So the watchdog answers two questions the next time it happens:

  1. **Did this process's event loop stall?** A daemon thread posts a heartbeat
     onto the loop every ``interval`` seconds; if the loop hasn't run one for
     ``threshold`` seconds, it is stalled.
  2. **Was it the loop, or the whole host?** The thread also measures its own
     oversleep. A blocked *loop* leaves the thread running (Python releases the
     GIL around I/O and sleeps), so the thread can dump the main thread's stack
     *mid-stall* — naming the exact blocking call. If the thread overslept by
     about as much as the loop, the whole process was frozen (swap thrash, CPU
     steal, a VM pause), the stack is innocent, and it says so instead —
     with the memory-pressure and major-fault evidence attached.

Both falconfox processes run one; it is diagnostic machinery, not protocol, so
sharing the module does not move any opinion across the daemon/client boundary.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from pathlib import Path

_PRESSURE_FILE = Path("/proc/pressure/memory")
_SELF_STAT = Path("/proc/self/stat")


def _memory_pressure() -> str | None:
    """The PSI ``some`` line for memory, or None where PSI is unavailable."""
    try:
        for line in _PRESSURE_FILE.read_text().splitlines():
            if line.startswith("some"):
                return line.strip()
    except OSError:
        pass
    return None


def _major_faults() -> int | None:
    """Cumulative major page faults of this process (majflt, /proc/self/stat)."""
    try:
        # Field 2 (comm) may contain spaces; parse from after the closing paren.
        after_comm = _SELF_STAT.read_text().rsplit(")", 1)[1].split()
        return int(after_comm[9])  # majflt is field 12, 1-indexed
    except (OSError, IndexError, ValueError):
        return None


def _main_thread_stack() -> str:
    frame = sys._current_frames().get(threading.main_thread().ident)
    if frame is None:
        return "<main thread frame unavailable>"
    return "".join(traceback.format_stack(frame)).rstrip()


class StallWatchdog:
    """Watch one asyncio event loop from a daemon thread; log stalls as WARNING."""

    def __init__(
        self,
        log: logging.Logger,
        *,
        interval: float = 1.0,
        threshold: float = 5.0,
    ) -> None:
        self.log = log
        self.interval = interval
        self.threshold = threshold
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._last_beat = time.monotonic()

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Start watching. Call from the loop's own thread (the usual place)."""
        self._loop = loop or asyncio.get_running_loop()
        self._last_beat = time.monotonic()
        thread = threading.Thread(
            target=self._run, name="falconfox-stall-watchdog", daemon=True
        )
        thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _beat(self) -> None:
        self._last_beat = time.monotonic()

    def _run(self) -> None:
        stall_reported = False
        faults_before = _major_faults()
        woke = time.monotonic()
        while not self._stop.wait(self.interval):
            now = time.monotonic()
            # How late this thread itself woke up: nonzero only when the whole
            # process was starved (GIL held, swap thrash, VM pause).
            oversleep = max(0.0, (now - woke) - self.interval)
            woke = now
            try:
                self._loop.call_soon_threadsafe(self._beat)
            except RuntimeError:
                return  # loop closed; nothing left to watch
            lag = now - self._last_beat
            if lag < self.threshold:
                if stall_reported:
                    self.log.warning(
                        "event loop recovered after ~%.1fs stall", lag)
                    stall_reported = False
                    faults_before = _major_faults()
                continue
            if stall_reported:
                continue  # one report per stall; recovery closes it out
            stall_reported = True
            faults_now = _major_faults()
            fault_delta = (
                faults_now - faults_before
                if faults_now is not None and faults_before is not None else None
            )
            evidence = "majflt+%s pressure[%s]" % (fault_delta, _memory_pressure())
            if oversleep > lag / 2:
                # The watchdog thread was frozen too: the host starved the whole
                # process, so the loop's current frame is bystander, not culprit.
                self.log.warning(
                    "process-wide stall: no heartbeat for %.1fs and the watchdog "
                    "thread itself overslept %.1fs — host pressure (swap/CPU), "
                    "not a blocked loop. %s", lag, oversleep, evidence)
            else:
                self.log.warning(
                    "event loop stalled for %.1fs (watchdog thread healthy — "
                    "the loop is blocked in-process). %s\n"
                    "main thread is at:\n%s", lag, evidence, _main_thread_stack())
