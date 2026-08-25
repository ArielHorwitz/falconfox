"""Two-channel Telegram client for FalconFox."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from watchfiles import awatch
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

# Diagnostic machinery, not daemon protocol: the 2026-08-25 keepalive stalls
# could not even be attributed to a side, because neither process recorded its
# own freezes. Both run the same watchdog; sharing it crosses no boundary.
from falconfox.watchdog import StallWatchdog

from .api import ApiError, DaemonApi, TelegramApi
from .rendering import render_messages

log = logging.getLogger("falconfox.telegram")

# A daemon restart used to be invisible from the phone: the bot reconnected in
# silence, and unless a turn happened to be in flight nothing was ever said. Self
# -updating from inside a session makes restarts routine, so they get announced.
DAEMON_DOWN = "\u26a0\ufe0f Daemon connection lost \u2014 reconnecting."
DAEMON_UP = "\u2705 FalconFox is up"
BUSY_TURN = (
    "Still working on the previous message, so this one was not sent — send it "
    "again once the reply arrives."
)
INTERRUPTED_TURN = (
    "Lost the connection mid-turn, so anything not already sent is gone. The "
    "session still has it — ask it to repeat."
)
# The recurring silent failure: a turn ends, nothing was ever delivered, and no
# layer had an error to report. Now the moment it happens, the chat hears it.
SILENT_TURN = "⚠️ The turn ended without delivering a reply ({detail})."


@dataclass(frozen=True)
class BotConfig:
    token: str
    focus_chat_id: int
    work_chat_id: int
    daemon_url: str = "http://127.0.0.1:9721"
    pointer_file: Path = Path.home().joinpath(".local/state/falconfox/telegram/focus")
    focus_backend: str | None = None
    default_path: Path = Path.home()

    @classmethod
    def from_env(cls) -> "BotConfig":
        try:
            token = os.environ["FALCONFOX_TELEGRAM_TOKEN"]
            focus_chat = int(os.environ["FALCONFOX_TELEGRAM_FOCUS_CHAT_ID"])
            work_chat = int(os.environ["FALCONFOX_TELEGRAM_WORK_CHAT_ID"])
        except (KeyError, ValueError) as error:
            raise ValueError(
                "set FALCONFOX_TELEGRAM_TOKEN, FALCONFOX_TELEGRAM_FOCUS_CHAT_ID, "
                "and FALCONFOX_TELEGRAM_WORK_CHAT_ID"
            ) from error
        return cls(
            token=token,
            focus_chat_id=focus_chat,
            work_chat_id=work_chat,
            daemon_url=os.environ.get("FALCONFOX_URL", "http://127.0.0.1:9721"),
            pointer_file=Path(os.environ.get(
                "FALCONFOX_TELEGRAM_POINTER_FILE",
                str(Path.home().joinpath(".local/state/falconfox/telegram/focus")),
            )).expanduser(),
            focus_backend=os.environ.get("FALCONFOX_TELEGRAM_FOCUS_BACKEND") or None,
            default_path=Path(os.environ.get(
                "FALCONFOX_TELEGRAM_DEFAULT_PATH", str(Path.home())
            )).expanduser(),
        )


# Telegram has no "thinking", "working" or "stuck" chat action: every one of the
# eleven valid values describes the bot producing a kind of content. So the
# vocabulary gets spent as a code -- one distinct action per state we can
# actually tell apart -- which is the most this channel can carry. Each lasts
# about five seconds, hence the refresh loop below.
#
# Which glyph means what is deliberately arbitrary for now. What matters is that
# the states are distinguishable in the chat; the mapping is a table of one-line
# choices to reshuffle here, in one place, once we have watched it in use.
#
# Note `record_voice` is on loan: it is the honest action for a reply that is
# itself a voice message, which is what the deferred voice work would produce.
# If that lands, move audio to `upload_voice` or move streaming to one of the
# unused actions (`choose_sticker` aside, `find_location`, `upload_photo`, the
# video ones).
TURN_ACTIONS = {
    "starting": "choose_sticker",    # resuming or launching the ACP subprocess
    "working": "typing",             # alive, but producing nothing right now
    "thinking": "find_location",     # agent_thought_chunk
    "streaming": "record_voice",     # agent_message_chunk -- output is flowing
    "tool": "upload_document",       # a tool call is running
}
DEFAULT_ACTION = TURN_ACTIONS["working"]
ACTION_REFRESH_SECONDS = 4

# A turn's reply is normally sent as one message when the turn ends. When output
# stops but the turn does not -- the agent has gone off to run a tool -- what has
# accumulated is pushed early instead, so a long turn delivers progress rather
# than a promise of it. Guarded, because the point is to make a turn feel alive,
# not to narrate it: Telegram rate-limits messages per chat far harder than chat
# actions, and chat clutter on a phone is a first-order cost.
MIN_FLUSH_CHARS = 240
MIN_FLUSH_SECONDS = 15


class FalconFoxTelegramBot:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.daemon = DaemonApi(config.daemon_url)
        self.telegram = TelegramApi(config.token)
        self.pointer = config.pointer_file.resolve()
        self.focus_session_id: str | None = None
        self._pointer_value: str | None = None
        self._focus_working = False
        self._rotate_pending = False
        self._reply_parts: dict[str, list[str]] = {}
        self._turn_chat: dict[str, int] = {}
        self._activity_tasks: dict[str, asyncio.Task] = {}
        self._activity_state: dict[str, str] = {}
        self._last_flush: dict[str, float] = {}
        self._turn_working: set[str] = set()
        # The daemon's id for the turn this client is carrying, plus what this
        # client has actually handed to Telegram for it — the two facts that
        # let a turn which delivered nothing be caught instead of shrugged at.
        self._turn_id: dict[str, str] = {}
        self._delivered: dict[str, int] = {}
        self._turn_started_at: dict[str, float] = {}
        self._action_sends: set[asyncio.Task] = set()
        self._ws = None
        self._ws_lock = asyncio.Lock()

    async def run(self) -> None:
        StallWatchdog(logging.getLogger("falconfox.telegram.watchdog")).start()
        self._prepare_focus_workspace()
        ws_url = self.config.daemon_url.replace("http://", "ws://", 1).replace(
            "https://", "wss://", 1
        ) + "/ws"
        # `async for ... in connect(...)` retries the connection with backoff,
        # so the bot survives daemon restarts (e.g. a self-update) instead of
        # dying with the connection.
        async for websocket in connect(ws_url):
            try:
                await self._run_connected(websocket)
            except (ConnectionClosed, ApiError, OSError) as error:
                log.warning("daemon connection lost (%s); reconnecting", error)
                await self._announce(DAEMON_DOWN)
                await asyncio.sleep(2)
                continue

    async def _run_connected(self, websocket) -> None:
        self._ws = websocket
        snapshot = json.loads(await websocket.recv())
        if snapshot.get("type") != "snapshot":
            raise RuntimeError("FalconFox did not send an initial snapshot")
        # Announced on every connection, not only on a reconnect: a deploy
        # restarts the bot too, so the process that saw the daemon go down is
        # rarely the one that sees it return. A bare "up" after a bot-only
        # restart is worth saying anyway -- it reports the restart.
        await self._announce_daemon_up()
        await self._ensure_work_pointer()
        # Rotate rather than plain-spawn: after a reconnect that was not a
        # daemon restart, this also cleans up the previous focus session.
        await self._rotate_focus_session()
        loops = [asyncio.create_task(coroutine) for coroutine in (
            self._receive_events(), self._poll_telegram(), self._watch_pointer(),
        )]
        try:
            # All three loops are endless, so any completion means the
            # connection (or a loop) is gone; surface its outcome.
            done, _pending = await asyncio.wait(loops, return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                finished.result()
        finally:
            for loop in loops:
                loop.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            # Dropping the connection discards whatever each in-flight turn
            # had accumulated. Say so, rather than leaving a chat waiting on
            # a reply that can no longer arrive -- the session still holds the
            # turn, so the content is recoverable, but only if you know.
            for chat_id in self._reset_connection_state():
                try:
                    await self.telegram.message(chat_id, INTERRUPTED_TURN)
                except Exception:
                    log.warning("could not report the lost turn to %s", chat_id)

    async def _announce(self, text: str) -> None:
        """Tell the work chat something about the bot itself. Never fatal."""
        try:
            await self.telegram.message(self.config.work_chat_id, text)
        except Exception:
            # An announcement failing must not take down the connection it is
            # announcing -- that would turn a blip into an outage.
            log.warning("could not announce to the work chat: %s", text)

    async def _announce_daemon_up(self) -> None:
        # Over the API rather than importing falconfox: the bot is a client of
        # the daemon, and the revision it reports should be the daemon's own.
        try:
            version = (await self.daemon.version()).get("version")
        except Exception:
            version = None
        await self._announce(f"{DAEMON_UP} ({version})." if version else f"{DAEMON_UP}.")

    def _reset_connection_state(self) -> list[int]:
        """Clear per-connection state; return chats left mid-turn."""
        self._ws = None
        self._focus_working = False
        self._rotate_pending = False
        for activity in self._activity_tasks.values():
            activity.cancel()
        self._activity_tasks.clear()
        self._activity_state.clear()
        self._last_flush.clear()
        self._turn_working.clear()
        self._turn_id.clear()
        self._delivered.clear()
        self._turn_started_at.clear()
        interrupted = sorted(set(self._turn_chat.values()))
        self._turn_chat.clear()
        self._reply_parts.clear()
        return interrupted

    def _prepare_focus_workspace(self) -> None:
        # Keep the pointer inside the focus session's cwd so ACP-brokered file
        # writes as well as ordinary shell writes can reach it.
        root = self.pointer.parent
        skill_dir = root.joinpath(".agents", "skills", "falconfox-pointer")
        skill_dir.mkdir(parents=True, exist_ok=True)
        packaged_skill = files("falconfox_telegram").joinpath(
            "skills", "falconfox-pointer", "SKILL.md"
        ).read_text()
        skill_dir.joinpath("SKILL.md").write_text(packaged_skill)
        # Claude discovers skills under .claude/skills; bridge with a symlink.
        claude_dir = root.joinpath(".claude")
        claude_dir.mkdir(exist_ok=True)
        skills_link = claude_dir.joinpath("skills")
        if not skills_link.is_symlink() and not skills_link.exists():
            skills_link.symlink_to(Path("..", ".agents", "skills"))
        orientation = (
            "You are the FalconFox Telegram session manager: the single-purpose "
            "session behind the focus chat. You are NOT a work agent — you manage "
            "sessions, you do not work inside them. That includes deciding which "
            "session the work chat talks to, and also spawning, renaming, stopping "
            "and deleting sessions. For every user message, follow the "
            "falconfox-pointer skill (.agents/skills/falconfox-pointer). "
            f"The pointer file is `{self.pointer}`. You may run any `falconfox` "
            "command and write that pointer file — nothing else. Confirm the target "
            "back to the user before `stop` or `delete`. Never orient on or work in "
            "any project. When greeting or unsure, ask what the user wants to do.\n"
        )
        # Both files, so every agent runtime picks the orientation up natively.
        root.joinpath("AGENTS.md").write_text(orientation)
        root.joinpath("CLAUDE.md").write_text(orientation)
        self.focus_workspace = root

    async def _ensure_work_pointer(self) -> None:
        sessions = await self.daemon.sessions()
        current = self._read_pointer()
        if current and any(item["session_id"] == current for item in sessions):
            self._pointer_value = current
            return
        session = await self.daemon.spawn(path=str(self.config.default_path), name="telegram home")
        self._write_pointer(session["session_id"])
        self._pointer_value = session["session_id"]

    async def _spawn_focus_session(self) -> None:
        session = await self.daemon.spawn(
            path=str(self.focus_workspace), name="telegram focus",
            backend=self.config.focus_backend, ephemeral=True,
        )
        self.focus_session_id = session["session_id"]
        self._focus_working = False

    async def _rotate_focus_session(self) -> None:
        old = self.focus_session_id
        await self._spawn_focus_session()
        self._rotate_pending = False
        if old:
            try:
                await self.daemon.delete(old)
            except ApiError:
                log.warning("could not delete rotated focus session %s", old, exc_info=True)

    def _read_pointer(self) -> str | None:
        try:
            value = self.pointer.read_text().strip()
            return value or None
        except OSError:
            return None

    def _write_pointer(self, session_id: str) -> None:
        self.pointer.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.pointer.with_suffix(".tmp")
        temporary.write_text(session_id + "\n")
        temporary.replace(self.pointer)

    async def _watch_pointer(self) -> None:
        self.pointer.parent.mkdir(parents=True, exist_ok=True)
        async for changes in awatch(self.pointer.parent):
            if not any(Path(changed).resolve() == self.pointer for _change, changed in changes):
                continue
            current = self._read_pointer()
            if not current or current == self._pointer_value:
                continue
            sessions = await self.daemon.sessions()
            match = next((item for item in sessions if item["session_id"] == current), None)
            if match is None:
                await self.telegram.message(self.config.work_chat_id,
                                            f"Focus pointer rejected unknown session {current}.")
                continue
            self._pointer_value = current
            await self.telegram.message(
                self.config.work_chat_id,
                f"Focused on {match['name']} ({current}) — {match['path']}",
            )
            if self._focus_working:
                self._rotate_pending = True
            else:
                await self._rotate_focus_session()

    async def _poll_telegram(self) -> None:
        offset = None
        while True:
            try:
                updates = await self.telegram.updates(offset)
                for update in updates:
                    offset = update["update_id"] + 1
                    await self._handle_update(update)
            except ApiError as error:
                log.warning("Telegram polling failed: %s", error)
                await asyncio.sleep(2)

    async def _handle_update(self, update: dict) -> None:
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id not in (self.config.focus_chat_id, self.config.work_chat_id):
            return
        text = message.get("text")
        if not text:
            await self.telegram.message(chat_id, "Text messages only in this PoC.")
            return
        if text.startswith("/"):
            if await self._command(chat_id, text):
                return
        if chat_id == self.config.focus_chat_id:
            target = self.focus_session_id
        else:
            target = self._pointer_value or self._read_pointer()
        if not target:
            await self.telegram.message(chat_id, "No focused FalconFox session.")
            return
        await self._forward(target, chat_id, text)

    async def _command(self, chat_id: int, text: str) -> bool:
        try:
            parts = shlex.split(text)
        except ValueError as error:
            await self.telegram.message(chat_id, f"Invalid command: {error}")
            return True
        command = parts[0].split("@", 1)[0]
        if command == "/status":
            # Diagnosis from the phone: what the daemon knows about sessions,
            # and what this bot *believes* is in flight — the five parallel
            # dicts that every silent failure so far has been a hidden state of.
            await self.telegram.message(chat_id, await self._status_report())
            return True
        if command == "/list":
            sessions = await self.daemon.sessions()
            listing = "\n".join(
                f"{item['session_id']}  {item['name']}  [{item['state']}]  {item['path']}"
                for item in sessions
            ) or "No sessions."
            await self.telegram.message(chat_id, listing)
            return True
        if command in ("/new", "/home"):
            path = str(self.config.default_path) if command == "/home" or len(parts) < 2 else parts[1]
            name_start = 1 if command == "/home" else 2
            name = " ".join(parts[name_start:]) or None
            session = await self.daemon.spawn(path=path, name=name)
            self._write_pointer(session["session_id"])
            return True
        if command == "/switch":
            if len(parts) != 2:
                await self.telegram.message(chat_id, "Usage: /switch <session-id>")
                return True
            sessions = await self.daemon.sessions()
            if not any(item["session_id"] == parts[1] for item in sessions):
                await self.telegram.message(chat_id, f"Unknown session: {parts[1]}")
                return True
            self._write_pointer(parts[1])
            return True
        if command == "/name":
            if len(parts) < 2 or not self._pointer_value:
                await self.telegram.message(chat_id, "Usage: /name <new name>")
                return True
            await self.daemon.rename(self._pointer_value, " ".join(parts[1:]))
            await self.telegram.message(chat_id, f"Renamed session to {' '.join(parts[1:])}.")
            return True
        return False

    async def _status_report(self) -> str:
        try:
            version = (await self.daemon.version()).get("version")
        except Exception:
            version = "daemon unreachable"
        sessions = await self.daemon.sessions()
        names = {item["session_id"]: item["name"] for item in sessions}
        lines = [f"FalconFox {version}"]
        focused = self._pointer_value or self._read_pointer()
        match = next((item for item in sessions if item["session_id"] == focused), None)
        if match:
            lines.append(f"Focused: {match['name']} ({focused}) [{match['state']}] "
                         f"— {match['path']}")
        else:
            lines.append(f"Focused: {focused or 'none'}")
        for item in sessions:
            lines.append(f"  {item['session_id']}  {item['name']}  [{item['state']}]")
        if not self._turn_chat:
            lines.append("No turn in flight (bot view).")
        else:
            lines.append("In flight (bot view):")
            now = time.monotonic()
            for session_id, turn_chat in self._turn_chat.items():
                chat = "focus" if turn_chat == self.config.focus_chat_id else "work"
                started = self._turn_started_at.get(session_id)
                age = f"{now - started:.0f}s ago" if started is not None else "unknown"
                buffered = sum(len(part) for part in self._reply_parts.get(session_id, []))
                lines.append(
                    f"  {names.get(session_id, session_id)}: chat={chat} "
                    f"turn={self._turn_id.get(session_id) or '?'} "
                    f"activity={self._activity_state.get(session_id) or '?'} "
                    f"buffered={buffered} delivered={self._delivered.get(session_id, 0)} "
                    f"started {age}")
        return "\n".join(lines)

    def _start_activity(self, session_id: str, chat_id: int) -> bool:
        """Ensure a refresh loop is running. True if this call started one."""
        task = self._activity_tasks.get(session_id)
        # `task.done()` matters: a finished task is still *in* the dict, and the
        # old `session_id not in self._typing_tasks` guard read that as live. One
        # failed sendChatAction therefore silenced a turn permanently, with the
        # `working` safety net unable to restart it because it hit the same
        # guard.
        if task is not None and not task.done():
            return False
        self._activity_tasks[session_id] = asyncio.create_task(
            self._activity_loop(session_id, chat_id))
        return True

    async def _set_activity(self, session_id: str, state: str) -> None:
        """Record what the session is doing and show it in the chat."""
        chat_id = self._turn_chat.get(session_id)
        if chat_id is None:
            return
        # Before the equality check, so this doubles as the safety net that
        # revives a loop which died mid-turn.
        started = self._start_activity(session_id, chat_id)
        previous = self._activity_state.get(session_id)
        if previous == state:
            return
        self._activity_state[session_id] = state
        # Streamed output fires an event per chunk; only a *change* is worth an
        # API call. A fresh loop sends immediately, so it needs no second one.
        #
        # Detached, never awaited inline: this sits on the event-pipeline path,
        # and one hung Telegram call here stalls every queued daemon event
        # behind it. Observed live (2026-08-25, 09:06): a 40s read timeout
        # delayed a finished reply by 45 seconds. The indicator is droppable
        # decoration; the pipeline is not allowed to wait for it.
        if not started:
            task = asyncio.create_task(self._send_action(session_id, chat_id))
            self._action_sends.add(task)
            task.add_done_callback(self._action_sends.discard)
        # Output has stopped while the turn continues -- the moment to hand over
        # what has arrived so far.
        if previous == "streaming":
            await self._flush_reply(session_id, chat_id)

    async def _flush_reply(self, session_id: str, chat_id: int, *,
                           final: bool = False) -> None:
        """Send the accumulated reply. Partial flushes must earn their message."""
        text = "".join(self._reply_parts.get(session_id, [])).strip()
        if not text:
            return
        if not final:
            if len(text) < MIN_FLUSH_CHARS:
                return
            # An odd number of fences means the stream stopped inside a code
            # block. The renderer tolerates that, but the reader would get half a
            # block and an unfenced remainder; wait for the next chance instead.
            if text.count("```") % 2:
                return
            now = time.monotonic()
            if now - self._last_flush.get(session_id, 0.0) < MIN_FLUSH_SECONDS:
                return
            self._last_flush[session_id] = now
        # Keep the list -- the turn is not over, and what comes next belongs to
        # the same reply.
        self._reply_parts[session_id] = []
        log.info("flush %s: session=%s chat=%s chars=%d",
                 "final" if final else "partial", session_id, chat_id, len(text))
        for rendered in render_messages(text):
            await self.telegram.html_message(chat_id, rendered.html, rendered.plain)
        self._delivered[session_id] = self._delivered.get(session_id, 0) + len(text)

    async def _send_action(self, session_id: str, chat_id: int) -> None:
        action = TURN_ACTIONS.get(
            self._activity_state.get(session_id, ""), DEFAULT_ACTION)
        try:
            await self.telegram.chat_action(chat_id, action)
        except ApiError as error:
            # Never fatal to the loop. A 429 from the rate limiter -- likeliest
            # on exactly the long turn that needs an indicator -- or one of the
            # read timeouts this deployment sees used to end the task outright
            # and leave the turn silent for the rest of its life.
            log.debug("chat action %s failed for %s: %s", action, session_id, error)

    async def _forward(self, session_id: str, chat_id: int, text: str) -> None:
        if session_id in self._turn_chat:
            # The daemon refuses a prompt while a turn is running, and says so
            # with an *info* notice -- which this client does not surface, so the
            # message vanished without a trace. Worse, forwarding it anyway reset
            # the buffers below and destroyed the reply already in flight. Refuse
            # here instead, and say so, so the text is never silently eaten.
            log.info("refused mid-turn message: session=%s chat=%s", session_id, chat_id)
            await self.telegram.message(chat_id, BUSY_TURN)
            return
        log.info("forward: session=%s chat=%s chars=%d", session_id, chat_id, len(text))
        self._turn_chat[session_id] = chat_id
        self._reply_parts[session_id] = []
        self._delivered[session_id] = 0
        self._turn_started_at[session_id] = time.monotonic()
        self._turn_working.discard(session_id)
        # Type from the moment the prompt goes out. Waiting for the daemon to
        # report `working` leaves the whole backend-startup window silent: a
        # stored session resumes an ACP subprocess first, and the daemon carries
        # that as a `starting` state on session_updated, which this client does
        # not consume. That gap is exactly when a turn looks like it hung.
        await self._set_activity(session_id, "working")
        async with self._ws_lock:
            await self._ws.send(json.dumps({
                "action": "send", "session_id": session_id, "text": text,
            }))

    async def _receive_events(self) -> None:
        async for raw in self._ws:
            await self._handle_event(json.loads(raw))

    async def _handle_event(self, event: dict) -> None:
        session_id = event.get("session_id")
        if not session_id:
            return
        event_type = event.get("type")
        if event_type == "message":
            role = event.get("role")
            if role == "agent":
                self._reply_parts.setdefault(session_id, []).append(event.get("text", ""))
                await self._set_activity(session_id, "streaming")
            elif role == "thought":
                # Never part of the reply -- only a sign of what is happening.
                await self._set_activity(session_id, "thinking")
            return
        if event_type == "tool_call":
            # Consumed as a *state signal* only. Tool calls stay suppressed as
            # content: one message per turn is a deliberate choice for a phone
            # chat, and rendering them would undo it.
            status = event.get("status")
            await self._set_activity(
                session_id, "working" if status in ("completed", "failed") else "tool")
            return
        if event_type == "session_updated":
            # The daemon carries a resuming ACP subprocess as `starting`, the
            # slowest part of a cold turn. The client used to ignore this event
            # entirely, so that whole window looked identical to working.
            if event.get("state") == "starting":
                await self._set_activity(session_id, "starting")
            return
        if event_type == "notice" and event.get("level") == "error":
            chat_id = self._turn_chat.get(session_id)
            if chat_id:
                await self.telegram.message(chat_id, f"FalconFox error: {event.get('message', '')}")
            return
        if event_type == "turn_started":
            # The daemon's own name for the turn this chat is waiting on. Turns
            # driven by other clients (the focus agent's CLI sends, the web UI)
            # have no chat here and are none of our business.
            if session_id in self._turn_chat:
                self._turn_id[session_id] = event.get("turn_id") or ""
                log.info("turn started: session=%s turn=%s", session_id, event.get("turn_id"))
            return
        if event_type == "turn_ended":
            # The authoritative end of a turn. `idle` below stays only as a
            # backstop — it is a state, not an event, and reading it as "turn
            # over" is how replies used to vanish.
            await self._finish_turn(session_id, event)
            return
        if event_type != "agent_state":
            return
        state = event.get("state")
        if session_id == self.focus_session_id:
            self._focus_working = state == "working"
        if state == "working":
            # Normally already active since _forward; this covers a turn that
            # began before the indicator did, and revives a loop that has died.
            self._turn_working.add(session_id)
            await self._set_activity(session_id, "working")
            return
        if state != "idle":
            return
        if (session_id in self._turn_chat and session_id not in self._turn_working
                and not self._reply_parts.get(session_id)):
            # Resuming a stored session emits `idle` *before* the turn starts
            # (engine/session.py sets it once the ACP subprocess is up). Treating
            # that as the end of the turn tore down _turn_chat before a single
            # chunk had arrived, so the real reply streamed into a session with
            # nowhere to send it and was dropped in silence -- every first turn
            # after a daemon restart. A turn ends only if it ever began.
            #
            # The empty-buffer condition is the safety catch: if anything has
            # streamed, the turn plainly began, so an idle ends it whatever the
            # state flags say. Without it, one confused flag strands the session
            # forever -- observed live, with the indicator left running for 54
            # minutes and every later message refused.
            log.info("ignoring pre-turn idle for session=%s", session_id)
            return
        # Normally a no-op: turn_ended has already finalized, and _finish_turn
        # is idempotent. Kept so a daemon that never sent one (or a turn whose
        # end this client somehow missed) still cannot strand the session.
        await self._finish_turn(session_id, None)

    async def _finish_turn(self, session_id: str, event: dict | None) -> None:
        """Close out a turn: deliver the remainder, stop the indicator, account
        for what was handed over — and say so when that is nothing. Idempotent:
        the `idle` that follows a `turn_ended` finds nothing left to do."""
        self._turn_working.discard(session_id)
        activity = self._activity_tasks.pop(session_id, None)
        if activity:
            activity.cancel()
        self._activity_state.pop(session_id, None)
        chat_id = self._turn_chat.pop(session_id, None)
        turn_id = self._turn_id.pop(session_id, None) or (event or {}).get("turn_id")
        started = self._turn_started_at.pop(session_id, None)
        if chat_id is not None:
            await self._flush_reply(session_id, chat_id, final=True)
        delivered = self._delivered.pop(session_id, 0)
        self._reply_parts.pop(session_id, None)
        self._last_flush.pop(session_id, None)
        if chat_id is not None:
            outcome = (event or {}).get("outcome")
            stop = (event or {}).get("stop_reason")
            elapsed = time.monotonic() - started if started is not None else -1.0
            log.info("turn ended: session=%s turn=%s outcome=%s stop=%s "
                     "delivered=%d chars in %.1fs",
                     session_id, turn_id, outcome, stop, delivered, elapsed)
            if delivered == 0 and outcome != "error" and stop != "cancelled":
                # An errored turn already surfaced its error notice, and a
                # cancelled one is empty on purpose. Anything else that ends
                # with nothing delivered is the silent failure this client
                # kept producing -- so it stops being silent, in both places.
                streamed = (event or {}).get("output_chars")
                if streamed:
                    detail = (f"the agent wrote {streamed} characters "
                              "that were lost on the way to this chat")
                else:
                    detail = f"the agent produced no output; stop reason: {stop or 'unknown'}"
                log.warning("turn delivered nothing: session=%s turn=%s %s",
                            session_id, turn_id, detail)
                await self.telegram.message(chat_id, SILENT_TURN.format(detail=detail))
        if session_id == self.focus_session_id and self._rotate_pending:
            await self._rotate_focus_session()

    async def _activity_loop(self, session_id: str, chat_id: int) -> None:
        try:
            while True:
                await self._send_action(session_id, chat_id)
                await asyncio.sleep(ACTION_REFRESH_SECONDS)
        except asyncio.CancelledError:
            raise
