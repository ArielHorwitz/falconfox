"""One session, held as one thing.

A session used to be an agreement between a dozen dicts in the coordinator
that they all carried the same key: `_metadata`, `_transcripts`, `_acp_ids`,
`_config_options`, `_commands`, `_pending_context`, `_auto_named`, `_usage`,
`_queued`, `_persisted`, and the live `AgentSession` in a manager of its own.
Every lifecycle site pushed and popped each of them by hand, so the two
teardown paths cleared slightly different lists and a forgotten field was a
leak with nothing to catch it.

The record is that agreement made into an object. Creating a session is
creating one, and ending it is discarding it (`delete`) or calling
`release` (`stop`), so no site hand-lists fields any more.

**"Live" is answered here, once.** The record holds the running
`AgentSession`, and `live` is "there is one". The coordinator no longer keeps
a second index of live sessions beside this one, which is what let the
live-session cap be exceeded: the old teardown popped the session from its
manager and only marked the metadata not-live after an await, so a second
caller in that window counted a slot that was already being freed. Here the
two facts cannot disagree, because there is only one.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

from .engine.session import AgentSession, PromptPart


@dataclass
class OpenTurn:
    """A turn that is open as far as the disk is concerned.

    Persisted in the metadata rather than as replayable `turn_started` and
    `turn_ended` events, because it is one fact rather than a pair of events
    to reconcile: the transcript stays a conversation, which is what a
    backend without native session loading is replayed, and what a client
    reads back is unchanged. Metadata is rewritten on every replayable event
    anyway, so carrying it costs two extra writes per turn.
    """

    turn_id: Optional[str]
    started: str
    last_event: str
    last_at: str


@dataclass
class SessionRecord:
    """Everything FalconFox knows about one session."""

    # --- what a client sees. `wire` is the whole of the shape.
    session_id: str
    name: str
    path: str
    backend: str
    created: str
    last_active: str
    ephemeral: bool = False
    hidden: bool = False
    tags: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    oriented: bool = False
    state: str = "stored"

    # --- the live agent, and therefore whether this session is live at all.
    agent: Optional[AgentSession] = None
    # The backend's own id for its session, which is what a native
    # `session/load` resumes from. Ours is `session_id`; this one is theirs.
    acp_id: Optional[str] = None

    # --- what is on disk, and what is only in memory.
    auto_named: bool = True
    # Whether this session's metadata has ever been written. Not the same
    # question as `keeps_state`, and the memory of the answer: a session
    # restored from disk, or one that earned a write earlier and has since
    # had its transcript cache dropped, is still a session with state.
    persisted: bool = False
    # None means "not read from disk yet", which is not the same as empty:
    # stopping a session drops the cache to reclaim the memory.
    transcript: Optional[list[dict]] = None

    # --- what the backend last told us. None means it never said, which is
    # why these are not plain empty containers: the snapshot carries an entry
    # only for sessions that have one.
    config_options: Optional[list[dict]] = None
    commands: Optional[list[dict]] = None
    usage: Optional[dict] = None

    # Context owed to the session on its next prompt, ahead of the user's own
    # words: a transcript replay for a backend that cannot reload one, or the
    # notice that its last turn was cut off by a restart.
    pending_context: list[PromptPart] = field(default_factory=list)

    # --- waiting for a live slot, with the message that is waiting with it.
    queued: bool = False
    queued_text: Optional[str] = None
    # Monotonic, and only for ordering the queue: sessions get their slot in
    # the order they asked for one.
    queued_since: Optional[float] = None

    # Whether this session was in the last "N sessions running" line the
    # coordinator logged, so the line is written when it changes and not on
    # every event.
    reported_busy: bool = False

    # Held by every method that changes this session, so two transitions
    # cannot interleave. See the lock ordering in coordinator.py.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # The turn in flight, if there is one. A handle rather than a flag,
    # because a transition that cannot run beside a turn has to be able to
    # end that turn and wait for it to be over.
    turn: Optional[asyncio.Task] = field(default=None, repr=False)
    # The turn that is open on disk, which is not the same thing: `turn` is
    # this process's handle on a running one, and this is the fact that
    # outlives the process. Set on `turn_started`, cleared on `turn_ended`,
    # and read back on startup to notice a turn a restart cut off.
    open_turn: Optional[OpenTurn] = None

    @property
    def live(self) -> bool:
        """Whether this session holds a live agent subprocess."""
        return self.agent is not None

    @property
    def keeps_state(self) -> bool:
        """Whether this session is worth keeping on disk.

        One answer to one question, because two callers used to reach it by
        different routes and disagree. What `stop` keeps and what a write
        saves have to be the same thing, or stopping a session deletes what
        the last write put there.

        The transcript is the *last* thing consulted, and only ever to earn
        an answer that has not been earned yet: it is a cache that a stop
        drops and a restart starts empty, so reading it first is how a
        restored session came to look like one that had never said anything.
        """
        if self.ephemeral:
            return False
        if self.persisted or not self.auto_named:
            return True
        return any(event.get("type") == "message"
                   for event in (self.transcript or []))

    def wire(self) -> dict:
        """The session as every client sees it.

        The one place that shape is written. `always_allow` is a constant the
        PoC has no interactive posture behind, and it stays on the wire
        because clients read it.
        """
        return {
            "session_id": self.session_id,
            "name": self.name,
            "path": self.path,
            "backend": self.backend,
            "always_allow": True,
            "ephemeral": self.ephemeral,
            "hidden": self.hidden,
            "tags": list(self.tags),
            "roles": list(self.roles),
            "oriented": self.oriented,
            "state": self.state,
            "live": self.live,
            "created": self.created,
            "last_active": self.last_active,
        }

    def stored(self) -> dict:
        """The session as `meta.toml` holds it.

        Deliberately not `wire`: `state` and `live` describe a running
        process and are always "stored" and false on the way back in, while
        `named` and the backend's session id matter to a restart and to
        nobody else.
        """
        # An open turn is written out; a closed one leaves no keys at all,
        # since the file is replaced rather than edited.
        turn = {} if self.open_turn is None else {
            "turn_open": True,
            "turn_id": self.open_turn.turn_id,
            "turn_started": self.open_turn.started,
            "turn_last_event": self.open_turn.last_event,
            "turn_last_at": self.open_turn.last_at,
        }
        return {
            "session_id": self.session_id,
            "name": self.name,
            "path": self.path,
            "backend": self.backend,
            "always_allow": True,
            "named": not self.auto_named,
            "acp_session_id": self.acp_id,
            "hidden": self.hidden,
            "tags": list(self.tags),
            **turn,
            # Roles decide the orientation, and `oriented` decides whether it
            # is still owed. Both have to survive a restart or a session that
            # was created and not yet spoken to would come back either
            # unoriented forever or oriented as something it is not.
            "roles": list(self.roles),
            "oriented": self.oriented,
            "created": self.created,
            "last_active": self.last_active,
        }

    def release(self) -> None:
        """Give up everything that belonged to the running agent.

        What `stop` leaves behind is the session as it exists on disk: its
        metadata, the backend's session id, whether it was named. Everything
        that only meant something while a subprocess was running goes, and it
        goes from one list rather than from each caller's own.
        """
        self.agent = None
        self.turn = None
        self.state = "stored"
        self.transcript = None
        self.config_options = None
        self.commands = None
        self.pending_context = []
        self.queued = False
        self.queued_text = None
        self.queued_since = None
