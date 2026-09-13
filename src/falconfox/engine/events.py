"""A tiny asyncio pub/sub bus.

The engine publishes events (plain dicts); each subscriber (a connected
client) gets its own queue. Publishing never blocks the engine on a slow
consumer. Events are the *only* way state leaves the engine, which keeps a
client a pure reflection of engine state.

The queues are bounded. They were not, and a subscriber that stopped reading
grew the daemon's memory without limit, on the same host the live-session cap
exists to protect. A subscriber that falls further behind than the bound is
dropped and its stream closed, which closes its connection, which is what
makes its client reconnect: a reconnect re-subscribes and takes a fresh
snapshot, so the recovery is the client's existing one and nothing new.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Optional

from .. import logsetup
from ..config import DEFAULT_EVENT_QUEUE_LIMIT

log = logsetup.get_logger("engine.events")

# What a dropped subscriber finds in place of the events it never took. A
# reader that sees this is at the end of its stream.
CLOSED = object()


class EventBus:
    def __init__(self, bound: Optional[int] = None) -> None:
        # Zero or less means no bound, for a caller that has decided it wants
        # the old behaviour.
        self._bound = DEFAULT_EVENT_QUEUE_LIMIT if bound is None else bound
        self._subscribers: dict[asyncio.Queue, str] = {}

    @property
    def subscribers(self) -> int:
        """How many clients are listening. Zero means an event goes nowhere,
        which is worth knowing before waiting on one to answer."""
        return len(self._subscribers)

    def publish(self, event: dict) -> None:
        for queue, name in list(self._subscribers.items()):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._drop(queue, name)

    def _drop(self, queue: asyncio.Queue, name: str) -> None:
        """Give up on a subscriber that has stopped taking its events."""
        self._subscribers.pop(queue, None)
        log.warning("subscriber %s is %d events behind and not reading; "
                    "dropping it, and the connection with it", name, queue.qsize())
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(CLOSED)

    @contextmanager
    def subscribe(self, name: str = "?"):
        """Yield a queue receiving every event published while subscribed.

        `name` is what the log calls this subscriber when it has to be
        dropped, so it should say which client this is.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=max(0, self._bound))
        self._subscribers[queue] = name
        try:
            yield queue
        finally:
            self._subscribers.pop(queue, None)
