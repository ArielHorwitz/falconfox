"""Global session coordinator over the vendor-neutral ACP engine."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Sequence

from . import config, help as ffhelp, logsetup, state, storage
from .engine import oneshot
from .engine.client import resolve_config_value
from .engine.events import EventBus
from .engine.session import AgentSession, PromptPart, new_session_id
from .errors import FalconFoxError
from .record import OpenTurn, SessionRecord

_REPLAYABLE = {"message", "tool_call", "notice", "plan", "usage"}
# How the last thing a cut-off turn recorded is named to the agent that was
# in the middle of it. Plain words rather than the event type, since the
# reader is being told where it got to, not read a log.
_EVENT_IN_WORDS = {
    "message": "a message", "tool_call": "a tool call", "notice": "a notice",
    "plan": "a plan", "usage": "a token count",
    "turn_started": "the start of the turn itself",
}
# How long `attach` waits for the client to report back. Generous: the
# client is uploading a file of unknown size over a network.
ATTACHMENT_TIMEOUT = 120.0
_LOG_INFO_EVENTS = {
    "session_added", "session_removed", "config_changed", "permission_request",
    "permission_resolved", "transcript_reset",
}


def _now_iso() -> str:
    return datetime.datetime.now().isoformat()


def _normalize_tags(tags: list) -> list[str]:
    """Fold a tag list into the form clients can match on.

    Tags are opaque to FalconFox: it stores them and shows them, and what
    they mean is between the user and whoever reads them. The only rules are
    the mechanical ones a lookup needs -- lowercase, and no whitespace, so a
    tag is one word that matches by string. Order is preserved and meaningful,
    since a client mapping tags to a single slot (a topic icon) takes the
    first one it knows.
    """
    seen: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            raise FalconFoxError(f"tags must be strings, got {type(tag).__name__}")
        folded = tag.strip().lower()
        if not folded:
            continue
        if any(character.isspace() for character in folded):
            raise FalconFoxError(f"tags must not contain whitespace: {tag!r}")
        if folded not in seen:
            seen.append(folded)
    return seen


def _interrupted_turn_context(meta: dict) -> str:
    """What to tell a session whose last turn a restart cut off."""
    last_event = meta.get("turn_last_event") or ""
    last_at = str(meta.get("turn_last_at") or meta.get("turn_started") or "")
    return config.INTERRUPTED_TURN_CONTEXT.format(
        last_event=_EVENT_IN_WORDS.get(last_event, f"a {last_event} event"),
        last_at=last_at.split(".")[0].replace("T", " ") or "an unrecorded time",
    )


def _clean_name(reply: str) -> str:
    first_line = reply.strip().splitlines()[0] if reply.strip() else ""
    return first_line.strip().strip("\"'").strip()[:80]


def _auto_allow_option(options: list[dict]) -> Optional[str]:
    for kind in ("allow_always", "allow_once"):
        for option in options:
            if option.get("kind") == kind:
                return option["option_id"]
    return options[0]["option_id"] if options else None


class SessionCoordinator:
    """Own all FalconFox sessions, regardless of their working directory."""

    def __init__(self, store_root: Path | None = None) -> None:
        self.log = logsetup.get_logger("coordinator")
        self.config = config.load_config()
        self.store = storage.SessionStore(store_root)
        self.bus = EventBus(self.config.event_queue_limit)
        # One record per session, holding everything about it -- including the
        # live `AgentSession`, so "is this live" has one answer. See record.py.
        self._records: dict[str, SessionRecord] = {}
        # In-flight `attach` calls, keyed by request id rather than by session:
        # this is the state of one call, not of a session, and several can be
        # outstanding for the same session at once. The daemon cannot send a
        # file itself -- only the client attached to the chat can -- so the
        # HTTP call waits here until that client reports back.
        self._attachments: dict[str, asyncio.Future] = {}
        # Slot accounting: see the lock ordering below.
        self._slot_lock = asyncio.Lock()
        self._draining = False

    # --- serialising transitions ---------------------------------------
    #
    # Two locks, always taken in this order and never the other way round:
    #
    #   1. `_slot_lock`, by anything that may need a live slot (`add_session`,
    #      `resume_session`, `send`). It is held across `_ensure_slot` *and*
    #      the activation that follows it, because a slot that is checked in
    #      one step and filled in another can be handed to two callers -- the
    #      cap being exceeded is the OOM it exists to prevent.
    #   2. the session's own `record.lock`, by every method that changes that
    #      session.
    #
    # The only thing that ever holds two session locks at once is the holder
    # of the slot lock, evicting a victim, and nothing holding a session lock
    # ever asks for the slot lock. So there is no cycle, and nothing to
    # deadlock on. What keeps it that way: a method holding a session lock
    # must never await another public method that takes the same one. The
    # `_locked` helpers below exist for exactly that and assume it is held.
    #
    # Deliberately *not* under any lock: the turn itself. `send` holds the
    # locks while it starts a turn and gives them up before waiting for it,
    # or `cancel`, `stop` and `delete` could never reach a session that was
    # busy -- which is exactly when they are wanted. What stops a transition
    # racing a turn is `_settle_turn`, not the lock.

    @asynccontextmanager
    async def _locked(self, session_id: str):
        """Yield a session's record, held against every other transition."""
        record = self._require(session_id)
        async with record.lock:
            # It can be deleted while we wait for the lock, and acting on a
            # discarded record would resurrect what the delete threw away.
            if self._records.get(session_id) is not record:
                raise FalconFoxError(f"no such session: {session_id}")
            yield record

    async def _settle_turn(self, record: SessionRecord, reason: str) -> None:
        """End an in-flight turn before a transition that cannot run beside it.

        Cancelled rather than waited for, and said out loud. Waiting would
        hold a delete behind a turn the user has already decided to discard,
        for as long as that turn runs. Dropping it in silence is what used to
        happen and is worse: the reply landed after the session was gone and
        was discarded with no line to show for it.

        The turn ends the way a backend-side cancel ends one -- `turn_ended`
        with `stop_reason: cancelled` -- so no client learns a second shape.
        """
        turn = record.turn
        record.turn = None
        if turn is None or turn.done():
            return
        self.log.warning("session=%s (%s): %s while a turn was in flight; "
                         "the turn is cancelled", record.session_id, record.name,
                         reason)
        # Said before the cancellation, not after: a client that finalizes a
        # turn on `turn_ended` has nowhere to put a notice arriving behind it.
        self._emit({"type": "notice", "session_id": record.session_id,
                    "level": "error",
                    "message": f"The turn running here was cut short: {reason}."})
        turn.cancel()
        await asyncio.wait({turn})

    # --- persistence and event flow ------------------------------------

    def load_persisted(self) -> None:
        for meta in self.store.load_all_meta():
            session_id = meta["session_id"]
            created = meta.get("created")
            self._records[session_id] = SessionRecord(
                session_id=session_id,
                name=meta.get("name", session_id),
                path=meta.get("path", str(Path.home())),
                backend=meta.get("backend", ""),
                created=created,
                last_active=meta.get("last_active") or created,
                ephemeral=False,
                # Restored from disk: a client's plumbing must still be
                # plumbing after a daemon restart, or it reappears in every
                # listing and starts competing as if it were the user's.
                hidden=bool(meta.get("hidden")),
                tags=_normalize_tags(meta.get("tags") or []),
                roles=list(meta.get("roles") or []),
                oriented=bool(meta.get("oriented")),
                acp_id=meta.get("acp_session_id"),
                auto_named=not bool(meta.get("named", False)),
                persisted=True,
            )
            if meta.get("turn_open"):
                self._adopt_interrupted_turn(self._records[session_id], meta)

    def _adopt_interrupted_turn(self, record: SessionRecord, meta: dict) -> None:
        """Owe a session the news that its last turn was cut off.

        Queued through the pending-context channel rather than announced,
        because there is nothing running to announce it to: the session is
        stored, and the agent that was mid-turn is gone. It reaches the next
        one that starts, ahead of the user's own words.

        The marker is left standing on disk until a turn actually ends, so a
        second restart before the session is next spoken to says it again,
        which is right: it still has not been told.
        """
        record.open_turn = OpenTurn(
            turn_id=meta.get("turn_id"),
            started=str(meta.get("turn_started") or ""),
            last_event=str(meta.get("turn_last_event") or ""),
            last_at=str(meta.get("turn_last_at") or meta.get("turn_started") or ""),
        )
        record.pending_context.append(
            PromptPart(text=_interrupted_turn_context(meta), system=True))
        self.log.warning("session=%s (%s) came back with a turn still open "
                         "(turn=%s, last %s at %s); its next prompt will say so",
                         record.session_id, record.name, record.open_turn.turn_id,
                         record.open_turn.last_event, record.open_turn.last_at)

    def _ensure_transcript(self, record: SessionRecord) -> list[dict]:
        if record.transcript is None:
            record.transcript = self.store.read_transcript(record.session_id)
        return record.transcript

    def _emit(self, event: dict) -> None:
        event.setdefault("ts", _now_iso())
        session_id = event.get("session_id")
        event_type = event.get("type")
        record = self._records.get(session_id)
        if record is not None:
            if event_type == "agent_state":
                record.state = event.get("state")
            elif event_type == "usage":
                merged = record.usage if record.usage is not None else {}
                for key, value in event.items():
                    if key not in ("type", "session_id") and value is not None:
                        merged[key] = value
                record.usage = merged
            elif event_type == "config_options":
                record.config_options = event.get("options", [])
            elif event_type == "commands":
                record.commands = event.get("commands", [])
            # A turn's boundaries are persisted as one fact in the metadata
            # rather than as two replayable events, so a restart mid-turn
            # leaves a marker where it used to leave nothing at all.
            if event_type == "turn_started":
                record.open_turn = OpenTurn(
                    turn_id=event.get("turn_id"), started=event["ts"],
                    last_event="turn_started", last_at=event["ts"])
                self._persist_meta(record)
            elif event_type == "turn_ended":
                record.open_turn = None
                self._persist_meta(record)
            elif record.open_turn is not None and event_type in _REPLAYABLE:
                # Roughly where the turn got to, for free: the metadata is
                # rewritten on every replayable event as it is.
                record.open_turn.last_event = event_type
                record.open_turn.last_at = event["ts"]
            if event_type in _REPLAYABLE:
                record.last_active = _now_iso()
                # Loaded before appending, not appended to whatever happens to
                # be cached: a session whose transcript was dropped on stop
                # would otherwise end up holding a one-event history, which a
                # resume then shows the client and a revert then writes to
                # disk over the real one.
                self._ensure_transcript(record).append(event)
                if record.keeps_state:
                    self._persist_meta(record)
                    self.store.append_event(session_id, event)
        self.bus.publish(event)
        self._log_event(event)
        if event_type in ("agent_state", "session_added", "session_updated", "session_removed"):
            self._report_activity()
        # A session going idle is the only thing that makes an occupied slot
        # evictable, so it is the moment to retry anything waiting for one.
        if event_type == "agent_state" and event.get("state") == "idle" and self._anything_queued():
            self._schedule_drain()

    def _log_event(self, event: dict) -> None:
        event_type = event.get("type")
        session_id = event.get("session_id")
        record = self._records.get(session_id) if session_id else None
        name = record.name if record is not None else None
        if event_type == "agent_state":
            return  # turns log themselves below; idle/working are mere states
        if event_type == "turn_started":
            self.log.info("turn start: session=%s name=%s turn=%s prompt_chars=%s",
                          session_id, name, event.get("turn_id"),
                          event.get("prompt_chars"))
            return
        if event_type == "turn_ended":
            line = ("turn complete: session=%s name=%s turn=%s outcome=%s stop=%s "
                    "duration=%ss chunks=%s chars=%s thoughts=%s tools=%s")
            values = (session_id, name, event.get("turn_id"), event.get("outcome"),
                      event.get("stop_reason"), event.get("duration"),
                      event.get("message_chunks"), event.get("output_chars"),
                      event.get("thought_chunks"), event.get("tool_calls"))
            silent = (event.get("output_chars") == 0
                      and event.get("outcome") == "completed"
                      and event.get("stop_reason") != "cancelled")
            if silent:
                # The recurring failure shape: a turn ends with nothing to show
                # and nobody notices. Detectable right here, so it is a warning.
                self.log.warning(line + " — turn produced NO output", *values)
            else:
                self.log.info(line, *values)
            return
        if event_type == "notice":
            self.log.info("notice[%s]: session=%s msg=%s", event.get("level", "info"),
                          session_id, event.get("message"))
            return
        level = logging.INFO if event_type in _LOG_INFO_EVENTS else logging.DEBUG
        self.log.log(level, "event=%s session=%s", event_type, session_id)

    def _report_activity(self) -> None:
        busy = {record.session_id for record in self._records.values()
                if record.live and record.state in ("starting", "working")}
        if busy == {record.session_id for record in self._records.values()
                    if record.reported_busy}:
            return
        for record in self._records.values():
            record.reported_busy = record.session_id in busy
        if not busy:
            self.log.info("all sessions idle")
        else:
            running = ", ".join(
                f"{self._records[s].name} ({self._records[s].state})" for s in busy
            )
            self.log.info("%d session(s) running: %s", len(busy), running)

    def _persist_meta(self, record: SessionRecord) -> None:
        if not record.keeps_state:
            return
        record.persisted = True
        self.store.write_meta(record.stored())

    # --- metadata/config views -----------------------------------------

    def list_sessions(self, include_hidden: bool = False) -> list[dict]:
        """Sessions, minus a client's own plumbing unless asked for.

        `hidden` is deliberately separate from `ephemeral`. Hiding is about
        the listing; ephemeral is about being a throwaway, which also makes
        `stop` a `delete`. Infrastructure wants the first without the second,
        so it can be stopped to reclaim memory and resumed with its
        conversation intact.
        """
        sessions = [record.wire() for record in self._records.values()
                    if include_hidden or not record.hidden]
        return sorted(sessions, key=lambda item: item.get("created") or "")

    def get_session(self, session_id: str) -> dict:
        record = self._require(session_id)
        return {**record.wire(), "usage": dict(record.usage or {})}

    def transcript(self, session_id: str) -> list[dict]:
        return list(self._ensure_transcript(self._require(session_id)))

    def open_session(self, session_id: str) -> None:
        transcript = self.transcript(session_id)
        self._emit({"type": "transcript_reset", "session_id": session_id,
                    "transcript": transcript})

    def list_backends(self) -> dict:
        return {"backends": sorted(self.config.backends), "default": self.config.default_backend}

    def hotkeys(self) -> dict:
        return dict(self.config.hotkeys)

    def ui_config(self) -> dict:
        return dict(self.config.ui)

    def reload_config(self) -> None:
        self.config = config.load_config()
        self._emit({"type": "config_changed"})

    def _require(self, session_id: str) -> SessionRecord:
        record = self._records.get(session_id)
        if record is None:
            raise FalconFoxError(f"no such session: {session_id}")
        return record

    # --- session lifecycle ---------------------------------------------

    async def add_session(
        self,
        path: str | Path | None = None,
        name: Optional[str] = None,
        backend_name: Optional[str] = None,
        ephemeral: bool = False,
        hidden: Optional[bool] = None,
        roles: Optional[Sequence[str]] = None,
    ) -> str:
        working_path = Path(path or Path.home()).expanduser().resolve()
        if not working_path.is_dir():
            raise FalconFoxError(f"session path is not a directory: {working_path}")
        try:
            backend = self.config.select_backend(backend_name)
        except KeyError as error:
            raise FalconFoxError(str(error)) from error
        session_id = new_session_id()
        # The slot lock spans the whole of making this session live, so that
        # the room `_ensure_slot` makes is the room this session takes.
        async with self._slot_lock:
            now = _now_iso()
            # Decided before the subprocess exists: a new session over the
            # limit is created *stored*, so it has an id, metadata, a
            # transcript and -- for the Telegram client -- a topic, and simply
            # is not running yet. Refusing instead would deny the user
            # something the interface invites. A throwaway is hidden by
            # default; infrastructure asks for hidden without asking to be
            # thrown away.
            record = SessionRecord(
                session_id=session_id,
                name=(name or "").strip() or f"Session {len(self._records) + 1}",
                path=str(working_path),
                backend=backend.name,
                created=now,
                last_active=now,
                ephemeral=bool(ephemeral),
                hidden=bool(ephemeral) if hidden is None else bool(hidden),
                roles=list(roles or []),
                auto_named=not bool((name or "").strip()),
            )
            has_slot = await self._ensure_slot()
            self._records[session_id] = record
            # Uncontended, since nobody else has the id yet -- but the id is
            # public from `session_added` on, and starting is a long await.
            async with record.lock:
                if not has_slot:
                    self._persist_meta(record)
                    self._emit({"type": "session_added", **record.wire()})
                    self._enqueue(record, None)
                    return session_id
                record.agent = AgentSession(
                    session_id=session_id,
                    name=record.name,
                    path=working_path,
                    backend=backend,
                    emit=self._emit,
                    request_permission=self._request_permission,
                )
                record.state = "starting"
                self._persist_meta(record)
                self._emit({"type": "session_added", **record.wire()})
                try:
                    await record.agent.start()
                except Exception as error:
                    # Discarded whole, rather than field by field: whatever
                    # the record picked up on the way to failing goes with it.
                    self._records.pop(session_id, None)
                    self.store.delete(session_id)
                    self._emit({"type": "session_removed", "session_id": session_id})
                    self._emit({"type": "notice", "session_id": session_id,
                                "level": "error",
                                "message": f"failed to start session: {error}"})
                    raise
                record.acp_id = record.agent.acp_session_id
                await self._apply_config_options(record)
                self._persist_meta(record)
        return session_id

    # --- the live-session cap ------------------------------------------

    def live_session_ids(self) -> list[str]:
        """Every session holding a live agent subprocess.

        Client infrastructure -- the Telegram manager and its private chat --
        counts, because it is real memory and a limit that omits real
        processes is a lie. Nothing privileges it either: it is hidden but
        resumable, so it queues for a slot, and for eviction by recency, like
        everything else -- costing a resume rather than its conversation.
        """
        return [record.session_id for record in self._records.values() if record.live]

    async def _ensure_slot(self) -> bool:
        """Make room for one more live session. True if there is room now.

        Sessions are the unit of memory cost -- each holds its own ACP backend
        subprocess -- and this daemon has been OOM-killed carrying ten of them.
        Eviction is least-recently-used among *idle* sessions: evicting by
        oldest activation would take the session you have had open all day,
        and evicting a working one would destroy a turn in flight.
        """
        limit = self.config.max_live_sessions
        if limit <= 0:
            return True
        live = self.live_session_ids()
        if len(live) < limit:
            return True
        # Plain least-recently-used, with no class of session privileged.
        # Infrastructure once skipped this queue outright, so that the manager
        # was reachable even with every session busy. That made the limit a
        # number the daemon could exceed, which is the one thing a limit must
        # not be, and it bought a guarantee the client already provides out of
        # band: the commands that stop a turn or run a shell are the client's
        # own, and reach neither the cap nor an agent. So infrastructure waits
        # like anything else, and its wait ends at the next idle session.
        candidates = sorted(
            (sid for sid in live if self._records[sid].state == "idle"),
            key=lambda sid: self._records[sid].last_active or "",
        )
        if not candidates:
            return False
        victim = candidates[0]
        self.log.info("session limit %d reached: stopping least-recently-used %s (%s)",
                      limit, victim, self._records[victim].name)
        # Said before the stop, in the victim's own words, so a topic that
        # closes under the user reads as the system managing memory rather
        # than as their session mysteriously dying. `kind` marks the notices a
        # client should surface: most notices are internal chatter.
        self._emit({
            "type": "notice", "session_id": victim, "level": "info",
            "kind": "capacity",
            "message": f"Stopped to free a session slot — {limit} of {limit} of "
                       f"your sessions were active and this one was idle "
                       f"longest. Nothing is lost: send a message here to pick "
                       f"it up again.",
        })
        await self.stop_session(victim)
        return True

    def _enqueue(self, record: SessionRecord, text: Optional[str]) -> None:
        record.queued = True
        record.queued_text = text or record.queued_text
        if record.queued_since is None:
            record.queued_since = time.monotonic()
        live = len(self.live_session_ids())
        self.log.info("session %s queued for a slot (%d live, limit %d)",
                      record.session_id, live, self.config.max_live_sessions)
        self._emit({"type": "notice", "session_id": record.session_id, "level": "info",
                    "kind": "capacity",
                    "message": f"Waiting for a free session slot — {live} of "
                               f"{self.config.max_live_sessions} of your "
                               f"sessions are active and busy. This starts as "
                               f"soon as one goes idle."})

    def _dequeue(self, record: SessionRecord) -> None:
        """Off the waiting list, keeping whatever is waiting to be said."""
        record.queued = False
        record.queued_since = None

    def _anything_queued(self) -> bool:
        return any(record.queued for record in self._records.values())

    def _waiting_for_a_slot(self) -> list[SessionRecord]:
        """Queued sessions, in the order they asked for a slot."""
        return sorted((record for record in self._records.values() if record.queued),
                      key=lambda record: record.queued_since or 0.0)

    def _schedule_drain(self) -> None:
        if self._draining:
            return
        self._draining = True
        asyncio.create_task(self._drain_queue())

    async def _drain_queue(self) -> None:
        """Activate queued sessions while slots can be freed. Never fatal."""
        try:
            for record in self._waiting_for_a_slot():
                if self._records.get(record.session_id) is not record:
                    continue  # deleted while the queue was draining
                try:
                    await self.resume_session(record.session_id)
                    if record.queued:
                        return  # no slot came free; the rest are waiting too
                    # Held until it is actually said, so a resume that has to
                    # queue again keeps the message it was queued with.
                    text = record.queued_text
                    record.queued_text = None
                    if text:
                        await self.send(record.session_id, text)
                except Exception as error:
                    # Off the waiting list: a session that cannot start does
                    # not get retried at every idle event for the life of the
                    # daemon, each retry costing another notice.
                    self._dequeue(record)
                    record.queued_text = None
                    self.log.warning("could not activate queued session %s: %s",
                                     record.session_id, error, exc_info=True)
                    self._emit({"type": "notice", "session_id": record.session_id,
                                "level": "error",
                                "message": f"could not start after waiting: {error}"})
        finally:
            self._draining = False

    async def resume_session(self, session_id: str) -> None:
        async with self._slot_lock, self._locked(session_id) as record:
            await self._resume_locked(record)

    async def _resume_locked(self, record: SessionRecord) -> None:
        session_id = record.session_id
        if record.live:
            self._dequeue(record)
            return
        if not await self._ensure_slot():
            self._enqueue(record, None)
            return
        path = Path(record.path)
        if not path.is_dir():
            raise FalconFoxError(f"session path is not a directory: {path}")
        try:
            backend = self.config.select_backend(record.backend or None)
        except KeyError as error:
            raise FalconFoxError(str(error)) from error
        session = AgentSession(
            session_id=session_id,
            name=record.name,
            path=path,
            backend=backend,
            emit=self._emit,
            request_permission=self._request_permission,
        )
        record.agent = session
        record.state = "starting"
        self._dequeue(record)
        self._emit({"type": "session_updated", **record.wire()})
        stored_acp_id = record.acp_id
        try:
            loaded = await session.resume(stored_acp_id)
        except Exception as error:
            # The backend's own session id can go stale -- it is not found if
            # the backend never persisted anything for it (a session stopped
            # before its first turn is the common case, and automatic eviction
            # under the live-session cap makes that common). Falling back to a
            # fresh backend session is exactly the degraded path `loaded=False`
            # already exists for: the transcript is ours, so the conversation
            # is replayed as context rather than lost.
            if stored_acp_id is None:
                self._resume_failed(record)
                raise
            self.log.warning("could not load backend session for %s (%s); "
                             "starting a fresh one", session_id, error)
            record.acp_id = None
            try:
                loaded = await session.resume(None)
            except Exception:
                self._resume_failed(record)
                raise
        record.acp_id = session.acp_session_id
        await self._apply_config_options(record)
        self._persist_meta(record)
        transcript = self._ensure_transcript(record)
        self._emit({"type": "transcript_reset", "session_id": session_id,
                    "transcript": transcript})
        if not loaded and transcript:
            # Appended, not substituted. The orientation this session has
            # not received yet is still owed to it, and a transcript cannot
            # stand in for it: `record=False` keeps replays out of the
            # transcript, so a replay never contains one.
            record.pending_context.append(
                PromptPart(text=self._context_prompt(record),
                           system=True, record=False))
            self._emit({"type": "notice", "session_id": session_id,
                        "message": "Context re-sent from saved transcript imperfectly — "
                                   "this backend has no native session loading."})

    def _resume_failed(self, record: SessionRecord) -> None:
        record.agent = None
        record.state = "stored"
        self._emit({"type": "session_updated", **record.wire()})

    def _orientation_parts(self, record: SessionRecord) -> list[PromptPart]:
        """A session's orientation, once, on the first prompt it ever gets.

        Built here rather than queued at spawn, and the difference matters: a
        session created and not yet spoken to would otherwise lose its
        orientation to a daemon restart, because the queue is in memory while
        the session is on disk. What is persisted instead is the fact that it
        has been told -- which also means the roles are read back from
        metadata, so a restart rebuilds exactly the same text.
        """
        if record.oriented:
            return []
        parts = [PromptPart(text=piece, system=True)
                 for piece in self._orientation(record.roles)]
        record.oriented = True
        self._persist_meta(record)
        return parts

    def _orientation(self, roles: Sequence[str]) -> list[str]:
        """The pieces a session is told about itself, in reading order.

        Global first, then every client registered for this daemon run, then
        the text for each role the session holds. Client orientations are
        unconditional: a session may be started in one client and spoken to
        through another later, so it needs all of them whatever it is talking
        to right now. A role's text is conditional on holding the role.
        """
        clients = self._client_registrations()
        pieces = [config.SESSION_CONTEXT + self._help_index()]
        pieces += [entry["orientation"] for _, entry in sorted(clients.items())
                   if entry["orientation"]]
        for role in roles:
            piece = self._role_orientation(role, clients)
            if piece:
                pieces.append(piece)
            else:
                # Loud, because a role with no text is a session that believes
                # it has a job nobody described to it.
                self.log.warning("no orientation registered for role %r", role)
        # Every piece ends with a newline. Blocks arrive at a backend as an
        # array and are joined by it, so a piece ending mid-line runs into the
        # next one's heading -- seen in a live session as
        # "...Telegram commands# Talking through Telegram". Guaranteed here
        # rather than asked of each author, since registered files are read
        # stripped and their authors are other people's clients.
        return [piece.rstrip() + "\n" for piece in pieces]

    def _help_index(self) -> str:
        """The lookup table, appended to the global piece.

        Generated here rather than written by a client, because it is the one
        part that spans every namespace: a client knows what it registered and
        nothing about anyone else, while the daemon sees all of them and its
        own besides. Generated per spawn, so it describes what is actually
        registered rather than what was once expected to be.
        """
        listing = ffhelp.index(state.clients_dir().parent)
        if not listing:
            return ""
        return ("\n\n## Looking things up\n"
                "\n"
                "`falconfox help <module>` prints one of these, and "
                "`falconfox help` on its own reprints the list:\n"
                "\n"
                f"{listing}")

    def _role_orientation(self, role: str, clients: dict) -> Optional[str]:
        """Resolve `telegram.concierge`, or `.manager` for the daemon's own.

        The namespace is the client that registered the role, so two clients
        can both offer a "concierge" without meeting. A bare name with no dot
        is read as the daemon's, which makes `--role manager` work as well as
        `--role .manager`.
        """
        namespace, _, name = role.rpartition(".")
        if not namespace:
            return config.ROLE_ORIENTATIONS.get(name)
        return (clients.get(namespace) or {}).get("roles", {}).get(name)

    def _client_registrations(self) -> dict[str, dict]:
        """What each client wrote for this daemon run.

        Read per spawn, not once at startup, because the daemon starts before
        its clients do -- the Telegram unit is `After=falconfox-daemon` -- so a
        single read at startup would find an empty directory on every boot.

        A client's directory name is its namespace, so nothing here has to
        trust a name a client declared for itself.
        """
        registrations: dict[str, dict] = {}
        root = state.clients_dir()
        if not root.is_dir():
            return registrations
        for client in sorted(root.iterdir()):
            if not client.is_dir():
                continue
            roles = {}
            roles_dir = client.joinpath("roles")
            if roles_dir.is_dir():
                for role_file in sorted(roles_dir.glob("*.md")):
                    roles[role_file.stem] = self._read_orientation(role_file)
            registrations[client.name] = {
                "orientation": self._read_orientation(client.joinpath("orientation.md")),
                "roles": {name: body for name, body in roles.items() if body},
            }
        return registrations

    def _read_orientation(self, path: Path) -> str:
        """A registration file, or "" if it is missing or unreadable.

        Never fatal: a client that wrote nonsense should cost its own
        orientation, not every spawn on the daemon.
        """
        try:
            return path.read_text().strip()
        except OSError:
            self.log.warning("could not read client orientation %s", path,
                             exc_info=True)
            return ""

    def _context_prompt(self, record: SessionRecord) -> str:
        body = self._transcript_text(record, limit=24000)
        return (
            "You are resuming a previous session that was interrupted. This backend "
            "cannot restore it natively, so below is the prior conversation. Re-read "
            "files as needed and continue from where it stopped.\n\n"
            f"=== prior conversation ===\n{body}\n=== end of prior conversation ==="
        )

    async def send(self, session_id: str, text: str) -> None:
        self._require(session_id)
        if not (text or "").strip():
            raise FalconFoxError("message must not be empty")
        async with self._slot_lock, self._locked(session_id) as record:
            turn = await self._begin_turn(record, text)
        if turn is None:
            return
        # Waited for with both locks given up. A turn runs for as long as the
        # agent takes, and `cancel`, `stop` and `delete` have to be able to
        # reach the session while it does.
        await asyncio.wait({turn})
        if not turn.cancelled() and turn.exception() is not None:
            raise turn.exception()

    async def _begin_turn(self, record: SessionRecord,
                          text: str) -> Optional[asyncio.Task]:
        """Start a turn and return it, or None if the session had to queue."""
        session_id = record.session_id
        if not record.live:
            await self._resume_locked(record)
        if not record.live:
            if record.queued:
                # Held, not lost. Blocking here instead would be worse than
                # useless: `send` is an HTTP call with a 40-second client
                # timeout, and the slot may not free for many minutes.
                self._enqueue(record, text)
                return None
            raise FalconFoxError(f"could not resume session: {session_id}")
        # Orientation first, then anything else pending, then the user's
        # words -- each its own block, so no producer can displace another.
        parts = self._orientation_parts(record)
        parts += record.pending_context
        record.pending_context = []
        parts.append(PromptPart(text=text))
        running = record.turn is not None and not record.turn.done()
        turn = asyncio.create_task(record.agent.send(parts))
        # Stepped once before the lock is given up: the engine marks itself
        # busy and emits the turn start without awaiting anything, so a
        # second send waiting on this lock finds a turn in progress rather
        # than opening another one on the same connection.
        await asyncio.sleep(0)
        if not running:
            record.turn = turn
        return turn

    async def attach(self, session_id: str, path: str,
                     caption: Optional[str] = None, ack: bool = True,
                     raw: bool = False,
                     timeout: float = ATTACHMENT_TIMEOUT) -> dict:
        """Hand a file to whichever client is showing this session.

        The daemon has no chat of its own, so this is a request to the client
        and the answer has to come back from it. Waiting for that answer is the
        default because the alternative is an agent that cannot tell the
        difference between a delivered file and one that was silently dropped.
        """
        self._require(session_id)
        source = Path(path).expanduser()
        if not source.is_file():
            raise FalconFoxError(f"not a file: {source}")
        if not self.bus.subscribers:
            # Fail now rather than after the timeout: nothing is listening, so
            # waiting cannot change the outcome, only how long it takes.
            raise FalconFoxError("no client is connected to send the file to")
        request_id = uuid.uuid4().hex[:8]
        waiter: Optional[asyncio.Future] = None
        if ack:
            waiter = asyncio.get_running_loop().create_future()
            self._attachments[request_id] = waiter
        # `raw` is a hint, not an instruction: the daemon has no idea what a
        # client can do with a file, only that this one should not be degraded
        # to display it.
        self._emit({"type": "attachment", "session_id": session_id,
                    "path": str(source.resolve()), "caption": caption,
                    "raw": bool(raw), "request_id": request_id})
        if waiter is None:
            return {"request_id": request_id, "delivered": None}
        try:
            result = await asyncio.wait_for(waiter, timeout)
        except asyncio.TimeoutError:
            raise FalconFoxError(
                f"no client confirmed the file within {timeout:.0f}s") from None
        finally:
            self._attachments.pop(request_id, None)
        if not result.get("ok"):
            raise FalconFoxError(result.get("error") or "the client could not send the file")
        return {"request_id": request_id, "delivered": True}

    def resolve_attachment(self, request_id: str, ok: bool,
                           error: Optional[str] = None) -> None:
        """A client reporting what became of an attachment it was handed."""
        waiter = self._attachments.get(request_id)
        if waiter is None or waiter.done():
            return  # already timed out, or never waited for
        waiter.set_result({"ok": ok, "error": error})

    # --- the inbox -----------------------------------------------------
    #
    # Files given to a session from outside it, the counterpart to `attach`.
    # The daemon stores them and nothing more: it does not decide when one
    # reaches the agent, because the client is what composes a prompt.
    #
    # What the daemon owns here is the lifetime. A session's files live inside
    # its own directory, so deleting the session takes them with it and no
    # client has to watch for that, get it right, or leak the files of every
    # session deleted while it happened to be down.

    def add_file(self, session_id: str, path: str,
                 name: Optional[str] = None) -> dict:
        """Copy a file into the session's store. Returns its id and path.

        `name` is what to store it under, for a caller holding a download
        whose own filename means nothing.
        """
        record = self._require(session_id)
        if record.ephemeral:
            # Ephemeral means nothing on disk, so there is nowhere to put it
            # and nothing that would ever clean it up.
            raise FalconFoxError("an ephemeral session has no file store")
        source = Path(path).expanduser()
        if not source.is_file():
            raise FalconFoxError(f"not a file: {source}")
        file_id, stored = self.store.add_file(session_id, source, name or source.name)
        return {"file_id": file_id, "path": str(stored), "name": stored.name}

    def remove_file(self, session_id: str, file_id: str) -> dict:
        """Delete one stored file, by the id `add_file` returned."""
        self._require(session_id)
        return {"removed": int(self.store.remove_file(session_id, file_id))}

    def clear_files(self, session_id: str) -> dict:
        """Delete every file stored for a session."""
        self._require(session_id)
        return {"removed": self.store.clear_files(session_id)}

    async def cancel(self, session_id: str) -> None:
        async with self._locked(session_id) as record:
            if record.agent is not None:
                await record.agent.cancel()

    async def stop_session(self, session_id: str) -> None:
        async with self._locked(session_id) as record:
            await self._stop_locked(record)

    async def _stop_locked(self, record: SessionRecord) -> None:
        if not record.keeps_state:
            # Nothing on disk and nothing that earned any: a session that was
            # never named and never spoke has nothing to wake up again, so
            # stopping it is deleting it.
            await self._delete_locked(record)
            return
        await self._settle_turn(record, "the session was stopped")
        agent = record.agent
        # Not live from here on, before any await: the slot this session held
        # is free the moment its agent is given up, and a second caller
        # counting live sessions must not still see it.
        record.release()
        if agent is not None:
            await agent.stop()
        self._persist_meta(record)
        self._emit({"type": "session_updated", **record.wire()})

    async def delete_session(self, session_id: str) -> None:
        async with self._locked(session_id) as record:
            await self._delete_locked(record)

    async def _delete_locked(self, record: SessionRecord) -> None:
        session_id = record.session_id
        self.log.info("deleting session=%s name=%s live=%s", session_id,
                      record.name, record.live)
        await self._settle_turn(record, "the session was deleted")
        agent = record.agent
        # The record is the whole of the session, so discarding it is the
        # whole of the cleanup. Nothing here lists fields, which is what used
        # to make the two teardown paths disagree.
        self._records.pop(session_id, None)
        record.agent = None
        if agent is not None:
            await agent.stop()
        self.store.delete(session_id)
        self._emit({"type": "session_removed", "session_id": session_id})

    async def rename_session(self, session_id: str, name: str) -> None:
        async with self._locked(session_id) as record:
            self._rename_locked(record, name)

    def _rename_locked(self, record: SessionRecord, name: str) -> None:
        name = (name or "").strip()
        if not name:
            raise FalconFoxError("session name must not be empty")
        record.name = name
        record.auto_named = False
        self._persist_meta(record)
        self._emit({"type": "session_updated", **record.wire()})

    async def set_tags(self, session_id: str, tags: list) -> list[str]:
        """Replace a session's tags, in the order given.

        Replace rather than add/remove, because the order is the payload as
        much as the membership is: a client with one slot to fill reads the
        first tag it recognises, so "which comes first" has to be sayable in
        one call. An empty list clears them.
        """
        async with self._locked(session_id) as record:
            record.tags = _normalize_tags(tags)
            self._persist_meta(record)
            self._emit({"type": "session_updated", **record.wire()})
            return list(record.tags)

    # --- transcript utilities retained from the existing daemon --------

    async def revert_session(self, session_id: str, event_index: int) -> None:
        async with self._locked(session_id) as record:
            transcript = self._ensure_transcript(record)
            if event_index < 0 or event_index >= len(transcript):
                raise FalconFoxError(f"event_index {event_index} out of range")
            target = transcript[event_index]
            if target.get("type") != "message" or target.get("role") != "user":
                raise FalconFoxError("revert target must be a user message")
            # Settled first, and the truncation read only afterwards: a turn
            # still in flight has events to emit, and they belong on the
            # transcript this rewrites rather than appended to the file after
            # it has been rewritten.
            await self._settle_turn(record, "the conversation was reverted")
            truncated = self._ensure_transcript(record)[:event_index]
            agent = record.agent
            if agent is not None:
                record.agent = None
                record.state = "stored"
                await agent.stop()
            record.acp_id = None
            record.transcript = truncated
            if record.persisted:
                self.store.rewrite_transcript(session_id, truncated)
                self._persist_meta(record)
            self._emit({"type": "session_updated", **record.wire()})
            self._emit({"type": "transcript_reset", "session_id": session_id,
                        "transcript": truncated})

    async def fork_session(self, session_id: str, event_index: Optional[int] = None) -> str:
        async with self._locked(session_id) as source:
            return self._fork_locked(source, event_index)

    def _fork_locked(self, source: SessionRecord,
                     event_index: Optional[int]) -> str:
        transcript = list(self._ensure_transcript(source))
        if event_index is not None:
            if event_index < 0 or event_index > len(transcript):
                raise FalconFoxError(f"event_index {event_index} out of range")
            transcript = transcript[:event_index]
        new_id = new_session_id()
        now = _now_iso()
        fork = SessionRecord(
            session_id=new_id,
            name=f"{source.name} (fork)",
            path=source.path,
            backend=source.backend,
            created=now,
            last_active=now,
            hidden=source.hidden,
            tags=list(source.tags),
            roles=list(source.roles),
            oriented=source.oriented,
            transcript=transcript,
            auto_named=False,
            persisted=True,
        )
        self._records[new_id] = fork
        self._persist_meta(fork)
        self.store.rewrite_transcript(new_id, transcript)
        self._emit({"type": "session_added", **fork.wire()})
        return new_id

    async def name_session(self, session_id: str) -> None:
        record = self._require(session_id)
        transcript_text = self._transcript_text(record)
        if not transcript_text.strip():
            raise FalconFoxError("nothing to name yet — the session has no messages")
        if not self.config.naming_backend:
            raise FalconFoxError("session naming is disabled — set naming_backend in config.toml")
        try:
            backend = self.config.select_backend(self.config.naming_backend)
        except KeyError as error:
            raise FalconFoxError(str(error)) from error
        prompt = f"{self.config.naming_prompt}\n\n--- transcript ---\n{transcript_text}"
        # Named outside any lock: this is a whole model call, and holding the
        # session against every other transition for its duration would be a
        # worse bargain than the rename is worth.
        reply = await oneshot.one_shot(backend, Path(record.path), prompt)
        name = _clean_name(reply)
        if name:
            await self.rename_session(session_id, name)

    def _transcript_text(self, record: SessionRecord, limit: int = 6000) -> str:
        """The conversation as text, for a backend that cannot reload it.

        System messages are included, which they were not before. They are the
        orientation, delivered in the user's turn and part of what this
        session was told, so a replay that dropped them would hand the agent
        its history with the explanation of where it is removed.
        """
        lines = []
        for event in self._ensure_transcript(record):
            if event.get("type") != "message":
                continue
            if event.get("role") in ("user", "agent"):
                lines.append(f"{event['role']}: {event.get('text', '')}")
        return "\n".join(lines)[-limit:]

    # --- config options and permissions --------------------------------

    async def _apply_config_options(self, record: SessionRecord) -> None:
        session_id = record.session_id
        session = record.agent
        backend = self.config.select_backend(record.backend or None)
        for config_id, preference in backend.config_options.items():
            option = next((o for o in session.config_options if o["id"] == config_id), None)
            if option is None:
                self._warn_option(session_id, f"backend advertises no option {config_id!r}")
                continue
            desired = resolve_config_value(option, preference)
            if desired is None:
                self._warn_option(session_id, f"configured value for {config_id!r} matched nothing")
                continue
            if desired != option["current_value"]:
                try:
                    await session.set_config_option(config_id, desired)
                except Exception as error:
                    self._warn_option(session_id, f"could not apply {config_id}: {error}")
        self._publish_config_options(session_id, session)

    def _warn_option(self, session_id: str, message: str) -> None:
        self._emit({"type": "notice", "session_id": session_id,
                    "level": "error", "message": message})

    def _publish_config_options(self, session_id: str, session: AgentSession) -> None:
        self._emit({"type": "config_options", "session_id": session_id,
                    "options": session.config_options})

    async def set_config_option(self, session_id: str, config_id: str, value) -> None:
        async with self._locked(session_id) as record:
            if record.agent is None:
                raise FalconFoxError("session must be live to change an option")
            await record.agent.set_config_option(config_id, value)
            self._publish_config_options(session_id, record.agent)

    async def _request_permission(self, payload: dict) -> Optional[str]:
        """Always allow when possible; empty options are an immediate denial.

        The old coordinator created an unresolved future for this edge case,
        hanging unattended clients forever. The PoC intentionally has no
        interactive permission posture.
        """
        session_id = payload.get("session_id")
        chosen = _auto_allow_option(payload.get("options", []))
        if chosen is None:
            self._emit({"type": "permission_resolved", "session_id": session_id,
                        "option_id": None, "denied": True})
            return None
        tool = payload.get("tool_call", {}).get("title") or "tool call"
        self._emit({"type": "notice", "session_id": session_id,
                    "message": f"auto-allowed: {tool}"})
        return chosen

    # --- socket state and lifecycle ------------------------------------

    def snapshot(self) -> dict:
        # Only sessions the backend has actually spoken about carry an entry,
        # as before: an absent key and an empty one are different answers.
        return {
            "type": "snapshot",
            "sessions": self.list_sessions(),
            "config_options": {record.session_id: record.config_options
                               for record in self._records.values()
                               if record.config_options is not None},
            "commands": {record.session_id: record.commands
                         for record in self._records.values()
                         if record.commands is not None},
            "usage": {record.session_id: record.usage
                      for record in self._records.values()
                      if record.usage is not None},
        }

    async def shutdown(self) -> None:
        live = [record for record in self._records.values() if record.live]
        self.log.info("coordinator shutdown: sessions=%d", len(live))
        for record in live:
            agent = record.agent
            record.agent = None
            await agent.stop()
